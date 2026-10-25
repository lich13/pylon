use crate::{config::Config, db::Store};
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::Path,
    process::Stdio,
    time::Duration,
};

fn trusted(path: &Path) -> Result<()> {
    let p = path.canonicalize()?;
    let mut current = Some(p.as_path());
    while let Some(p) = current {
        let m = std::fs::metadata(p)?;
        ensure!(
            m.uid() == 0 && m.mode() & 0o022 == 0,
            "helper path is not exclusively root-owned"
        );
        current = p.parent();
    }
    Ok(())
}
pub async fn invoke(config: &Config, token: &str, params: &Value) -> Result<Value> {
    let client = reqwest::Client::builder()
        .unix_socket(config.home.join("run/broker.sock"))
        .timeout(Duration::from_secs(180))
        .build()?;
    crate::errors::rpc_response(
        client
            .post("http://localhost/capability")
            .json(&json!({"context":token,"request":params}))
            .send()
            .await,
    )
    .await
}
pub async fn dispatch(config: &Config, request: &Value) -> Result<Value> {
    ensure!(unsafe { libc::geteuid() } == 0, "broker requires root");
    let path = config.home.join("config/capabilities.json");
    trusted(&path)?;
    let grants: Value = serde_json::from_slice(&std::fs::read(path)?)?;
    let token = request["context"].as_str().context("context required")?;
    let ctx = crate::auth::inspect(&Store::new(&config.db())?, token)?;
    let name = request["request"]["capability"]
        .as_str()
        .context("capability required")?;
    let grant = grants.get(name).context("capability is not installed")?;
    ensure!(
        ctx["capabilities"]
            .as_array()
            .is_some_and(|a| a.iter().any(|v| v == name)),
        "capability not declared"
    );
    ensure!(
        grant["modules"]
            .as_array()
            .is_some_and(|a| a.contains(&ctx["module"])),
        "capability not granted to module"
    );
    ensure!(
        grant["origins"]
            .as_array()
            .is_some_and(|a| a.contains(&ctx["origin"])),
        "capability not granted to this execution origin"
    );
    let args = grant["argv"]
        .as_array()
        .context("helper entry missing")?
        .iter()
        .map(|s| s.as_str().context("invalid helper argument"))
        .collect::<Result<Vec<_>>>()?;
    ensure!(!args.is_empty(), "empty helper command");
    trusted(Path::new(args[0]))?;
    // Any helper source and dependency roots are separately approved and immutable to runtime users.
    for path in grant["trusted_paths"]
        .as_array()
        .context("helper source ownership required")?
    {
        trusted(Path::new(path.as_str().context("invalid trusted path")?))?;
    }
    let input = json!({"api_version":2,"capability":name,"module":ctx["module"],"bot":ctx["bot"],"workspace":ctx["workspace"],"origin":ctx["origin"],"params":request["request"]["params"]});
    let mut command = tokio::process::Command::new(args[0]);
    command
        .args(&args[1..])
        .env_clear()
        .env(
            "PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        )
        .env("HOME", "/root")
        .env("PYLON_HOME", &config.home)
        .env("LANG", "C.UTF-8");
    run_helper(
        &mut command,
        serde_json::to_vec(&input)?,
        Duration::from_secs(grant["timeout"].as_u64().unwrap_or(150)),
    )
    .await
}

async fn run_helper(
    command: &mut tokio::process::Command,
    input: Vec<u8>,
    timeout: Duration,
) -> Result<Value> {
    use crate::errors::ReconcileRequired;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    const OUTPUT_LIMIT: u64 = 8 * 1024 * 1024;

    // Cancellation must also stop descendants that inherited the pipes.
    struct Group(u32);
    impl Drop for Group {
        fn drop(&mut self) {
            if self.0 != 0 {
                unsafe { libc::kill(-(self.0 as i32), libc::SIGKILL) };
            }
        }
    }
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .process_group(0)
        .spawn()?;
    let mut group = Group(child.id().context(ReconcileRequired)?);
    let mut stdin = child.stdin.take().context(ReconcileRequired)?;
    let stdout = child.stdout.take().context(ReconcileRequired)?;
    let operation = async {
        let (_, bytes, status) = tokio::try_join!(
            async move {
                stdin.write_all(&input).await.context(ReconcileRequired)?;
                stdin.shutdown().await.context(ReconcileRequired)
            },
            async move {
                let mut bytes = Vec::new();
                stdout
                    .take(OUTPUT_LIMIT + 1)
                    .read_to_end(&mut bytes)
                    .await
                    .context(ReconcileRequired)?;
                ensure!(bytes.len() as u64 <= OUTPUT_LIMIT, ReconcileRequired);
                Ok(bytes)
            },
            async { child.wait().await.context(ReconcileRequired) },
        )?;
        ensure!(
            status.success(),
            "privileged helper rejected or failed; inspect retained state"
        );
        serde_json::from_slice(&bytes).context(ReconcileRequired)
    };
    let result = tokio::time::timeout(timeout, operation)
        .await
        .context(ReconcileRequired)
        .and_then(|result| result);
    if result.is_err() {
        unsafe { libc::kill(-(group.0 as i32), libc::SIGTERM) };
        let _ = tokio::time::timeout(Duration::from_secs(2), child.wait()).await;
        unsafe { libc::kill(-(group.0 as i32), libc::SIGKILL) };
        let _ = child.wait().await;
    }
    group.0 = 0;
    result
}
pub async fn serve(config: Config) -> Result<()> {
    use axum::{Json, Router, extract::State, routing::post};
    ensure!(unsafe { libc::geteuid() } == 0, "broker requires root");
    async fn handle(State(config): State<Config>, Json(request): Json<Value>) -> Json<Value> {
        match dispatch(&config, &request).await {
            Ok(value) => Json(json!({"ok":true,"value":value})),
            Err(e) => {
                crate::errors::log("broker", &e);
                Json(crate::errors::public(&e))
            }
        }
    }
    let path = config.home.join("run/broker.sock");
    if path.exists() {
        ensure!(
            tokio::net::UnixStream::connect(&path).await.is_err(),
            "broker already running"
        );
        std::fs::remove_file(&path)?;
    }
    let listener = tokio::net::UnixListener::bind(&path)?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o660))?;
    axum::serve(
        listener,
        Router::new()
            .route("/capability", post(handle))
            .with_state(config),
    )
    .await?;
    Ok(())
}

#[cfg(test)]
#[path = "broker_tests.rs"]
mod tests;
