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
    let result: Value = client
        .post("http://localhost/capability")
        .json(&json!({"context":token,"request":params}))
        .send()
        .await
        .context("privileged broker unavailable")?
        .json()
        .await?;
    ensure!(
        result["ok"] == true,
        "{}",
        result["error"].as_str().unwrap_or("capability rejected")
    );
    Ok(result["value"].clone())
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
    let mut child = tokio::process::Command::new(args[0])
        .args(&args[1..])
        .env_clear()
        .env(
            "PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        )
        .env("HOME", "/root")
        .env("PYLON_HOME", &config.home)
        .env("LANG", "C.UTF-8")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()?;
    use tokio::io::AsyncWriteExt;
    let mut stdin = child.stdin.take().context("helper input missing")?;
    stdin.write_all(input.to_string().as_bytes()).await?;
    stdin.shutdown().await?;
    drop(stdin);
    let output = tokio::time::timeout(
        Duration::from_secs(grant["timeout"].as_u64().unwrap_or(150)),
        child.wait_with_output(),
    )
    .await
    .context("privileged result uncertain; inspect state before retry")??;
    ensure!(
        output.status.success(),
        "privileged helper rejected or failed; inspect retained state"
    );
    serde_json::from_slice(&output.stdout).context("invalid helper result; inspect retained state")
}
pub async fn serve(config: Config) -> Result<()> {
    use axum::{Json, Router, extract::State, routing::post};
    ensure!(unsafe { libc::geteuid() } == 0, "broker requires root");
    async fn handle(State(config): State<Config>, Json(request): Json<Value>) -> Json<Value> {
        match dispatch(&config, &request).await {
            Ok(value) => Json(json!({"ok":true,"value":value})),
            Err(e) => Json(json!({"ok":false,"error":e.to_string()})),
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
