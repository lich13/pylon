use crate::{
    catalog::Catalog,
    config::{Config, Settings},
    db::Store,
};
use anyhow::{Context, Result, ensure};
use axum::{
    Json, Router,
    body::{Body, to_bytes},
    extract::{DefaultBodyLimit, Request, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::post,
};
use serde_json::{Value, json};
use std::{os::unix::fs::PermissionsExt, sync::Arc};
use tokio::sync::RwLock;

#[derive(Clone)]
pub struct Snapshot {
    pub settings: Settings,
    pub catalog: Catalog,
}
#[derive(Clone)]
pub struct App {
    pub config: Config,
    pub store: Store,
    pub snapshot: Arc<RwLock<Snapshot>>,
}
impl App {
    pub fn new(config: Config) -> Result<Self> {
        let settings = config.settings()?;
        config.validate_credentials(&settings)?;
        let catalog = Catalog::load(&config)?;
        let store = Store::new(&config.db())?;
        Ok(Self {
            config,
            store,
            snapshot: Arc::new(RwLock::new(Snapshot { settings, catalog })),
        })
    }
    pub async fn reload(&self) -> Result<()> {
        let config = self.config.clone();
        let (settings, catalog) = crate::blocking(move || {
            let settings = config.settings()?;
            config.validate_credentials(&settings)?;
            Ok((settings, Catalog::load(&config)?))
        })
        .await?;
        ensure!(
            settings.web_port == self.snapshot.read().await.settings.web_port,
            "web port requires restart"
        );
        *self.snapshot.write().await = Snapshot { settings, catalog };
        Ok(())
    }
}
pub async fn rpc(config: &Config, method: &str, params: Value, context: &str) -> Result<Value> {
    let http = reqwest::Client::builder()
        .unix_socket(config.socket())
        .timeout(std::time::Duration::from_secs(180))
        .build()?;
    let response: Value = http
        .post("http://localhost/rpc")
        .json(&json!({"api_version":2,"method":method,"params":params,"context":context}))
        .send()
        .await
        .context("gateway unavailable")?
        .json()
        .await
        .context("invalid gateway response")?;
    ensure!(
        response["ok"] == true,
        "{}",
        response["error"]
            .as_str()
            .unwrap_or("gateway request failed")
    );
    Ok(response["value"].clone())
}
pub async fn dispatch(app: &App, r: &Value) -> Result<Value> {
    if r["api_version"] == 2 && r["method"] == "health" {
        return Ok(
            json!({"version":env!("CARGO_PKG_VERSION"),"api_version":2,"catalog":app.snapshot.read().await.catalog.hash}),
        );
    }
    let app = app.clone();
    let request = r.clone();
    crate::rpc_isolated(async move { dispatch_inner(&app, &request).await }).await
}
async fn dispatch_inner(app: &App, r: &Value) -> Result<Value> {
    ensure!(r["api_version"] == 2, "unsupported API version");
    let method = r["method"].as_str().context("method required")?;
    let p = &r["params"];
    if method == "health" {
        return Ok(
            json!({"version":env!("CARGO_PKG_VERSION"),"api_version":2,"catalog":app.snapshot.read().await.catalog.hash}),
        );
    }
    if method == "diagnostics" && r["context"].as_str().unwrap_or("").is_empty() {
        return diagnostics(app).await;
    }
    if method == "admin.reload" {
        app.reload().await?;
        return Ok(json!({"ok":true}));
    }
    if method == "context.open" {
        let snapshot = app.snapshot.read().await.clone();
        let module = snapshot
            .catalog
            .modules
            .get(p["module"].as_str().unwrap_or(""))
            .context("unknown module")?;
        let subject = p["subject"].as_str().context("subject required")?;
        let (workspace, topic, origin) = if let Some(id) = subject.strip_prefix("job:") {
            let j = module.manifest.jobs.get(id).context("unknown task")?;
            (&j.workspace, &j.topic, subject)
        } else if let Some(id) = subject.strip_prefix("service:") {
            let s = module
                .manifest
                .services
                .get(id)
                .context("unknown service")?;
            (&s.workspace, &s.topic, subject)
        } else if let Some(id) = subject.strip_prefix("cli:") {
            ensure!(
                module.manifest.cli.contains_key(id),
                "unknown module command"
            );
            (
                &module.manifest.workspace,
                module
                    .manifest
                    .notifications
                    .get("default")
                    .context("default topic missing")?,
                subject,
            )
        } else {
            anyhow::bail!("unsupported context subject")
        };
        let bot = snapshot
            .settings
            .routes
            .get(topic)
            .context("topic has no route")?;
        let token = crate::auth::context(&app.store, module, bot, workspace, origin)?;
        return Ok(
            json!({"token":token,"bot":bot,"workspace":workspace,"module_hash":module.hash}),
        );
    }
    let token = r["context"]
        .as_str()
        .context("execution context required")?;
    let ctx = crate::auth::inspect(&app.store, token)?;
    let module = ctx["module"].as_str().context("module missing")?;
    if method.starts_with("auth.") {
        return crate::auth::request(app, &ctx, method, p).await;
    }
    match method {
        "diagnostics" => {
            ensure!(
                ctx["capabilities"]
                    .as_array()
                    .is_some_and(|a| a.iter().any(|v| v == "diagnostics:read")),
                "diagnostics capability required"
            );
            diagnostics(app).await
        }
        "context.close" => {
            crate::auth::revoke(&app.store, token)?;
            Ok(Value::Null)
        }
        "context.inspect" => Ok(ctx),
        "request.forget" => {
            ensure!(
                ctx["capabilities"]
                    .as_array()
                    .is_some_and(|a| a.iter().any(|c| c == "request:forget")),
                "forget capability required"
            );
            let request = p["id"].as_str().context("request id required")?;
            let fields = request.split(':').collect::<Vec<_>>();
            ensure!(
                fields.len() == 3 && fields[0] == "telegram",
                "invalid request identity"
            );
            let snapshot = app.snapshot.read().await.clone();
            ensure!(
                snapshot
                    .settings
                    .bots
                    .get(fields[1])
                    .is_some_and(|b| snapshot
                        .settings
                        .bots
                        .get(ctx["bot"].as_str().unwrap_or(""))
                        .is_some_and(|source| b.owner == source.owner)),
                "owner mismatch"
            );
            let id = fields[2].parse::<i64>()?;
            app.store.connect()?.execute("UPDATE updates SET payload='{}' WHERE profile=? AND update_id=? AND status NOT IN ('pending','running')",rusqlite::params![fields[1],id])?;
            Ok(json!({"ok":true}))
        }
        "delivery.submit" => {
            let snapshot = app.snapshot.read().await.clone();
            let fallback = ctx["bot"].as_str().context("bot missing")?;
            let bot = if p["retain_target"] == true {
                p["profile"]
                    .as_str()
                    .context("existing event target required")?
            } else {
                snapshot
                    .settings
                    .routes
                    .get(p["topic"].as_str().unwrap_or(""))
                    .map(String::as_str)
                    .unwrap_or(fallback)
            };
            ensure!(snapshot.settings.bots.contains_key(bot), "unknown bot");
            ensure!(
                snapshot
                    .settings
                    .bots
                    .get(fallback)
                    .is_some_and(|source| snapshot.settings.bots[bot].owner == source.owner),
                "delivery owner mismatch"
            );
            let bot = bot.to_owned();
            drop(snapshot);
            if p["kind"] == "document" {
                let path = std::path::Path::new(
                    p["payload"]["path"]
                        .as_str()
                        .context("attachment path required")?,
                )
                .canonicalize()?;
                let root = app.config.home.join("data/attachments").canonicalize()?;
                ensure!(
                    path.starts_with(root) && path.is_file(),
                    "attachment outside delivery spool"
                );
            }
            if p["kind"] == "bark" {
                // Reconcile an already persisted event without depending on today's DNS.
                if app.store.has_event(p["id"].as_str().unwrap_or(""))? {
                    return app.store.submit(module, p, &bot);
                }
                crate::bark::validate(
                    p["payload"]["api_base"]
                        .as_str()
                        .context("Bark endpoint missing")?,
                )
                .await?;
            }
            app.store.submit(module, p, &bot)
        }
        "delivery.get" | "delivery.cancel" => {
            let id = p["id"].as_str().context("event id required")?;
            if method == "delivery.cancel" && p["if_missing"] == true {
                // A tombstone closes the race with an in-flight submit whose acknowledgement was lost.
                app.store.connect()?.execute("INSERT OR IGNORE INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,module) VALUES(?,?,'cancelled','text','{}','suppressed',0,0,?,?,?)",rusqlite::params![id,ctx["bot"].as_str().unwrap_or(""),crate::now(),crate::now(),module])?;
            }
            let event = app.store.event(id)?;
            ensure!(event["module"] == module, "event belongs to another module");
            if method == "delivery.cancel" {
                app.store.cancel(module, id)
            } else {
                Ok(event)
            }
        }
        "delivery.lookup" => {
            let bot = ctx["bot"].as_str().context("bot missing")?;
            let db = app.store.connect()?;
            let mut query=db.prepare("SELECT id,payload,receipt FROM events WHERE module=? AND profile=? AND receipt IS NOT NULL AND (json_extract(receipt,'$[#-1].message_id')=? OR json_extract(receipt,'$.message_id')=?) LIMIT 5")?;
            let rows=query.query_map(rusqlite::params![module,bot,p["message_id"].as_i64(),p["message_id"].as_i64()],|r|Ok(json!({"id":r.get::<_,String>(0)?,"payload":serde_json::from_str::<Value>(&r.get::<_,String>(1)?).unwrap_or(Value::Null),"receipt":serde_json::from_str::<Value>(&r.get::<_,String>(2)?).unwrap_or(Value::Null)})))?.collect::<std::result::Result<Vec<_>,_>>()?;
            Ok(json!(rows))
        }
        "delivery.clear_buttons" => {
            let id = p["id"].as_str().context("event required")?;
            let event = app.store.event(id)?;
            ensure!(event["module"] == module, "event belongs to another module");
            let snapshot = app.snapshot.read().await.clone();
            let bot = snapshot
                .settings
                .bots
                .get(event["profile"].as_str().unwrap_or(""))
                .context("bot missing")?;
            ensure!(
                snapshot
                    .settings
                    .bots
                    .get(ctx["bot"].as_str().unwrap_or(""))
                    .is_some_and(|source| source.owner == bot.owner),
                "owner mismatch"
            );
            let tg = crate::telegram::Telegram::with_family(
                &app.config,
                bot,
                &snapshot.settings.telegram_ip_family,
            )?;
            let receipts = event["receipt"]
                .as_array()
                .cloned()
                .unwrap_or_else(|| vec![event["receipt"].clone()]);
            for receipt in receipts {
                ensure!(
                    crate::telegram::numeric_id(&receipt["chat_id"]) == bot.owner,
                    "receipt owner mismatch"
                );
                tg.call("editMessageReplyMarkup",&json!({"chat_id":bot.owner,"message_id":receipt["message_id"],"reply_markup":{"inline_keyboard":[]}}),true).await?;
            }
            Ok(json!({"ok":true}))
        }
        "capability.invoke" => crate::broker::invoke(&app.config, token, p).await,
        "extension.hook" | "extension.invoke" => {
            let snapshot = app.snapshot.read().await.clone();
            let action = p["action"].as_str().context("extension action required")?;
            let source = snapshot
                .catalog
                .modules
                .get(module)
                .context("source module unavailable")?;
            ensure!(
                source
                    .manifest
                    .capabilities
                    .iter()
                    .any(|c| c == &format!("hook:{action}")),
                "extension hook not declared"
            );
            let mut value = Value::Null;
            for target in snapshot.catalog.modules.values().filter(|m| {
                m.manifest.hooks.iter().any(|h| h == action)
                    && (method == "extension.hook" || p["module"] == m.manifest.id)
            }) {
                let bot = ctx["bot"].as_str().context("bot missing")?;
                let context = crate::auth::context(
                    &app.store,
                    target,
                    bot,
                    &target.manifest.workspace,
                    &format!("hook:{action}"),
                )?;
                let result=crate::runtime::request_executor(&app.config,&json!({"module":target.manifest.id,"context":context,"action":action,"payload":p["payload"]})).await;
                crate::auth::revoke(&app.store, &context)?;
                let output = result?;
                if !output.is_null() {
                    value = output;
                    break;
                }
            }
            Ok(value)
        }
        "telegram.download" => {
            ensure!(
                ctx["origin"] == "document",
                "document owner request required"
            );
            let snapshot = app.snapshot.read().await.clone();
            let bot = snapshot
                .settings
                .bots
                .get(ctx["bot"].as_str().unwrap_or(""))
                .context("bot missing")?;
            let tg = crate::telegram::Telegram::with_family(
                &app.config,
                bot,
                &snapshot.settings.telegram_ip_family,
            )?;
            Ok(json!(
                tg.download(p["file_id"].as_str().context("file id required")?)
                    .await?
            ))
        }
        "task.run" => {
            let id = p["job_id"].as_str().context("task id required")?;
            let snapshot = app.snapshot.read().await.clone();
            let (m, _) = snapshot.catalog.job(id)?;
            ensure!(m.manifest.id == module, "task belongs to another module");
            crate::runtime::request_executor(
                &app.config,
                &json!({"kind":"job","job_id":id,"context":token}),
            )
            .await
        }
        "task.status" => {
            let id = p["job_id"].as_str().context("task id required")?;
            let snapshot = app.snapshot.read().await.clone();
            ensure!(
                snapshot.catalog.job(id)?.0.manifest.id == module,
                "task belongs to another module"
            );
            let db = app.store.connect()?;
            let mut s=db.prepare("SELECT id,status,started_at,finished_at FROM runs WHERE job_id=? ORDER BY started_at DESC LIMIT 10")?;
            let rows=s.query_map([id],|r|Ok(json!({"id":r.get::<_,String>(0)?,"status":r.get::<_,String>(1)?,"started_at":r.get::<_,f64>(2)?,"finished_at":r.get::<_,Option<f64>>(3)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
            Ok(json!(rows))
        }
        "run.start" => {
            let id = p["id"].as_str().context("run id required")?;
            let job = ctx["origin"]
                .as_str()
                .and_then(|s| s.strip_prefix("job:"))
                .context("task context required")?;
            app.store.connect()?.execute(
                "INSERT OR IGNORE INTO runs(id,job_id,started_at,finished_at,status,detail) VALUES(?,?,?,NULL,'running',NULL)",
                rusqlite::params![id, job, crate::now()],
            )?;
            Ok(Value::Null)
        }
        "run.finish" => {
            ensure!(
                ctx["origin"]
                    .as_str()
                    .is_some_and(|s| s.starts_with("job:")),
                "task context required"
            );
            ensure!(
                matches!(p["status"].as_str(), Some("ok" | "failed" | "uncertain")),
                "invalid run status"
            );
            let id = p["id"].as_str().context("run id required")?;
            let job = ctx["origin"]
                .as_str()
                .context("task context required")?
                .trim_start_matches("job:");
            let mut db = app.store.connect()?;
            let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
            let (old, finished): (String, Option<f64>) = tx
                .query_row(
                    "SELECT status,finished_at FROM runs WHERE id=? AND job_id=?",
                    rusqlite::params![id, job],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .context("unknown task run")?;
            ensure!(
                finished.is_none() || p["status"] == old,
                "completed run already has a different result"
            );
            if finished.is_none() {
                tx.execute(
                    "UPDATE runs SET status=?,finished_at=?,detail=? WHERE id=? AND job_id=?",
                    rusqlite::params![
                        p["status"].as_str().context("invalid run status")?,
                        crate::now(),
                        p["detail"].as_str().unwrap_or(""),
                        id,
                        job
                    ],
                )?;
                tx.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",rusqlite::params![format!("run-result:{id}"),json!({"execution_status":p["execution_status"],"completion_status":p["completion_status"],"notification_status":p["notification_status"]}).to_string()])?;
            }
            tx.commit()?;
            Ok(Value::Null)
        }
        _ => anyhow::bail!("unknown gateway method"),
    }
}
#[derive(Clone, Copy)]
pub struct Peer(pub u32);
impl
    axum::extract::connect_info::Connected<
        axum::serve::IncomingStream<'_, tokio::net::UnixListener>,
    > for Peer
{
    fn connect_info(stream: axum::serve::IncomingStream<'_, tokio::net::UnixListener>) -> Self {
        Self(stream.io().peer_cred().map(|v| v.uid()).unwrap_or(u32::MAX))
    }
}
async fn handler(
    State(app): State<App>,
    axum::extract::ConnectInfo(peer): axum::extract::ConnectInfo<Peer>,
    Json(r): Json<Value>,
) -> Json<Value> {
    if (matches!(r["method"].as_str(), Some("context.open" | "admin.reload"))
        || (r["method"] == "diagnostics" && r["context"].as_str().unwrap_or("").is_empty()))
        && !app.config.offline
        && peer.0 != 0
    {
        return Json(
            json!({"ok":false,"api_version":2,"error":"request rejected","error_code":"rejected","retryable":false}),
        );
    }
    match dispatch(&app, &r).await {
        Ok(value) => Json(json!({"ok":true,"api_version":2,"value":value})),
        Err(e) => {
            crate::errors::log("rpc", &e);
            let mut result = crate::errors::public(&e);
            result["api_version"] = json!(2);
            Json(result)
        }
    }
}
pub async fn serve_socket(app: App) -> Result<()> {
    let path = app.config.socket();
    std::fs::create_dir_all(path.parent().context("socket parent missing")?)?;
    if path.exists() {
        if tokio::net::UnixStream::connect(&path).await.is_ok() {
            anyhow::bail!("gateway socket already active")
        }
        std::fs::remove_file(&path)?
    }
    let listener = tokio::net::UnixListener::bind(&path)?;
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o660))?;
    axum::serve(
        listener,
        Router::new()
            .route("/rpc", post(handler))
            .layer(DefaultBodyLimit::max(8 * 1024 * 1024))
            .with_state(app)
            .into_make_service_with_connect_info::<Peer>(),
    )
    .await?;
    Ok(())
}
async fn proxy(State(app): State<App>, request: Request) -> Response {
    if app.config.home.join("run/freeze").is_file() {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            "服务正在切换，请稍后重试。",
        )
            .into_response();
    }

    async fn forward(app: App, request: Request) -> Result<Response> {
        let path = request.uri().path();
        let route = path
            .strip_prefix("/pylon/")
            .and_then(|s| s.split('/').next())
            .context("unknown workspace")?;
        let snapshot = app.snapshot.read().await.clone();
        let (module, web) = snapshot
            .catalog
            .modules
            .values()
            .find_map(|m| m.manifest.web.get(route).map(|w| (m, w)))
            .context("unknown workspace")?;
        if app
            .config
            .home
            .join("run/drain")
            .join(&module.manifest.id)
            .exists()
        {
            return Ok((
                StatusCode::SERVICE_UNAVAILABLE,
                "工作区正在切换，请稍后重试。",
            )
                .into_response());
        }
        let url = format!(
            "{}{}",
            web.upstream.trim_end_matches('/'),
            request
                .uri()
                .path_and_query()
                .map(|s| s.as_str())
                .unwrap_or(path)
        );
        let (parts, body) = request.into_parts();
        let body = tokio::time::timeout(
            std::time::Duration::from_secs(30),
            to_bytes(body, 16 * 1024 * 1024),
        )
        .await
        .context("workspace request body timed out")??;
        let client = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(std::time::Duration::from_secs(180))
            .build()?;
        let mut headers = parts.headers;
        for key in [
            "connection",
            "transfer-encoding",
            "upgrade",
            "x-pylon-context",
            "x-forwarded-for",
        ] {
            headers.remove(key);
        }
        drop(snapshot);
        let response = client
            .request(parts.method, url)
            .headers(headers)
            .body(body)
            .send()
            .await?;
        let mut output = Response::builder().status(response.status());
        for (key, value) in response.headers() {
            if !["connection", "transfer-encoding"].contains(&key.as_str()) {
                output = output.header(key, value)
            }
        }
        Ok(output.body(Body::from(response.bytes().await?))?)
    }
    match forward(app, request).await {
        Ok(r) => r,
        Err(_) => (StatusCode::BAD_GATEWAY, "工作区暂时不可用，请稍后重试。").into_response(),
    }
}
pub async fn serve_web(app: App, port: u16) -> Result<()> {
    let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, port)).await?;
    axum::serve(listener, Router::new().fallback(proxy).with_state(app)).await?;
    Ok(())
}

/// Public operational summary. Never expose credentials, message payloads, or context tokens.
pub async fn diagnostics(app: &App) -> Result<Value> {
    let snapshot = app.snapshot.read().await.clone();
    let app = app.clone();
    // Historical queries must not occupy Telegram's async workers or the registry lock.
    tokio::task::spawn_blocking(move || diagnostics_snapshot(&app, &snapshot))
        .await
        .context("diagnostics worker failed")?
}

fn diagnostics_snapshot(app: &App, snapshot: &Snapshot) -> Result<Value> {
    let db = app.store.connect()?;
    let mut q = db.prepare("SELECT profile,status,count(*) FROM events GROUP BY profile,status")?;
    let deliveries=q.query_map([],|r|Ok(json!({"bot":r.get::<_,String>(0)?,"status":r.get::<_,String>(1)?,"count":r.get::<_,u64>(2)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
    let mut q=db.prepare("SELECT id,job_id,status,finished_at FROM runs WHERE status IN ('failed','uncertain') ORDER BY started_at DESC LIMIT 10")?;
    let failures=q.query_map([],|r|Ok(json!({"id":r.get::<_,String>(0)?,"job_id":r.get::<_,String>(1)?,"status":r.get::<_,String>(2)?,"finished_at":r.get::<_,Option<f64>>(3)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
    let mut q=db.prepare("SELECT r.id,r.job_id,r.status,r.finished_at FROM runs r JOIN (SELECT job_id,max(started_at) AS started_at FROM runs GROUP BY job_id) latest ON r.job_id=latest.job_id AND r.started_at=latest.started_at")?;
    let latest=q.query_map([],|r|Ok(json!({"id":r.get::<_,String>(0)?,"job_id":r.get::<_,String>(1)?,"status":r.get::<_,String>(2)?,"finished_at":r.get::<_,Option<f64>>(3)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
    let bots=snapshot.settings.bots.iter().map(|(id,bot)|Ok(json!({"id":id,"enabled":bot.enabled,"connection":app.store.get(&format!("connection:{id}"))?,"menu":app.store.get(&format!("menu-state:{id}"))?}))).collect::<Result<Vec<_>>>()?;
    let tasks = crate::queue::public(&app.config);
    let queued = tasks["requests"]
        .as_array()
        .map(|rows| {
            rows.iter()
                .filter(|r| r["state"] == "queued")
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let oldest_wait = queued
        .iter()
        .filter_map(|r| r["created_at"].as_f64())
        .map(|at| (crate::now() - at).max(0.0))
        .fold(0.0_f64, f64::max);
    let queued_count = tasks["totals"]["queued_count"]
        .as_u64()
        .unwrap_or(queued.len() as u64);
    let oldest_wait = tasks["totals"]
        .get("oldest_queued_at")
        .map(|at| {
            at.as_f64()
                .map(|v| (crate::now() - v).max(0.0))
                .unwrap_or(0.0)
        })
        .unwrap_or(oldest_wait);
    let new_uncertain = app.store.get("uncertain:counter:v2")?["pending"]
        .as_u64()
        .unwrap_or(0);
    let mut q = db.prepare(
        "SELECT key,value FROM kv WHERE key LIKE 'worker:%' OR key LIKE 'delivery-timing:%'",
    )?;
    let worker_metrics = q
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
        .collect::<std::result::Result<Vec<_>, _>>()?
        .into_iter()
        .map(|(k, v)| (k, serde_json::from_str::<Value>(&v).unwrap_or(Value::Null)))
        .collect::<std::collections::BTreeMap<_, _>>();
    let (proxy_invalid, response_invalid) = crate::telegram::invalid_responses();
    let completions_pending = crate::completion::pending_count(&app.store.path)?;
    Ok(
        json!({"api_version":2,"metrics":{"sqlite_busy":crate::db::busy_count(),"telegram_proxy_invalid":proxy_invalid,"telegram_response_invalid":response_invalid,"workers":worker_metrics,"queued_count":queued_count,"oldest_queued_seconds":oldest_wait,"new_uncertain":new_uncertain},"version":env!("CARGO_PKG_VERSION"),"completions_pending":completions_pending,"sampled_at":crate::now(),"maintenance":app.config.home.join("run/freeze").exists(),"bots":bots,"delivery":deliveries,"tasks":crate::queue::public(&app.config),"recent_failures":failures,"latest_runs":latest,"modules":snapshot.catalog.modules.iter().map(|(id,m)|json!({"id":id,"version":m.hash})).collect::<Vec<_>>(),"configuration_rejected":app.store.get("configuration-rejected")?,"runtime":{"os":std::env::consts::OS,"arch":std::env::consts::ARCH,"pid":std::process::id()}}),
    )
}
