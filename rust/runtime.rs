use crate::{
    api,
    catalog::{Catalog, Module},
    config::Config,
};
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::{self, File},
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

// Keep OS lock semantics shared by the supervisor, dispatch and module leases.
// Contention is expected; other I/O failures must not masquerade as a busy job.
pub(crate) fn try_lock(file: &File) -> std::io::Result<bool> {
    match file.try_lock() {
        Ok(()) => Ok(true),
        Err(std::fs::TryLockError::WouldBlock) => Ok(false),
        Err(std::fs::TryLockError::Error(error)) => Err(error),
    }
}
pub(crate) fn lock(file: &File) -> std::io::Result<()> {
    file.lock()
}
pub(crate) fn lock_shared(file: &File) -> std::io::Result<()> {
    file.lock_shared()
}
pub(crate) fn unlock(file: &File) -> std::io::Result<()> {
    file.unlock()
}

pub(crate) fn open_task_lock(path: &Path) -> Result<File> {
    // O_CREAT on an existing lock owned by the previous runtime UID is denied
    // by Linux protected_regular in a shared sticky directory, even for root.
    // Open the same inode without O_CREAT, and create only when it is absent.
    for _ in 0..4 {
        let existing = fs::OpenOptions::new()
            .append(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(path);
        let file = match existing {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                match fs::OpenOptions::new()
                    .append(true)
                    .create_new(true)
                    .mode(0o660)
                    .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
                    .open(path)
                {
                    Ok(file) => file,
                    Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                    Err(error) => return Err(error.into()),
                }
            }
            Err(error) => return Err(error.into()),
        };
        ensure!(
            file.metadata()?.is_file(),
            "task lock is not a regular file"
        );
        return Ok(file);
    }
    anyhow::bail!("task lock changed repeatedly during open")
}

pub(crate) fn task_lock_current(path: &Path, file: &File) -> Result<bool> {
    use std::os::unix::fs::MetadataExt;
    let actual = file.metadata()?;
    let expected = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error.into()),
    };
    Ok(actual.is_file()
        && expected.is_file()
        && actual.dev() == expected.dev()
        && actual.ino() == expected.ino())
}
pub(crate) fn acquire_task_lock(path: &Path) -> Result<Option<File>> {
    for _ in 0..4 {
        let file = open_task_lock(path)?;
        if !try_lock(&file)? {
            return Ok(None);
        }
        if task_lock_current(path, &file)? {
            return Ok(Some(file));
        }
        // Drop the lock and descriptor before retrying the current pathname.
    }
    anyhow::bail!("task lock changed repeatedly during acquisition")
}

pub fn inherited_dispatch_lock(
    config: &Config,
    run: &str,
    fd: Option<i32>,
) -> Result<Option<File>> {
    let Some(fd) = fd else { return Ok(None) };
    ensure!(
        fd >= 3 && (config.offline || unsafe { libc::geteuid() } == 0),
        "invalid dispatch descriptor"
    );
    use std::os::{
        fd::{AsRawFd, FromRawFd},
        unix::fs::MetadataExt,
    };
    let file = unsafe { File::from_raw_fd(fd) };
    let path = config
        .home
        .join("run")
        .join(format!("dispatch-{}.lock", crate::digest(run)));
    let actual = file.metadata()?;
    let expected = fs::symlink_metadata(&path)?;
    ensure!(
        actual.is_file()
            && expected.is_file()
            && actual.dev() == expected.dev()
            && actual.ino() == expected.ino(),
        "dispatch descriptor identity mismatch"
    );
    ensure!(try_lock(&file)?, "dispatch lock is held by another process");
    ensure!(
        task_lock_current(&path, &file)?,
        "dispatch descriptor identity mismatch"
    );
    ensure!(
        unsafe { libc::fcntl(file.as_raw_fd(), libc::F_SETFD, libc::FD_CLOEXEC) } >= 0,
        "cannot protect dispatch descriptor"
    );
    Ok(Some(file))
}

pub struct Lease {
    _file: File,
    path: PathBuf,
}
impl Drop for Lease {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}
pub fn lease(config: &Config, catalog: &Catalog) -> Result<Lease> {
    let dir = config.home.join("run/module-leases");
    fs::create_dir_all(&dir)?;
    let path = dir.join(format!("{}.json", uuid::Uuid::new_v4()));
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)?;
    lock(&file)?;
    use std::io::Write;
    file.write_all(
        serde_json::to_string(
            &catalog
                .modules
                .values()
                .map(|m| m.root.to_string_lossy())
                .collect::<Vec<_>>(),
        )?
        .as_bytes(),
    )?;
    Ok(Lease { _file: file, path })
}
fn snapshot(config: &Config) -> Result<(Catalog, Lease)> {
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(config.home.join("modules/.leases.lock"))?;
    lock_shared(&lock)?;
    let catalog = Catalog::load(config)?;
    let lease = lease(config, &catalog)?;
    unlock(&lock)?;
    Ok((catalog, lease))
}
pub fn environment(
    config: &Config,
    catalog: &Catalog,
    module: &Module,
    workspace: &str,
    bot: &str,
    context: &str,
) -> Result<BTreeMap<String, String>> {
    let settings = config.settings()?;
    let w = settings
        .workspaces
        .get(workspace)
        .context("unknown data workspace")?;
    let mut env = if w.env.is_empty() {
        BTreeMap::new()
    } else {
        crate::config::read_env(&config.contained(&w.env)?)?
    };
    // Runtime environments never inherit bot credentials, gateway state, or a shell profile.
    env.retain(|k, _| {
        !k.starts_with("TELEGRAM_")
            && !k.starts_with("PYLON_")
            && !matches!(
                k.as_str(),
                "PYTHONPATH"
                    | "LD_PRELOAD"
                    | "LD_LIBRARY_PATH"
                    | "DYLD_INSERT_LIBRARIES"
                    | "BASH_ENV"
                    | "ENV"
            )
    });
    let root = config.contained(&w.root)?;
    for (k, v) in [
        (
            "HOME",
            config.home.join("home").to_string_lossy().into_owned(),
        ),
        (
            "PATH",
            format!(
                "{}:/usr/local/bin:/usr/bin:/bin",
                config.home.join("venv/bin").display()
            ),
        ),
        (
            "PYTHONPATH",
            std::iter::once(
                config
                    .home
                    .join("sdk/current/src")
                    .to_string_lossy()
                    .into_owned(),
            )
            .chain(
                catalog
                    .modules
                    .values()
                    .map(|m| m.root.join("src").to_string_lossy().into_owned()),
            )
            .collect::<Vec<_>>()
            .join(":"),
        ),
        ("PYLON_HOME", config.home.to_string_lossy().into_owned()),
        ("PYLON_PROFILE_ROOT", root.to_string_lossy().into_owned()),
        ("PYLON_PROFILE", workspace.into()),
        ("PYLON_WORKSPACE_ID", workspace.into()),
        ("PYLON_BOT_ID", bot.into()),
        ("PYLON_MODULE_ID", module.manifest.id.clone()),
        ("PYLON_MODULE_VERSION", module.hash.clone()),
        (
            "PYLON_OUTBOX",
            config
                .home
                .join("data/outbox.sqlite3")
                .to_string_lossy()
                .into_owned(),
        ),
        ("PYLON_CONTEXT", context.into()),
        (
            "PYLON_GATEWAY_SOCKET",
            config.socket().to_string_lossy().into_owned(),
        ),
        (
            "PYLON_TASK_PYTHON",
            config.python().to_string_lossy().into_owned(),
        ),
        (
            "PYLON_MODULE_SNAPSHOT",
            serde_json::to_string(
                &catalog
                    .modules
                    .iter()
                    .map(|(id, m)| (id, m.root.to_string_lossy()))
                    .collect::<BTreeMap<_, _>>(),
            )?,
        ),
        (
            "PLAYWRIGHT_BROWSERS_PATH",
            config
                .home
                .join("playwright-browsers")
                .to_string_lossy()
                .into_owned(),
        ),
        ("PYTHONUNBUFFERED", "1".into()),
        ("TZ", "Asia/Shanghai".into()),
        ("LANG", "C.UTF-8".into()),
    ] {
        env.insert(k.into(), v);
    }
    Ok(env)
}
fn command(config: &Config, module: &Module, entry: &[String]) -> Result<tokio::process::Command> {
    ensure!(crate::catalog::valid_entry(entry), "invalid entrypoint");
    let (program, args) = match entry[0].as_str() {
        "python" => (
            config.python(),
            std::iter::once("-m".to_owned())
                .chain(entry[1..].iter().cloned())
                .collect::<Vec<_>>(),
        ),
        "node" | "bash" => {
            let path = module.root.join(&entry[1]).canonicalize()?;
            ensure!(path.starts_with(&module.root), "entrypoint escapes module");
            (
                PathBuf::from(if entry[0] == "node" {
                    "/usr/bin/node"
                } else {
                    "/bin/bash"
                }),
                std::iter::once(path.to_string_lossy().into_owned())
                    .chain(entry[2..].iter().cloned())
                    .collect(),
            )
        }
        "native" => {
            let path = module.root.join(&entry[1]).canonicalize()?;
            ensure!(path.starts_with(&module.root), "entrypoint escapes module");
            (path, entry[2..].to_vec())
        }
        _ => unreachable!(),
    };
    let mut cmd = tokio::process::Command::new(program);
    cmd.args(args).kill_on_drop(true).process_group(0);
    Ok(cmd)
}
pub async fn process(
    config: &Config,
    module: &Module,
    entry: &[String],
    env: &BTreeMap<String, String>,
    input: Option<&Value>,
    timeout: Option<u64>,
) -> Result<(i32, String, String)> {
    let identity = execution_identity(config, module)?;
    process_as(config, module, entry, env, input, timeout, identity).await
}
fn execution_identity(config: &Config, module: &Module) -> Result<crate::config::RuntimeUser> {
    if unsafe { libc::geteuid() } == 0 {
        config
            .settings()?
            .runtime_users
            .get(&module.manifest.id)
            .cloned()
            .context("module has no unprivileged runtime identity")
    } else {
        ensure!(
            config.offline,
            "production business execution requires the trusted supervisor"
        );
        Ok(crate::config::RuntimeUser {
            uid: unsafe { libc::geteuid() },
            gid: unsafe { libc::getegid() },
        })
    }
}
async fn process_as(
    config: &Config,
    module: &Module,
    entry: &[String],
    env: &BTreeMap<String, String>,
    input: Option<&Value>,
    timeout: Option<u64>,
    identity: crate::config::RuntimeUser,
) -> Result<(i32, String, String)> {
    let mut cmd = command(config, module, entry)?;
    let home = config.home.canonicalize()?;
    let root = Path::new(&env["PYLON_PROFILE_ROOT"]);
    let relative = root.strip_prefix(&home).context("workspace escapes home")?;
    let checked_root = config.contained(relative.to_str().context("invalid workspace path")?)?;
    ensure!(checked_root == root, "workspace changed before execution");
    if unsafe { libc::geteuid() } == 0 {
        ensure!(
            identity.uid != 0 && identity.gid != 0,
            "business processes cannot run as root"
        );
        crate::child_identity(cmd.as_std_mut(), identity.uid, identity.gid);
    }
    cmd.env_clear()
        .envs(env)
        .current_dir(&checked_root)
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = cmd.spawn().context("could not start business process")?;
    let pid = child.id().context("missing process id")?;
    // Drain both streams concurrently; bounded capture keeps a noisy module from exhausting the gateway.
    type Tail = std::sync::Arc<std::sync::Mutex<Vec<u8>>>;
    async fn capture(
        mut stream: impl tokio::io::AsyncRead + Unpin,
        tail: Tail,
    ) -> std::io::Result<String> {
        let mut bytes = vec![];
        let mut chunk = [0; 8192];
        loop {
            let n = stream.read(&mut chunk).await?;
            if n == 0 {
                break;
            }
            {
                let mut tail = tail
                    .lock()
                    .map_err(|_| std::io::Error::other("output buffer unavailable"))?;
                tail.extend_from_slice(&chunk[..n]);
                if tail.len() > 65536 {
                    let excess = tail.len() - 65536;
                    tail.drain(..excess);
                }
            }
            bytes.extend_from_slice(&chunk[..n]);
            if bytes.len() > 8 * 1024 * 1024 {
                bytes.drain(..bytes.len() - 8 * 1024 * 1024);
            }
        }
        Ok(String::from_utf8_lossy(&bytes).into_owned())
    }
    let stdout_tail = Tail::default();
    let stderr_tail = Tail::default();
    let mut out = tokio::spawn(capture(
        child.stdout.take().context("missing stdout")?,
        stdout_tail.clone(),
    ));
    let mut err = tokio::spawn(capture(
        child.stderr.take().context("missing stderr")?,
        stderr_tail.clone(),
    ));
    let operation = async {
        if let Some(value) = input {
            let mut stdin = child.stdin.take().context("missing process input")?;
            stdin.write_all(value.to_string().as_bytes()).await?;
            stdin.shutdown().await?;
        }
        let status = child.wait().await?;
        Ok::<_, anyhow::Error>((
            status.code().unwrap_or(128),
            (&mut out).await??,
            (&mut err).await??,
        ))
    };
    let result = if let Some(seconds) = timeout {
        match tokio::time::timeout(Duration::from_secs(seconds), operation).await {
            Ok(result) => result,
            Err(_) => Err(anyhow::anyhow!(
                "business process timed out; result requires reconciliation"
            )),
        }
    } else {
        operation.await
    };
    if result.is_err() {
        // Kill the whole process group even when its original leader already exited.
        unsafe { libc::kill(-(pid as i32), libc::SIGTERM) };
        if tokio::time::timeout(Duration::from_secs(2), child.wait())
            .await
            .is_err()
        {
            unsafe { libc::kill(-(pid as i32), libc::SIGKILL) };
            let _ = child.wait().await;
        }
        unsafe { libc::kill(-(pid as i32), libc::SIGKILL) };
        out.abort();
        err.abort();
        let retain = || -> Result<()> {
            let _db = crate::queue::connect(config)?;
            let stdout = stdout_tail
                .lock()
                .map_err(|_| anyhow::anyhow!("output buffer unavailable"))?;
            let stderr = stderr_tail
                .lock()
                .map_err(|_| anyhow::anyhow!("output buffer unavailable"))?;
            crate::config::atomic_json(
                &config
                    .home
                    .join("supervisor")
                    .join(format!("process-last-{}.json", module.manifest.id)),
                &json!({"at":crate::now(),"module":module.manifest.id,"stdout":safe_tail(&stdout),"stderr":safe_tail(&stderr)}),
                0o600,
            )
        };
        if let Err(error) = retain() {
            crate::errors::log("process-diagnostics", &error)
        }
    }
    result
}

pub async fn invoke_local(config: &Config, request: &Value) -> Result<Value> {
    let (catalog, _lease) = snapshot(config)?;
    let module = catalog
        .modules
        .get(request["module"].as_str().unwrap_or(""))
        .context("unknown module")?;
    ensure!(
        !config
            .home
            .join("run/drain")
            .join(&module.manifest.id)
            .exists(),
        "module deployment in progress; request was not started"
    );
    let token = request["context"].as_str().context("context required")?;
    let ctx = api::rpc(config, "context.inspect", json!({}), token).await?;
    ensure!(
        ctx["module"] == module.manifest.id,
        "execution context does not match module"
    );
    let workspace = ctx["workspace"].as_str().context("workspace missing")?;
    let bot = ctx["bot"].as_str().context("bot missing")?;
    let mut env = environment(config, &catalog, module, workspace, bot, token)?;
    if let Some(id) = request["request_id"].as_str() {
        env.insert("PYLON_COMMAND_ID".into(), id.into());
    }
    if let Some(id) = request["reply_id"].as_i64() {
        env.insert("PYLON_REPLY_MESSAGE_ID".into(), id.to_string());
    }
    let input = json!({"api_version":2,"action":request["action"],"profile":workspace,"workspace_id":workspace,"bot_id":bot,"module_version":module.hash,"request_id":request["request_id"],"payload":request["payload"]});
    let (code, stdout, _) = process(
        config,
        module,
        &module.manifest.handler,
        &env,
        Some(&input),
        Some(150),
    )
    .await?;
    ensure!(code == 0, "module handler failed (exit {code})");
    let value: Value = serde_json::from_str(&stdout).context("module returned invalid JSON")?;
    ensure!(
        value["api_version"] == 2
            && value
                .as_object()
                .is_some_and(|v| v.len() == 2 && v.contains_key("value")),
        "invalid module response"
    );
    Ok(value["value"].clone())
}
pub async fn request_executor(config: &Config, request: &Value) -> Result<Value> {
    let client = reqwest::Client::builder()
        .unix_socket(config.home.join("run/executor.sock"))
        .timeout(Duration::from_secs(180))
        .build()?;
    let value: Value = client
        .post("http://localhost/invoke")
        .json(request)
        .send()
        .await
        .context("business executor unavailable")?
        .json()
        .await?;
    ensure!(
        value["ok"] == true,
        "{}",
        value["error"].as_str().unwrap_or("executor failed")
    );
    Ok(value["value"].clone())
}
pub async fn executor(config: Config) -> Result<()> {
    use axum::{Json, Router, extract::State, routing::post};
    async fn handle(State(config): State<Config>, Json(request): Json<Value>) -> Json<Value> {
        let result = crate::isolated(async move {
            if request["kind"] == "job" {
                async {
                    let ctx = api::rpc(
                        &config,
                        "context.inspect",
                        json!({}),
                        request["context"].as_str().unwrap_or(""),
                    )
                    .await?;
                    let id = request["job_id"].as_str().context("task required")?;
                    let catalog = Catalog::load(&config)?;
                    ensure!(
                        catalog.job(id)?.0.manifest.id == ctx["module"],
                        "task context mismatch"
                    );
                    let mut cmd = tokio::process::Command::new(std::env::current_exe()?);
                    cmd.args(["--home", config.home.to_str().context("invalid home path")?]);
                    if config.offline {
                        cmd.arg("--offline");
                    }
                    let mut child = cmd
                        .args(["execute", id])
                        .stdin(Stdio::null())
                        .stdout(Stdio::null())
                        .stderr(Stdio::null())
                        .spawn()?;
                    tokio::spawn(async move {
                        let _ = child.wait().await;
                    });
                    Ok(json!({"accepted":true}))
                }
                .await
            } else {
                invoke_local(&config, &request).await
            }
        })
        .await;
        match result {
            Ok(value) => Json(json!({"ok":true,"value":value})),
            Err(e) => {
                crate::errors::log("executor-request", &e);
                Json(crate::errors::public(&e))
            }
        }
    }
    let path = config.home.join("run/executor.sock");
    fs::create_dir_all(path.parent().context("socket parent missing")?)?;
    if path.exists() {
        ensure!(
            tokio::net::UnixStream::connect(&path).await.is_err(),
            "executor already running"
        );
        fs::remove_file(&path)?;
    }
    let listener = tokio::net::UnixListener::bind(&path)?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o660))?;
    let summary_config = config.clone();
    let summary_task = tokio::spawn(async move {
        let mut tick = tokio::time::interval(Duration::from_secs(2));
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tick.tick().await;
            let summary = summary_config.clone();
            if let Err(error) =
                crate::blocking(move || crate::queue::summary_refresh(&summary)).await
            {
                crate::errors::log("summary-refresh", &error);
            }
        }
    });
    let supervisor = config.clone();
    let dispatch_task = tokio::spawn(async move {
        loop {
            if let Err(error) = crate::queue::dispatch(&supervisor).await {
                crate::errors::log("queue-reconcile", &error);
            }
            tokio::time::sleep(Duration::from_secs(2)).await;
        }
    });
    let closing = config.clone();
    let (maintenance_stop, maintenance_rx) = tokio::sync::watch::channel(false);
    let maintenance_task = tokio::spawn(crate::retention::executor(config.clone(), maintenance_rx));
    let stopping = maintenance_stop.clone();
    let result = axum::serve(
        listener,
        Router::new()
            .route("/invoke", post(handle))
            .with_state(config),
    )
    .with_graceful_shutdown(async move {
        if let Ok(mut signal) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            tokio::select! {_=signal.recv()=>{},_=tokio::signal::ctrl_c()=>{}}
        }
        let _ = stopping.send(true);
        if let Err(error) = crate::blocking(move || crate::queue::summary_flush(&closing)).await {
            crate::errors::log("summary-shutdown", &error)
        }
    })
    .await;
    let _ = maintenance_stop.send(true);
    maintenance_task.abort();
    summary_task.abort();
    dispatch_task.abort();
    result?;
    Ok(())
}
pub async fn execute(config: &Config, id: &str) -> Result<i32> {
    let catalog = Catalog::load(config)?;
    let (module, job) = catalog.job(id)?;
    let trigger = format!(
        "{}:{}",
        id,
        std::env::var("INVOCATION_ID")
            .unwrap_or_else(|_| uuid::Uuid::new_v4().simple().to_string())
    );
    let run = crate::queue::admit(
        config,
        id,
        &module.manifest.id,
        &job.topic,
        job.model,
        &trigger,
    )?;
    let code = resume(config, &run).await?;
    if crate::queue::optional_state(config, &run)?.as_deref() == Some("queued") {
        println!("{}", json!({"task":id,"status":"queued","request_id":run}));
    }
    Ok(code)
}
pub async fn resume(config: &Config, run: &str) -> Result<i32> {
    let Some(_request_lock) = acquire_task_lock(
        &config
            .home
            .join("run")
            .join(format!("request-{}.lock", crate::digest(run))),
    )?
    else {
        return Ok(0);
    };
    if crate::queue::optional_state(config, run)?.as_deref() != Some("queued")
        || config.home.join("run/freeze").exists()
    {
        return Ok(0);
    }
    let db = crate::queue::connect(config)?;
    let id: String = db.query_row("SELECT job_id FROM requests WHERE id=?", [run], |r| {
        r.get(0)
    })?;
    let id = id.as_str();
    let (catalog, _lease) = snapshot(config)?;
    let (module, job) = catalog.job(id)?;
    if config
        .home
        .join("run/drain")
        .join(&module.manifest.id)
        .exists()
    {
        return Ok(0);
    }
    let lock = open_task_lock(&config.home.join("run").join(format!(
        "{}.lock",
        if job.lock.is_empty() { id } else { &job.lock }
    )))?;
    if !try_lock(&lock)? {
        return Ok(0);
    }
    // Preserve FIFO among requests that can acquire their task lock, within a quota group.
    let older = {
        let mut q=db.prepare("SELECT job_id FROM requests WHERE state='queued' AND topic=? AND model=? AND created_at<(SELECT created_at FROM requests WHERE id=?) ORDER BY created_at,id")?;
        q.query_map(rusqlite::params![job.topic, job.model, run], |r| {
            r.get::<_, String>(0)
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?
    };
    for prior in older {
        if let Ok((_, prior_job)) = catalog.job(&prior) {
            let name = if prior_job.lock.is_empty() {
                &prior
            } else {
                &prior_job.lock
            };
            let prior_lock = open_task_lock(&config.home.join("run").join(format!("{name}.lock")))?;
            if try_lock(&prior_lock)? {
                return Ok(0);
            }
        }
    }
    let settings = config.settings()?;
    let maximum = settings
        .groups
        .get(&job.topic)
        .map(|g| if job.model { g.models } else { g.processes })
        .unwrap_or(if job.model { 2 } else { 4 });
    let mut quota = None;
    for slot in 0..maximum {
        let file = open_task_lock(&config.home.join("run").join(format!(
            "quota-{}-{}-{slot}.lock",
            job.topic,
            if job.model { "model" } else { "process" }
        )))?;
        if try_lock(&file)? {
            quota = Some(file);
            break;
        }
    }
    let Some(_quota) = quota else { return Ok(0) };
    // A waiting request may only take a quota slot after older requests in its group.
    // The dispatcher visits creation order; the lock also serializes direct triggers.
    let context = api::rpc(
        config,
        "context.open",
        json!({"module":module.manifest.id,"subject":format!("job:{id}")}),
        "",
    )
    .await?;
    let token = context["token"].as_str().context("context missing")?;
    let identity = execution_identity(config, module)?;
    let output_dir = config.home.join("run/work").join(&module.manifest.id);
    fs::create_dir_all(&output_dir)?;
    if unsafe { libc::geteuid() } == 0 {
        std::os::unix::fs::chown(&output_dir, Some(identity.uid), Some(identity.gid))?;
        fs::set_permissions(&output_dir, fs::Permissions::from_mode(0o700))?;
    }
    let result_file = output_dir.join(format!("{}.result.json", uuid::Uuid::new_v4()));
    let mut env = environment(
        config,
        &catalog,
        module,
        &job.workspace,
        context["bot"].as_str().context("context bot missing")?,
        token,
    )?;
    for (k, v) in &job.env {
        ensure!(
            ![
                "PYLON_CONTEXT",
                "PYLON_BOT_ID",
                "PYLON_MODULE_ID",
                "PYLON_GATEWAY_SOCKET",
                "PYTHONPATH",
                "PATH",
                "HOME"
            ]
            .contains(&k.as_str())
                && !k.starts_with("TELEGRAM_"),
            "protected environment field"
        );
        env.insert(k.clone(), v.clone());
    }
    for (k, v) in [
        ("PYLON_JOB_ID", id.to_owned()),
        ("PYLON_RUN_ID", run.to_owned()),
        (
            "PYLON_RESULT_FILE",
            result_file.to_string_lossy().into_owned(),
        ),
        ("PYLON_TOPIC", job.topic.clone()),
        ("PYLON_TRIGGER", "scheduled".into()),
    ] {
        env.insert(k.into(), v);
    }
    api::rpc(config, "run.start", json!({"id":run}), token).await?;
    db.execute("UPDATE requests SET state='running',token=?,owner=?,updated_at=? WHERE id=? AND state='queued'",rusqlite::params![token,std::process::id(),crate::now(),run])?;
    crate::queue::summary(config)?;
    let output = process_as(
        config,
        module,
        &job.entry,
        &env,
        None,
        job.timeout,
        identity.clone(),
    )
    .await;
    let (status, stdout, stderr) = match output {
        Ok((0, out, err)) => ("ok", out, err),
        Ok((_, out, err)) => ("failed", out, err),
        Err(_) => (
            "uncertain",
            String::new(),
            "任务进程中断或超时，需先核对结果。".into(),
        ),
    };
    let result: Value = match fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(&result_file)
    {
        Ok(mut file) => {
            use std::os::unix::fs::MetadataExt;
            let metadata = file.metadata()?;
            let uid = identity.uid;
            if metadata.is_file() && metadata.uid() == uid && metadata.len() <= 8 * 1024 * 1024 {
                let mut bytes = Vec::new();
                std::io::Read::read_to_end(
                    &mut std::io::Read::take(&mut file, 8 * 1024 * 1024 + 1),
                    &mut bytes,
                )?;
                serde_json::from_slice(&bytes).unwrap_or_else(|_| json!({"status":"uncertain"}))
            } else {
                json!({"status":"uncertain"})
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => json!({}),
        Err(_) => json!({"status":"uncertain"}),
    };
    let payload = json!({"job_id":id,"run_id":run,"job":job,"status":status,"stdout":stdout,"stderr":stderr,"result":result});
    let input = json!({"api_version":2,"action":"runtime.complete","profile":job.workspace,"bot_id":context["bot"],"payload":payload});
    db.execute(
        "UPDATE requests SET state='completing',execution_status=?,completion_input=?,updated_at=? WHERE id=?",
        rusqlite::params![status, input.to_string(), crate::now(), run],
    )?;
    crate::queue::summary(config)?;
    let completed = process_as(
        config,
        module,
        &module.manifest.handler,
        &env,
        Some(&input),
        Some(150),
        identity,
    )
    .await;
    let mut finish = json!({"id":run,"status":"uncertain","execution_status":status,"completion_status":"failed","notification_status":"unknown","detail":"completion response unavailable or invalid; reconcile without replay"});
    if let Ok((0, out, _)) = completed
        && let Ok(response) = serde_json::from_str::<Value>(&out)
    {
        let value = &response["value"];
        if response["api_version"] == 2
            && matches!(
                value["status"].as_str(),
                Some("ok" | "failed" | "uncertain")
            )
        {
            finish["status"] = value["status"].clone();
            // A completion callback cannot turn a failed execution into success.
            if status != "ok" {
                finish["status"] = json!(status);
            }
            finish["completion_status"] = value
                .get("completion_status")
                .cloned()
                .unwrap_or(json!("ok"));
            finish["notification_status"] = value
                .get("notification_status")
                .cloned()
                .unwrap_or(json!("unknown"));
            finish["detail"] = json!(
                if value["hook_error"].as_str().is_some_and(|v| !v.is_empty()) {
                    "completion hook failed; inspect module completion record"
                } else if finish["status"] != "ok" {
                    "task reported failure or uncertainty"
                } else {
                    ""
                }
            );
        }
    }
    if matches!(result["status"].as_str(), Some("failed" | "uncertain")) {
        finish["status"] = result["status"].clone();
        finish["execution_status"] = result["status"].clone();
    }
    let final_status = finish["status"]
        .as_str()
        .context("completion status missing")?
        .to_owned();
    crate::queue::report(config, run, token, &finish)?;
    if let Err(error) = crate::queue::flush(config, run).await {
        crate::errors::log("queue-report", &error);
    }
    crate::queue::summary_flush(config)?;
    if fs::symlink_metadata(&result_file).is_ok() {
        fs::remove_file(result_file)?;
    }
    println!("{}", json!({"task":id,"status":final_status}));
    Ok(if final_status == "ok" { 0 } else { 1 })
}
pub async fn service(config: &Config, id: &str) -> Result<i32> {
    let (module_id, service_id) = id.split_once("--").context("expected module--service")?;
    let (catalog, _lease) = snapshot(config)?;
    let module = catalog.modules.get(module_id).context("unknown module")?;
    let service = module
        .manifest
        .services
        .get(service_id)
        .context("unknown service")?;
    while !service.passive && config.home.join("run/freeze").is_file() {
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
    // Persist the service identity in supervisor-only storage so an independently
    // restarted observer can record a gateway outage without requiring that gateway.
    let _ = crate::queue::connect(config)?;
    let cached = config
        .home
        .join("supervisor")
        .join(format!("service-{module_id}--{service_id}.json"));
    let previous = fs::read(&cached)
        .ok()
        .and_then(|v| serde_json::from_slice::<Value>(&v).ok());
    let opened = api::rpc(
        config,
        "context.open",
        json!({"module":module_id,"subject":format!("service:{service_id}")}),
        "",
    )
    .await;
    let context = match opened {
        Ok(mut value) => {
            value["module_hash"] = json!(module.hash);
            crate::config::atomic_json(&cached, &value, 0o600)?;
            if let Some(old) = &previous
                && let Some(token) = old["token"].as_str()
            {
                let _ = api::rpc(config, "context.close", json!({}), token).await;
            }
            value
        }
        Err(error) => match previous {
            Some(value) if value["module_hash"] == module.hash => value,
            _ => return Err(error),
        },
    };
    let token = context["token"].as_str().context("context missing")?;
    let env = environment(
        config,
        &catalog,
        module,
        &service.workspace,
        context["bot"].as_str().context("context bot missing")?,
        token,
    )?;
    // This context remains valid for this service across disconnected restarts.
    // Its next successful context.open replaces and revokes it.
    let result = process(config, module, &service.entry, &env, None, None).await;
    Ok(result?.0)
}

pub async fn module_exec(config: &Config, id: &str, alias: &str, args: &[String]) -> Result<i32> {
    let (catalog, _lease) = snapshot(config)?;
    let module = catalog.modules.get(id).context("unknown module")?;
    let mut entry = module
        .manifest
        .cli
        .get(alias)
        .context("unknown module command")?
        .clone();
    entry.extend_from_slice(args);
    let context = api::rpc(
        config,
        "context.open",
        json!({"module":id,"subject":format!("cli:{alias}")}),
        "",
    )
    .await?;
    let token = context["token"].as_str().context("context missing")?;
    let env = environment(
        config,
        &catalog,
        module,
        &module.manifest.workspace,
        context["bot"].as_str().context("context bot missing")?,
        token,
    )?;
    let result = process(config, module, &entry, &env, None, None).await;
    let _ = api::rpc(config, "context.close", json!({}), token).await;
    let (code, out, err) = result?;
    print!("{out}");
    eprint!("{err}");
    Ok(code)
}

pub(crate) fn safe_tail(bytes: &[u8]) -> Value {
    // Free-form module output is untrusted. Only known exception names survive redaction.
    let text = String::from_utf8_lossy(bytes);
    let allowed = [
        "TimeoutError",
        "ConnectionError",
        "PermissionError",
        "ValueError",
        "TypeError",
        "RuntimeError",
        "JSONDecodeError",
        "IncompleteRead",
        "Traceback",
        "BrokenPipeError",
        "OSError",
    ];
    let classes: std::collections::BTreeSet<_> = text
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|word| allowed.contains(word))
        .collect();
    json!({"captured_bytes":bytes.len(),"fingerprint":crate::digest(bytes),"error_types":classes,"tail":"[untrusted output redacted]"})
}
