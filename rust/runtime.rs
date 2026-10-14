use crate::{
    api,
    catalog::{Catalog, Module},
    config::Config,
};
use anyhow::{Context, Result, ensure};
use fs2::FileExt;
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

fn open_task_lock(path: &Path) -> Result<File> {
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
    file.lock_exclusive()?;
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
    FileExt::lock_shared(&lock)?;
    let catalog = Catalog::load(config)?;
    let lease = lease(config, &catalog)?;
    FileExt::unlock(&lock)?;
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
    let mut cmd = command(config, module, entry)?;
    if unsafe { libc::geteuid() } == 0 {
        let settings = config.settings()?;
        let user = settings
            .runtime_users
            .get(&module.manifest.id)
            .context("module has no unprivileged runtime identity")?;
        ensure!(
            user.uid != 0 && user.gid != 0,
            "business processes cannot run as root"
        );
        crate::child_identity(cmd.as_std_mut(), user.uid, user.gid);
    } else {
        ensure!(
            config.offline,
            "production business execution requires the trusted supervisor"
        )
    }
    cmd.env_clear()
        .envs(env)
        .current_dir(&env["PYLON_PROFILE_ROOT"])
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = cmd.spawn().context("could not start business process")?;
    let pid = child.id().context("missing process id")?;
    if let Some(value) = input {
        let mut stdin = child.stdin.take().context("missing process input")?;
        stdin.write_all(value.to_string().as_bytes()).await?;
        stdin.shutdown().await?;
    }
    // Drain both streams concurrently; bounded capture keeps a noisy module from exhausting the gateway.
    async fn capture(mut stream: impl tokio::io::AsyncRead + Unpin) -> std::io::Result<String> {
        let mut bytes = vec![];
        let mut chunk = [0; 8192];
        loop {
            let n = stream.read(&mut chunk).await?;
            if n == 0 {
                break;
            }
            bytes.extend_from_slice(&chunk[..n]);
            if bytes.len() > 8 * 1024 * 1024 {
                bytes.drain(..bytes.len() - 8 * 1024 * 1024);
            }
        }
        Ok(String::from_utf8_lossy(&bytes).into_owned())
    }
    let out = tokio::spawn(capture(child.stdout.take().unwrap()));
    let err = tokio::spawn(capture(child.stderr.take().unwrap()));
    let status = if let Some(seconds) = timeout {
        match tokio::time::timeout(Duration::from_secs(seconds), child.wait()).await {
            Ok(status) => status?,
            Err(_) => {
                unsafe { libc::kill(-(pid as i32), libc::SIGTERM) };
                if tokio::time::timeout(Duration::from_secs(15), child.wait())
                    .await
                    .is_err()
                {
                    unsafe { libc::kill(-(pid as i32), libc::SIGKILL) };
                    child.wait().await?;
                }
                out.abort();
                err.abort();
                anyhow::bail!("business process timed out; result requires reconciliation")
            }
        }
    } else {
        child.wait().await?
    };
    Ok((status.code().unwrap_or(128), out.await??, err.await??))
}
pub async fn invoke_local(config: &Config, request: &Value) -> Result<Value> {
    let (catalog, _lease) = snapshot(config)?;
    let module = catalog
        .modules
        .get(request["module"].as_str().unwrap_or(""))
        .context("unknown module")?;
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
        let result = if request["kind"] == "job" {
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
                cmd.args(["--home", config.home.to_str().unwrap()]);
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
        };
        match result {
            Ok(value) => Json(json!({"ok":true,"value":value})),
            Err(e) => Json(json!({"ok":false,"error":e.to_string()})),
        }
    }
    let path = config.home.join("run/executor.sock");
    fs::create_dir_all(path.parent().unwrap())?;
    if path.exists() {
        ensure!(
            tokio::net::UnixStream::connect(&path).await.is_err(),
            "executor already running"
        );
        fs::remove_file(&path)?;
    }
    let listener = tokio::net::UnixListener::bind(&path)?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o660))?;
    axum::serve(
        listener,
        Router::new()
            .route("/invoke", post(handle))
            .with_state(config),
    )
    .await?;
    Ok(())
}
pub async fn execute(config: &Config, id: &str) -> Result<i32> {
    if config.home.join("run/freeze").exists() {
        return Ok(0);
    }
    let (catalog, _lease) = snapshot(config)?;
    let (module, job) = catalog.job(id)?;
    fs::create_dir_all(config.home.join("run"))?;
    let lock = open_task_lock(&config.home.join("run").join(format!(
        "{}.lock",
        if job.lock.is_empty() { id } else { &job.lock }
    )))?;
    if lock.try_lock_exclusive().is_err() {
        return Ok(0);
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
        if file.try_lock_exclusive().is_ok() {
            quota = Some(file);
            break;
        }
    }
    let Some(_quota) = quota else { return Ok(0) };
    let context = api::rpc(
        config,
        "context.open",
        json!({"module":module.manifest.id,"subject":format!("job:{id}")}),
        "",
    )
    .await?;
    let token = context["token"].as_str().context("context missing")?;
    let run = format!(
        "{}:{}",
        id,
        std::env::var("INVOCATION_ID")
            .unwrap_or_else(|_| uuid::Uuid::new_v4().simple().to_string())
    );
    // An invocation marker is durable before spawning; the same systemd invocation never replays.
    let marker = config
        .home
        .join("run/invocations")
        .join(crate::digest(&run));
    fs::create_dir_all(marker.parent().unwrap())?;
    if marker.exists() {
        let _ = api::rpc(config, "context.close", json!({}), token).await;
        return Ok(0);
    }
    fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&marker)?
        .sync_all()?;
    let output_dir = config.home.join("run/work").join(&module.manifest.id);
    fs::create_dir_all(&output_dir)?;
    if unsafe { libc::geteuid() } == 0 {
        let users = config.settings()?;
        let user = users
            .runtime_users
            .get(&module.manifest.id)
            .context("runtime identity missing")?;
        std::os::unix::fs::chown(&output_dir, Some(user.uid), Some(user.gid))?;
        fs::set_permissions(&output_dir, fs::Permissions::from_mode(0o700))?;
    }
    let result_file = output_dir.join(format!("{}.result.json", uuid::Uuid::new_v4()));
    let mut env = environment(
        config,
        &catalog,
        module,
        &job.workspace,
        context["bot"].as_str().unwrap(),
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
        ("PYLON_RUN_ID", run.clone()),
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
    let output = process(config, module, &job.entry, &env, None, job.timeout).await;
    let (status, stdout, stderr) = match output {
        Ok((0, out, err)) => ("ok", out, err),
        Ok((_, out, err)) => ("failed", out, err),
        Err(_) => (
            "uncertain",
            String::new(),
            "任务进程中断或超时，需先核对结果。".into(),
        ),
    };
    let result: Value = if result_file.is_file() {
        serde_json::from_slice(&fs::read(&result_file)?)
            .unwrap_or_else(|_| json!({"status":"uncertain"}))
    } else {
        json!({})
    };
    let payload = json!({"job_id":id,"run_id":run,"job":job,"status":status,"stdout":stdout,"stderr":stderr,"result":result});
    let input = json!({"api_version":2,"action":"runtime.complete","profile":job.workspace,"bot_id":context["bot"],"payload":payload});
    let completed = process(
        config,
        module,
        &module.manifest.handler,
        &env,
        Some(&input),
        Some(150),
    )
    .await;
    let final_status = if completed.as_ref().is_ok_and(|(code, _, _)| *code == 0) {
        status
    } else {
        "uncertain"
    };
    api::rpc(config,"run.finish",json!({"id":run,"status":final_status,"detail":if final_status=="uncertain"{"task or completion requires reconciliation"}else{""}}),token).await?;
    let _ = api::rpc(config, "context.close", json!({}), token).await;
    if result_file.exists() {
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
    let context = api::rpc(
        config,
        "context.open",
        json!({"module":module_id,"subject":format!("service:{service_id}")}),
        "",
    )
    .await?;
    let token = context["token"].as_str().context("context missing")?;
    let env = environment(
        config,
        &catalog,
        module,
        &service.workspace,
        context["bot"].as_str().unwrap(),
        token,
    )?;
    let result = process(config, module, &service.entry, &env, None, None).await;
    let _ = api::rpc(config, "context.close", json!({}), token).await;
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
        context["bot"].as_str().unwrap(),
        token,
    )?;
    let result = process(config, module, &entry, &env, None, None).await;
    let _ = api::rpc(config, "context.close", json!({}), token).await;
    let (code, out, err) = result?;
    print!("{out}");
    eprint!("{err}");
    Ok(code)
}
