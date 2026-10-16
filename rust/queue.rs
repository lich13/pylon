//! Root-supervised durable admission and result mailbox. Never replay a started request.
use crate::{
    config::{Config, atomic_json},
    now,
};
use anyhow::{Context, Result, ensure};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::PathBuf,
    sync::{Mutex, OnceLock},
};

pub fn connect(config: &Config) -> Result<crate::db::PooledConnection> {
    if !config.home.join("run").is_dir() {
        std::fs::create_dir_all(config.home.join("run"))?;
    }
    // Keep SQLite sidecars and completion/context payloads out of the shared run directory.
    let private = config.home.join("supervisor");
    if std::fs::symlink_metadata(&private).is_err() {
        match std::fs::create_dir(&private) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(e) => return Err(e.into()),
        }
    }
    let metadata = std::fs::symlink_metadata(&private)?;
    ensure!(
        metadata.is_dir() && metadata.uid() == unsafe { libc::geteuid() },
        "unsafe supervisor state directory"
    );
    if metadata.permissions().mode() & 0o777 != 0o700 {
        std::fs::set_permissions(&private, std::fs::Permissions::from_mode(0o700))?;
    }
    let path = private.join("task-state.sqlite3");
    if let Ok(metadata) = std::fs::symlink_metadata(&path) {
        ensure!(
            metadata.is_file()
                && metadata.uid() == unsafe { libc::geteuid() }
                && metadata.nlink() == 1,
            "unsafe supervisor state file"
        );
    }
    let db = crate::db::pooled(&path)?;
    if std::fs::metadata(&path)?.permissions().mode() & 0o777 != 0o600 {
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
    }
    static INITIALIZED: OnceLock<Mutex<BTreeMap<PathBuf, (u64, u64)>>> = OnceLock::new();
    let metadata = std::fs::metadata(&path)?;
    let identity = (metadata.dev(), metadata.ino());
    let mut initialized = INITIALIZED
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| anyhow::anyhow!("queue initialization unavailable"))?;
    if initialized.get(&path) == Some(&identity) {
        return Ok(db);
    }
    db.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,module TEXT NOT NULL,topic TEXT NOT NULL,model INTEGER NOT NULL,state TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,execution_status TEXT,completion_status TEXT,detail TEXT NOT NULL DEFAULT '',report TEXT,token TEXT,owner INTEGER,completion_input TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS waiting_task ON requests(job_id) WHERE state='queued';
        CREATE TABLE IF NOT EXISTS triggers(id TEXT PRIMARY KEY,request_id TEXT NOT NULL);")?;
    initialized.insert(path, identity);
    Ok(db)
}
pub fn admit(
    config: &Config,
    job_id: &str,
    module: &str,
    topic: &str,
    model: bool,
    trigger: &str,
) -> Result<String> {
    let mut db = connect(config)?;
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let known: Option<String> = tx
        .query_row(
            "SELECT request_id FROM triggers WHERE id=?",
            [trigger],
            |r| r.get(0),
        )
        .optional()?;
    if let Some(id) = known {
        return Ok(id);
    }
    if config
        .home
        .join("run/invocations")
        .join(crate::digest(trigger))
        .exists()
    {
        tx.execute("INSERT OR IGNORE INTO requests(id,job_id,module,topic,model,state,created_at,updated_at,detail) VALUES(?,?,?,?,?,'uncertain',?,?,'legacy invocation retained; not replayed')",params![trigger,job_id,module,topic,model,now(),now()])?;
        tx.execute(
            "INSERT INTO triggers VALUES(?,?)",
            params![trigger, trigger],
        )?;
        tx.commit()?;
        summary(config)?;
        return Ok(trigger.to_owned());
    }
    let queued: Option<String> = tx
        .query_row(
            "SELECT id FROM requests WHERE job_id=? AND state='queued'",
            [job_id],
            |r| r.get(0),
        )
        .optional()?;
    let id = queued.unwrap_or_else(|| trigger.to_owned());
    tx.execute("INSERT OR IGNORE INTO requests(id,job_id,module,topic,model,state,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?)",params![id,job_id,module,topic,model,now(),now()])?;
    tx.execute("INSERT INTO triggers VALUES(?,?)", params![trigger, id])?;
    tx.commit()?;
    summary(config)?;
    Ok(id)
}
pub fn state(config: &Config, id: &str) -> Result<String> {
    Ok(connect(config)?.query_row("SELECT state FROM requests WHERE id=?", [id], |r| r.get(0))?)
}
pub fn summary(config: &Config) -> Result<()> {
    summary_inner(config, false)
}
pub fn summary_flush(config: &Config) -> Result<()> {
    summary_inner(config, true)
}
fn summary_inner(config: &Config, force: bool) -> Result<()> {
    let db = connect(config)?;
    let lock = crate::runtime::open_task_lock(&config.home.join("supervisor/summary.lock"))?;
    fs2::FileExt::lock_exclusive(&lock)?;
    let previous = public(config);
    if !force && (0.0..1.0).contains(&(now() - previous["updated_at"].as_f64().unwrap_or(0.0))) {
        return Ok(());
    }
    let mut q=db.prepare("SELECT id,job_id,module,state,created_at,updated_at,execution_status,completion_status,owner FROM requests WHERE state!='completed' OR updated_at>? ORDER BY created_at DESC LIMIT 200")?;
    let rows=q.query_map([now()-86400.0], |r| Ok(json!({"id":r.get::<_,String>(0)?,"job_id":r.get::<_,String>(1)?,"module":r.get::<_,String>(2)?,"state":r.get::<_,String>(3)?,"created_at":r.get::<_,f64>(4)?,"updated_at":r.get::<_,f64>(5)?,"execution_status":r.get::<_,Option<String>>(6)?,"completion_status":r.get::<_,Option<String>>(7)?,"owner":r.get::<_,Option<u32>>(8)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
    if previous["updated_at"].is_number() && previous["requests"] == json!(rows) {
        return Ok(());
    }
    // This contains no payload, context token, credential, path or user data.
    atomic_json(
        &config.home.join("run/task-summary.json"),
        &json!({"updated_at":now(),"requests":rows}),
        0o644,
    )
}
pub fn public(config: &Config) -> Value {
    std::fs::read(config.home.join("run/task-summary.json"))
        .ok()
        .and_then(|s| serde_json::from_slice(&s).ok())
        .unwrap_or_else(|| json!({"requests":[]}))
}
pub fn report(config: &Config, id: &str, token: &str, value: &Value) -> Result<()> {
    connect(config)?.execute("UPDATE requests SET state='reporting',report=?,token=?,execution_status=?,completion_status=?,detail=?,updated_at=? WHERE id=?",params![value.to_string(),token,value["execution_status"].as_str(),value["completion_status"].as_str(),value["detail"].as_str().unwrap_or(""),now(),id])?;
    summary(config)
}
pub async fn flush(config: &Config, id: &str) -> Result<()> {
    let (report, token): (String, String) = connect(config)?.query_row(
        "SELECT report,token FROM requests WHERE id=? AND state='reporting'",
        [id],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let value: Value = serde_json::from_str(&report)?;
    crate::api::rpc(config, "run.finish", value.clone(), &token).await?;
    // Persist the acknowledgement before closing context; resubmitting run.finish is idempotent.
    connect(config)?.execute(
        "UPDATE requests SET state='completed',updated_at=?,token=NULL,report=NULL,completion_input=CASE WHEN completion_status='ok' THEN NULL ELSE completion_input END WHERE id=?",
        params![now(), id],
    )?;
    summary(config)?;
    let _ = crate::api::rpc(config, "context.close", json!({}), &token).await;
    Ok(())
}
pub async fn dispatch(config: &Config) -> Result<()> {
    let config = config.clone();
    crate::isolated(async move { dispatch_inner(&config).await }).await
}
async fn dispatch_inner(config: &Config) -> Result<()> {
    let rows = {
        let db = connect(config)?;
        let mut q=db.prepare("SELECT id,job_id,state,token,execution_status FROM requests WHERE state IN ('queued','running','completing','reporting') ORDER BY created_at,id")?;
        q.query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, Option<String>>(3)?,
                r.get::<_, Option<String>>(4)?,
            ))
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?
    };
    for (id, job, _, _, _) in rows {
        let dispatch_lock = crate::runtime::open_task_lock(
            &config
                .home
                .join("run")
                .join(format!("dispatch-{}.lock", crate::digest(&id))),
        )?;
        if fs2::FileExt::try_lock_exclusive(&dispatch_lock).is_err() {
            continue;
        }
        // An OS lock, not a PID/age heuristic, owns an active invocation.
        let lock = crate::runtime::open_task_lock(
            &config
                .home
                .join("run")
                .join(format!("request-{}.lock", crate::digest(&id))),
        )?;
        if fs2::FileExt::try_lock_exclusive(&lock).is_err() {
            continue;
        }
        // The owner may have completed after the scan. Re-read while holding its lock.
        let (state, token, execution_status): (String, Option<String>, Option<String>) =
            connect(config)?.query_row(
                "SELECT state,token,execution_status FROM requests WHERE id=?",
                [&id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )?;
        if state == "completed" || state == "uncertain" {
            continue;
        }
        if state == "reporting" {
            if let Err(error) = flush(config, &id).await {
                crate::errors::log("queue-report", &error);
            }
        } else if state == "queued" {
            if config.home.join("run/freeze").exists() {
                continue;
            }
            let mut command = tokio::process::Command::new(std::env::current_exe()?);
            command.args(["--home", config.home.to_str().context("invalid home")?]);
            if config.offline {
                command.arg("--offline");
            }
            use std::os::fd::AsRawFd;
            let fd = dispatch_lock.as_raw_fd();
            unsafe {
                command.pre_exec(move || {
                    if libc::fcntl(fd, libc::F_SETFD, 0) < 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                    Ok(())
                });
            }
            // Drop only the request lock. The separate dispatch lock stays held across exec.
            drop(lock);
            let mut child = command
                .args(["resume", &id, "--dispatch-fd", &fd.to_string()])
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::piped())
                .spawn()?;
            let supervisor = config.clone();
            let identity = crate::digest(&job);
            tokio::spawn(async move {
                use tokio::io::AsyncReadExt;
                let stderr = child.stderr.take();
                let capture = tokio::spawn(async move {
                    let mut tail = Vec::new();
                    if let Some(mut stream) = stderr {
                        let mut bytes = [0; 8192];
                        while let Ok(n) = stream.read(&mut bytes).await {
                            if n == 0 {
                                break;
                            }
                            tail.extend_from_slice(&bytes[..n]);
                            if tail.len() > 65536 {
                                let excess = tail.len() - 65536;
                                tail.drain(..excess);
                            }
                        }
                    }
                    tail
                });
                let status = child.wait().await;
                let tail = capture.await.unwrap_or_default();
                if !status.as_ref().is_ok_and(|s| s.success()) {
                    let path = supervisor
                        .home
                        .join("supervisor")
                        .join(format!("resume-last-{}.json", &identity[..16]));
                    if let Err(error) = crate::blocking(move || {
                        atomic_json(
                            &path,
                            &json!({"at":now(),"stderr":crate::runtime::safe_tail(&tail)}),
                            0o600,
                        )
                    })
                    .await
                    {
                        crate::errors::log("resume-diagnostics", &error)
                    }
                }
                match status {
                    Ok(status) if status.success() => {}
                    Ok(status) => {
                        eprintln!("{}", json!({"event":"resume-exit","code":status.code()}))
                    }
                    Err(error) => crate::errors::log("resume-wait", &error.into()),
                }
            });
        } else {
            let report = json!({"id":id,"status":"uncertain","execution_status":execution_status.unwrap_or_else(||"uncertain".into()),"completion_status":"uncertain","notification_status":"unknown","detail":"supervisor lost before result was persisted; reconcile without replay"});
            if let Some(token) = token {
                self::report(config, &id, &token, &report)?;
            } else {
                connect(config)?.execute(
                    "UPDATE requests SET state='uncertain',updated_at=? WHERE id=?",
                    params![now(), id],
                )?;
            }
            let _ = job;
        }
    }
    summary(config)?;
    Ok(())
}
