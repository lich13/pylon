use crate::{
    api::{App, Snapshot},
    catalog::Module,
    config::Bot,
    telegram::{Telegram, authorized},
};
use anyhow::{Context, Result, ensure};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};
use std::{collections::BTreeMap, time::Duration};
use tokio::sync::watch;

/// Unlike a bare JoinHandle, dropping this owner cancels its async child.
struct Child<T>(tokio::task::JoinHandle<T>);
impl<T> std::ops::Deref for Child<T> {
    type Target = tokio::task::JoinHandle<T>;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}
impl<T> std::future::Future for Child<T> {
    type Output = Result<T, tokio::task::JoinError>;
    fn poll(
        mut self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Self::Output> {
        std::pin::Pin::new(&mut self.0).poll(cx)
    }
}
impl<T> Drop for Child<T> {
    fn drop(&mut self) {
        self.0.abort();
    }
}

pub fn menu(snapshot: &Snapshot, bot: &str) -> Vec<Value> {
    let mut commands = snapshot
        .catalog
        .modules
        .values()
        .flat_map(|m| m.manifest.commands.iter())
        .filter(|c| {
            c.menu
                && snapshot
                    .settings
                    .routes
                    .get(&c.topic)
                    .is_some_and(|id| id == bot)
        })
        .collect::<Vec<_>>();
    commands.sort_by_key(|c| (c.order, &c.name));
    let mut result = commands
        .into_iter()
        .map(|c| json!({"command":c.name,"description":c.description}))
        .collect::<Vec<_>>();
    result.push(json!({"command":"help","description":"查看功能与使用说明"}));
    result
}
async fn synchronize(app: &App, id: &str, bot: &Bot, tg: &Telegram) -> Result<()> {
    let wanted = menu(&*app.snapshot.read().await, id);
    let hash = crate::digest(serde_json::to_vec(&wanted)?);
    let key = format!("menu:{id}");
    if app.store.blocking(move |store| store.get(&key)).await? == hash {
        return Ok(());
    }
    for scope in [
        json!({"type":"default"}),
        json!({"type":"all_private_chats"}),
        json!({"type":"chat","chat_id":bot.owner}),
    ] {
        for language in ["", "zh", "en"] {
            let query = json!({"scope":scope,"language_code":language});
            if tg.call("getMyCommands", &query, true).await? != json!(wanted) {
                let mut update = query.clone();
                update["commands"] = json!(wanted);
                tg.call("setMyCommands", &update, true).await?;
                anyhow::ensure!(
                    tg.call("getMyCommands", &query, true).await? == json!(wanted),
                    "menu verification failed"
                );
            }
        }
    }
    for mut query in [json!({}), json!({"chat_id":bot.owner})] {
        if tg.call("getChatMenuButton", &query, true).await? != json!({"type":"commands"}) {
            query["menu_button"] = json!({"type":"commands"});
            tg.call("setChatMenuButton", &query, true).await?;
        }
    }
    let key = format!("menu:{id}");
    app.store
        .blocking(move |store| store.put(&key, &json!(hash)))
        .await?;
    Ok(())
}
pub fn save_updates(app: &App, id: &str, bot: &Bot, updates: &[Value]) -> Result<()> {
    let not_before = app
        .store
        .get("commands_not_before")?
        .as_f64()
        .unwrap_or(0.0);
    let mut db = app.store.connect()?;
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    for update in updates {
        let uid = update["update_id"].as_i64().context("invalid update")?;
        let callback = &update["callback_query"];
        let message = &update["message"];
        let row = if authorized(&bot.owner, message)
            && message["date"].as_f64().unwrap_or(not_before) >= not_before
        {
            Some((
                if message["document"].is_object() {
                    "document"
                } else {
                    "message"
                },
                message,
            ))
        } else if callback["data"].is_string()
            && authorized(
                &bot.owner,
                &json!({"from":callback["from"],"chat":callback["message"]["chat"]}),
            )
        {
            Some(("callback_query", callback))
        } else {
            None
        };
        if let Some((kind, payload)) = row {
            tx.execute("INSERT INTO kv VALUES(?, 'true') ON CONFLICT(key) DO UPDATE SET value=excluded.value",[format!("bot_started:{id}")])?;
            let message = if kind == "callback_query" {
                &payload["message"]
            } else {
                payload
            };
            tx.execute("INSERT OR IGNORE INTO updates(profile,update_id,payload,kind,chat_id,message_id) VALUES(?,?,?,?,?,?)",params![id,uid,payload.to_string(),kind,bot.owner,message["message_id"].as_i64()])?;
        }
    }
    if let Some(offset) = updates.iter().filter_map(|u| u["update_id"].as_i64()).max() {
        tx.execute(
            "INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            params![format!("offset:{id}"), (offset + 1).to_string()],
        )?;
    }
    tx.commit()?;
    Ok(())
}
async fn invoke(
    app: &App,
    contexts: &mut Vec<String>,
    module: &Module,
    bot: &str,
    action: &str,
    payload: Value,
    request: (&str, Option<i64>),
) -> Result<Value> {
    let (request_id, reply_id) = request;
    let origin = if matches!(action, "text" | "start" | "before_command") {
        "command"
    } else {
        action
    };
    let (owned_module, owned_bot, owned_origin) =
        (module.clone(), bot.to_owned(), origin.to_owned());
    let token = app
        .store
        .blocking(move |store| {
            crate::auth::context(
                &store,
                &owned_module,
                &owned_bot,
                &owned_module.manifest.workspace,
                &owned_origin,
            )
        })
        .await?;
    contexts.push(token.clone());
    let result=crate::runtime::request_executor(&app.config,&json!({"module":module.manifest.id,"context":token,"action":action,"payload":payload,"request_id":request_id,"reply_id":reply_id})).await;
    result.map(|v| {
        let mut v = card(v);
        if v.is_object() {
            v["_module"] = json!(module.manifest.id)
        }
        v
    })
}
fn card(value: Value) -> Value {
    if let Some(text) = value.as_str() {
        if let Some(encoded) = text.strip_prefix("PYLON_REPLY_JSON:")
            && let Ok(v) = serde_json::from_str::<Value>(encoded)
        {
            return v;
        }
        return json!({"text":text});
    }
    value
}
async fn route(
    app: &App,
    contexts: &mut Vec<String>,
    id: &str,
    kind: &str,
    message: &Value,
    request_id: &str,
) -> Result<Value> {
    let snapshot = app.snapshot.read().await.clone();
    let reply_id = message["reply_to_message"]["message_id"].as_i64();
    if kind == "callback_query" {
        let data = message["data"].as_str().unwrap_or("");
        let module = snapshot
            .catalog
            .modules
            .values()
            .find(|m| m.manifest.callbacks.iter().any(|p| data.starts_with(p)))
            .context("此按钮已停用，请使用 /help 打开当前入口")?;
        return invoke(
            app,
            contexts,
            module,
            id,
            "callback",
            json!({"query":message}),
            (request_id, message["message"]["message_id"].as_i64()),
        )
        .await
        .map(card);
    }
    if kind == "document" {
        let filename = message["document"]["file_name"].as_str().unwrap_or("");
        let caption = message["caption"]
            .as_str()
            .unwrap_or("")
            .split_whitespace()
            .next()
            .unwrap_or("");
        let module = snapshot
            .catalog
            .modules
            .values()
            .find(|m| {
                m.manifest
                    .documents
                    .iter()
                    .any(|d| d["filename"] == filename || d["caption"] == caption)
            })
            .context("未注册此文件的处理入口")?;
        return invoke(
            app,
            contexts,
            module,
            id,
            "document",
            json!({"message":message}),
            (request_id, reply_id),
        )
        .await
        .map(card);
    }
    let text = message["text"].as_str().unwrap_or("");
    if let Some(command) = text.strip_prefix('/') {
        let mut pieces = command.splitn(2, char::is_whitespace);
        let name = pieces.next().unwrap_or("").split('@').next().unwrap_or("");
        let argument = pieces.next().unwrap_or("").trim();
        if name == "help" || name == "start" {
            if name == "start" {
                for module in snapshot
                    .catalog
                    .modules
                    .values()
                    .filter(|m| m.manifest.hooks.iter().any(|s| s == "start"))
                {
                    let value = invoke(
                        app,
                        contexts,
                        module,
                        id,
                        "start",
                        json!({"argument":argument}),
                        (request_id, reply_id),
                    )
                    .await?;
                    if !value.is_null() {
                        return Ok(card(value));
                    }
                }
            }
            if !argument.is_empty()
                && let Some((_, c)) = snapshot.catalog.command(argument.trim_start_matches('/'))
            {
                return Ok(
                    json!({"text":format!("{}\n\n/{} {}\n{}\n{}",c.description,c.name,c.argument,c.detail,c.example)}),
                );
            }
            return Ok(
                json!({"text":format!("Pylon\n\n{}\n\n详细说明：/help 命令",menu(&snapshot,id).iter().map(|c|format!("/{} — {}",c["command"].as_str().unwrap(),c["description"].as_str().unwrap())).collect::<Vec<_>>().join("\n"))}),
            );
        }
        for hook in snapshot
            .catalog
            .modules
            .values()
            .filter(|m| m.manifest.hooks.iter().any(|s| s == "before_command"))
        {
            invoke(
                app,
                contexts,
                hook,
                id,
                "before_command",
                json!({"name":name,"argument":argument}),
                (request_id, reply_id),
            )
            .await?;
        }
        let Some((module, command)) = snapshot.catalog.command(name) else {
            return Ok(json!({"text":"该入口已停用，请使用 /help 查看当前功能。"}));
        };
        if !command.shared
            && let Some(target) = snapshot.settings.routes.get(&command.topic)
            && target != id
        {
            let bot = snapshot
                .settings
                .bots
                .get(target)
                .context("route bot missing")?;
            return Ok(
                json!({"text":format!("此功能已迁至 @{}，请在那里使用 /{}。",bot.username,command.name)}),
            );
        }
        invoke(
            app,
            contexts,
            module,
            id,
            "command",
            json!({"name":name,"argument":argument,"command_action":command.action}),
            (request_id, reply_id),
        )
        .await
        .map(card)
    } else {
        for module in snapshot
            .catalog
            .modules
            .values()
            .filter(|m| m.manifest.hooks.iter().any(|s| s == "text"))
        {
            let value = invoke(
                app,
                contexts,
                module,
                id,
                "text",
                json!({"text":text,"message":message,"reply_id":reply_id}),
                (request_id, reply_id),
            )
            .await?;
            if !value.is_null() {
                return Ok(card(value));
            }
        }
        Ok(json!({"text":"请使用 /help 选择功能；补充任务信息时请回复原任务消息。"}))
    }
}
pub async fn command_once(app: &App, id: &str, tg: &Telegram) -> Result<bool> {
    let (_sender, mut stop) = watch::channel(false);
    command_once_with_stop(app, id, tg, &mut stop).await
}

async fn command_once_with_stop(
    app: &App,
    id: &str,
    tg: &Telegram,
    stop: &mut watch::Receiver<bool>,
) -> Result<bool> {
    let bot_id = id.to_owned();
    let row = app.store.blocking(move |store| {
        let id = bot_id.as_str();
        let mut db = store.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let row:Option<(i64,String,String)>=tx.query_row("SELECT update_id,kind,payload FROM updates WHERE profile=? AND status='pending' ORDER BY update_id LIMIT 1",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
        if let Some((uid, _, _)) = &row {
            tx.execute(
                "UPDATE updates SET status='running' WHERE profile=? AND update_id=?",
                params![id, uid],
            )?;
        }
        tx.commit()?;
        Ok(row)
    }).await?;
    let Some((uid, kind, payload)) = row else {
        return Ok(false);
    };
    let initial = crate::completion::Completion::command(
        id,
        uid,
        &payload,
        "gateway",
        Value::Null,
        "uncertain",
        &[],
    );
    let _hold = crate::completion::Hold::new(&app.store, &initial, stop)
        .await
        .context(crate::errors::ReconcileRequired)?;
    crate::completion::stage(&app.store, initial)
        .await
        .context(crate::errors::ReconcileRequired)?;
    let message: Value = serde_json::from_str(&payload).unwrap_or(Value::Null);
    if kind == "callback_query" && !message.is_null() {
        let query = json!({"callback_query_id":message["id"]});
        tokio::select! {
            _ = stop.changed() => {},
            _ = tg.call(
                "answerCallbackQuery",
                &query,
                true,
            ) => {},
        }
    }
    let mut contexts = Vec::new();
    let result = if *stop.borrow() {
        Err(crate::errors::ReconcileRequired.into())
    } else if message.is_null() {
        Err(anyhow::anyhow!("stored update is invalid"))
    } else {
        let request_id = format!("telegram:{id}:{uid}");
        let result = tokio::select! {
            biased;
            value = route(app, &mut contexts, id, &kind, &message, &request_id) => value,
            _ = stop.changed() => Err(crate::errors::ReconcileRequired.into()),
        };
        // A disconnected executor request can still be running independently.
        if result
            .as_ref()
            .is_err_and(|e| e.is::<crate::errors::ReconcileRequired>())
        {
            contexts.clear();
        }
        result
    };
    let (mut reply, status) = match result {
        Ok(value) => (value, "done"),
        Err(error) => {
            crate::errors::log("command-execution", &error);
            (
                json!({"text":"操作未完成或结果待核对。请打开对应入口查看记录；没有自动重复执行。"}),
                "uncertain",
            )
        }
    };
    let owner = reply
        .as_object_mut()
        .and_then(|v| v.remove("_module"))
        .and_then(|v| v.as_str().map(str::to_owned))
        .unwrap_or_else(|| "gateway".into());
    if owner == "gateway" && status == "uncertain" {
        reply["expires_at"] = json!(crate::now() + 600.0);
    }
    let result =
        crate::completion::Completion::command(id, uid, &payload, &owner, reply, status, &contexts);
    crate::completion::persist(&app.store, result, stop)
        .await
        .context(crate::errors::ReconcileRequired)?;
    Ok(true)
}
async fn pause(stop: &mut watch::Receiver<bool>, seconds: f64) -> bool {
    tokio::select! {_=stop.changed()=>true,_=tokio::time::sleep(Duration::from_secs_f64(seconds))=>*stop.borrow()}
}
enum LaneOutcome {
    Wait(f64),
    Worked(f64),
    Empty(Option<f64>),
}

async fn lane_once(
    app: App,
    id: String,
    bot: Bot,
    tg: Telegram,
    lane: &'static str,
    mut stop: watch::Receiver<bool>,
) -> Result<LaneOutcome> {
    if *stop.borrow() {
        return Ok(LaneOutcome::Wait(0.0));
    }
    match lane {
        "poll" => {
            let key = format!("offset:{id}");
            let offset = app
                .store
                .blocking(move |s| s.get(&key))
                .await?
                .as_i64()
                .unwrap_or(0);
            let query = json!({"offset":offset,"timeout":25,"allowed_updates":["message","callback_query"]});
            let result = tokio::select! {
                biased;
                _ = stop.changed() => return Ok(LaneOutcome::Wait(0.0)),
                result = tg.call("getUpdates", &query, true) => result,
            };
            match result {
                Ok(value) => {
                    let updates = value.as_array().context("invalid updates")?.clone();
                    let empty = updates.is_empty();
                    let work = app.clone();
                    app.store
                        .blocking(move |s| {
                            save_updates(&work, &id, &bot, &updates)?;
                            s.put(
                                &format!("connection:{id}"),
                                &json!({"status":"ok","at":crate::now()}),
                            )
                        })
                        .await?;
                    Ok(LaneOutcome::Wait(if empty { 1.0 } else { 0.1 }))
                }
                Err(failure) => {
                    let delay = failure.delay.max(5.0);
                    app.store
                        .blocking(move |s| {
                            s.put(
                                &format!("connection:{id}"),
                                &json!({"status":"failed","at":crate::now(),"retry_after":delay}),
                            )
                        })
                        .await?;
                    Ok(LaneOutcome::Wait(delay))
                }
            }
        }
        "command" => Ok(LaneOutcome::Wait(
            if command_once_with_stop(&app, &id, &tg, &mut stop).await? {
                0.0
            } else {
                0.5
            },
        )),
        "menu" => {
            let result = tokio::select! {
                biased;
                _ = stop.changed() => return Ok(LaneOutcome::Wait(0.0)),
                result = synchronize(&app, &id, &bot, &tg) => result,
            };
            let ok = result.is_ok();
            app.store
                .blocking(move |s| {
                    s.put(
                        &format!("menu-state:{id}"),
                        &json!({"status":if ok {"ok"} else {"failed"},"at":crate::now()}),
                    )
                })
                .await?;
            result?;
            Ok(LaneOutcome::Wait(30.0))
        }
        _ => {
            if bot.require_start {
                let key = format!("bot_started:{id}");
                if app.store.blocking(move |s| s.get(&key)).await? != json!(true) {
                    return Ok(LaneOutcome::Wait(1.0));
                }
            }
            let claimed_bot = id.clone();
            let event = app
                .store
                .blocking(move |s| s.claim_ready(&claimed_bot, Some(lane == "bark")))
                .await?;
            let event = match event {
                crate::db::Claim::Empty(due) => return Ok(LaneOutcome::Empty(due)),
                crate::db::Claim::Discarded => return Ok(LaneOutcome::Worked(0.0)),
                crate::db::Claim::Event(event) => event,
            };
            {
                let started = crate::now();
                let delay =
                    crate::telegram::deliver_with_stop(&app.store, &tg, &bot, &event, &mut stop)
                        .await?;
                if *stop.borrow() {
                    return Ok(LaneOutcome::Worked(delay));
                }
                // Metrics cannot change an already persisted delivery result.
                if let Err(error) = app
                    .store
                    .blocking(move |s| {
                        s.put(
                            &format!("delivery-timing:{id}:{lane}"),
                            &json!({"seconds":crate::now()-started,"at":crate::now()}),
                        )
                    })
                    .await
                {
                    crate::errors::log("delivery-metrics", &error);
                }
                Ok(LaneOutcome::Worked(delay))
            }
        }
    }
}
async fn bot_worker(
    app: App,
    id: String,
    bot: Bot,
    family: String,
    credential: String,
    stop: watch::Receiver<bool>,
) -> Result<()> {
    let config = app.config.clone();
    let validated = bot.clone();
    let tg = crate::blocking(move || {
        let token = config.token(&validated)?;
        ensure!(
            crate::digest(&token) == credential,
            "credential generation changed"
        );
        Telegram::with_token(&config, &family, token)
    })
    .await?;
    bot_worker_client(app, id, bot, tg, stop).await
}
async fn bot_worker_client(
    app: App,
    id: String,
    bot: Bot,
    tg: Telegram,
    stop: watch::Receiver<bool>,
) -> Result<()> {
    let mut lanes = tokio::task::JoinSet::new();
    for lane in ["poll", "command", "telegram", "bark", "menu"] {
        let (app, id, bot, tg, mut stop) = (
            app.clone(),
            id.clone(),
            bot.clone(),
            tg.clone(),
            stop.clone(),
        );
        lanes.spawn(async move {
            let mut failures = 0_u32;
            let mut idle = crate::optimization::Idle::default();
            let listener = matches!(lane, "telegram" | "bark")
                .then(|| app.store.runtime.delivery_listener(&id, lane == "bark"));
            let (_wake, mut changed) = match listener {
                Some((wake, changed)) => (Some(wake), Some(changed)),
                None => (None, None),
            };
            while !*stop.borrow() {
                if app.config.home.join("run/freeze").exists() {
                    if pause(&mut stop, 1.0).await { break; }
                    continue;
                }
                if let Some(changed) = &mut changed { changed.borrow_and_update(); }
                // The child boundary catches a panic without cancelling another lane.
                let result = Child(tokio::spawn(lane_once(app.clone(), id.clone(), bot.clone(), tg.clone(), lane, stop.clone())))
                    .await.unwrap_or_else(|_| Err(crate::errors::ReconcileRequired.into()));
                if *stop.borrow() { break; }
                let recovering = failures > 0;
                let delay = match &result {
                    Ok(LaneOutcome::Empty(due)) => {
                        failures = 0;
                        let delay = idle.next(&crate::optimization::DELIVERY_IDLE).as_secs_f64();
                        due.filter(|due| due.is_finite())
                            .map(|due| delay.min((due - crate::now()).max(0.0))).unwrap_or(delay)
                    }
                    Ok(LaneOutcome::Wait(delay) | LaneOutcome::Worked(delay)) => {
                        failures = 0;
                        idle.reset();
                        *delay
                    }
                    Err(error) => {
                        idle.reset();
                        failures = failures.saturating_add(1);
                        crate::errors::log(lane, error);
                        if error.is::<crate::errors::ReconcileRequired>() {
                            let bot_id = id.clone();
                            if let Err(error) = app.store.blocking(move |s| {
                                let db = s.connect()?;
                                if lane == "command" { db.execute("UPDATE updates SET status='uncertain' WHERE profile=? AND status='running'", [&bot_id])?; }
                                if matches!(lane, "telegram" | "bark") { db.execute("UPDATE events SET status='uncertain',error='delivery worker interrupted',updated_at=? WHERE profile=? AND status='sending' AND (kind='bark')=?", params![crate::now(),bot_id,lane=="bark"])?; }
                                Ok(())
                            }).await { crate::errors::log("worker-reconcile", &error); }
                        }
                        (5.0 * 2_f64.powi(failures.min(5) as i32 - 1)).min(60.0)
                    }
                };
                if result.is_err() || recovering {
                    let key = format!("worker:{id}:{lane}");
                    if let Err(error) = app.store.blocking(move |s| {
                        let old = s.get(&key)?;
                        let count = old["restarts"].as_u64().unwrap_or(0) + u64::from(failures > 0);
                        s.put(&key, &json!({"restarts":count,"consecutive_failures":failures,"at":crate::now(),"status":if failures==0 {"ok"} else {"retrying"}}))
                    }).await { crate::errors::log("worker-status", &error); }
                }
                let stopped = if matches!(result, Ok(LaneOutcome::Empty(_))) {
                    if let Some(changed) = &mut changed {
                        tokio::select! {
                            _ = stop.changed() => true,
                            _ = changed.changed() => false,
                            _ = tokio::time::sleep(Duration::from_secs_f64(delay)) => *stop.borrow(),
                        }
                    } else { pause(&mut stop, delay).await }
                } else { pause(&mut stop, delay).await };
                if stopped { break; }
            }
        });
    }
    while let Some(result) = lanes.join_next().await {
        if let Err(error) = result {
            crate::errors::log("lane-supervisor", &error.into());
        }
    }
    Ok(())
}
fn configuration_fingerprint(config: &crate::config::Config) -> String {
    let mut bytes = std::fs::read(config.home.join("config/bots.json")).unwrap_or_default();
    let registry = config.home.join("modules/registry.json");
    let index = std::fs::read(&registry).unwrap_or_default();
    bytes.extend(&index);
    let mut roots = std::collections::BTreeSet::new();
    if registry.exists() {
        if let Ok(value) = serde_json::from_slice::<Value>(&index)
            && let Some(modules) = value["modules"].as_object()
        {
            roots.extend(
                modules
                    .values()
                    .filter_map(Value::as_str)
                    .map(str::to_owned),
            );
        }
    } else if let Ok(base) = config.contained("modules")
        && let Ok(entries) = std::fs::read_dir(base)
    {
        for entry in entries.flatten() {
            roots.insert(entry.file_name().to_string_lossy().into_owned());
        }
    }
    for relative in roots {
        bytes.extend(relative.as_bytes());
        if let Ok(path) = config.contained(&format!("modules/{relative}/module.json")) {
            bytes.extend(std::fs::read(path).unwrap_or_default());
        }
    }
    crate::digest(bytes)
}
pub fn acquire_lock(config: &crate::config::Config) -> Result<std::fs::File> {
    std::fs::create_dir_all(config.home.join("gateway"))?;
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(config.home.join("gateway/daemon.lock"))?;
    ensure!(crate::runtime::try_lock(&lock)?, "gateway already running");
    Ok(lock)
}

pub async fn run(app: App, _lock: &std::fs::File) -> Result<()> {
    app.store
        .blocking(|s| crate::completion::reconcile(&s))
        .await?;
    app.store.blocking(|s| s.recover()).await?;
    if let Some(target) = app.snapshot.read().await.settings.routes.get("personal") {
        let target = target.clone();
        app.store
            .blocking(move |s| s.uncertain_digest(&target))
            .await?;
    }
    let mut rpc = Child(tokio::spawn(crate::api::serve_socket(app.clone())));
    let port = app.snapshot.read().await.settings.web_port;
    let mut web = Child(tokio::spawn(crate::api::serve_web(app.clone(), port)));
    type BotWorker = (String, watch::Sender<bool>, Child<Result<()>>);
    let mut workers: BTreeMap<String, BotWorker> = BTreeMap::new();
    let mut shutdown = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let (shutdown_tx, mut stopping) = watch::channel(false);
    let _signal = Child(tokio::spawn(async move {
        tokio::select! { _ = shutdown.recv() => {}, _ = tokio::signal::ctrl_c() => {} }
        let _ = shutdown_tx.send(true);
    }));
    let journal_store = app.store.clone();
    let reconciler = Child(tokio::spawn(crate::completion::run(journal_store)));
    let mut rejected = app
        .store
        .blocking(|s| s.get("configuration-rejected"))
        .await?["fingerprint"]
        .as_str()
        .unwrap_or("")
        .to_owned();
    let mut logged_rejected = String::new();
    let mut retrying: BTreeMap<String, (u32, f64)> = BTreeMap::new();
    let mut next_digest = crate::now() + 60.0;
    let supervise = async {
        loop {
            // A malformed replacement keeps the entire last valid configuration active.
            if let Err(error) = app.reload_if_changed().await {
                let config = app.config.clone();
                let fingerprint =
                    crate::blocking(move || Ok(configuration_fingerprint(&config))).await?;
                if fingerprint != rejected {
                    if logged_rejected != fingerprint {
                        crate::errors::log("configuration rejected", &error);
                        logged_rejected = fingerprint.clone();
                    }
                    let hash = fingerprint.clone();
                    let target = app
                        .snapshot
                        .read()
                        .await
                        .settings
                        .routes
                        .get("personal")
                        .cloned();
                    match app
                        .store
                        .blocking(move |s| s.configuration_rejected(&hash, target.as_deref()))
                        .await
                    {
                        Ok(()) => rejected = fingerprint,
                        Err(error) => crate::errors::log("configuration-status", &error),
                    }
                }
            } else if !rejected.is_empty() {
                match app
                    .store
                    .blocking(|s| s.put("configuration-rejected", &Value::Null))
                    .await
                {
                    Ok(()) => {
                        rejected.clear();
                        logged_rejected.clear();
                    }
                    Err(error) => crate::errors::log("configuration-status", &error),
                }
            }
            let snapshot = app.snapshot.read().await.clone();
            let mut desired = BTreeMap::new();
            for (id, bot) in snapshot.settings.bots.iter().filter(|(_, b)| b.enabled) {
                desired.insert(
                    id.clone(),
                    crate::digest(format!(
                        "{}:{}:{}",
                        snapshot.settings.telegram_ip_family,
                        serde_json::to_string(bot)?,
                        snapshot.credentials[id]
                    )),
                );
            }
            let remove = workers
                .iter()
                .filter(|(id, (hash, _, task))| {
                    desired.get(*id) != Some(hash) || task.is_finished()
                })
                .map(|(id, _)| id.clone())
                .collect::<Vec<_>>();
            for id in remove {
                if let Some((_, stop, task)) = workers.get(&id) {
                    let _ = stop.send(true);
                    if !task.is_finished() {
                        continue;
                    }
                }
                if let Some((hash, stop, task)) = workers.remove(&id) {
                    let changed = desired.get(&id) != Some(&hash);
                    let _ = stop.send(true);
                    let result = task.await;
                    if !changed && !matches!(result, Ok(Ok(()))) {
                        let attempts = retrying
                            .get(&id)
                            .map(|v| v.0)
                            .unwrap_or(0)
                            .saturating_add(1);
                        retrying.insert(
                            id.clone(),
                            (
                                attempts,
                                crate::now()
                                    + (5.0 * 2_f64.powi(attempts.min(4) as i32 - 1)).min(60.0),
                            ),
                        );
                    } else {
                        retrying.remove(&id);
                    }
                    // A failed subloop can leave a claimed send/update with no owner.
                    // Quarantine it without replay, scoped to this bot only.
                    let bot_id = id.clone();
                    if let Err(error) = app
                        .store
                        .blocking(move |s| crate::completion::interrupt_bot(&s, &bot_id))
                        .await
                    {
                        crate::errors::log("worker-retirement", &error);
                    }
                }
            }
            for (id, hash) in desired {
                if retrying.get(&id).is_some_and(|(_, at)| *at > crate::now()) {
                    continue;
                }
                workers.entry(id.clone()).or_insert_with(|| {
                    let (tx, rx) = watch::channel(false);
                    let task = Child(tokio::spawn(bot_worker(
                        app.clone(),
                        id.clone(),
                        snapshot.settings.bots[&id].clone(),
                        snapshot.settings.telegram_ip_family.clone(),
                        snapshot.credentials[&id].clone(),
                        rx,
                    )));
                    (hash, tx, task)
                });
            }
            if let Some(target) = snapshot.settings.routes.get("personal")
                && crate::now() >= next_digest
            {
                next_digest = crate::now() + 60.0;
                let target = target.clone();
                if let Err(error) = app
                    .store
                    .blocking(move |s| s.uncertain_digest(&target))
                    .await
                {
                    crate::errors::log("uncertain-summary", &error)
                }
            }
            if rpc.is_finished() {
                (&mut rpc).await.context("gateway RPC listener failed")??;
                anyhow::bail!("gateway RPC listener stopped")
            }
            if web.is_finished() {
                (&mut web).await.context("gateway web listener failed")??;
                anyhow::bail!("gateway web listener stopped")
            }
            tokio::time::sleep(Duration::from_secs(5)).await;
        }
        #[allow(unreachable_code)]
        Ok::<(), anyhow::Error>(())
    };
    let outcome = tokio::select! {
        biased;
        _ = stopping.changed() => Ok(()),
        result = supervise => result,
    };
    // Five seconds for cooperative draining/database reconciliation and two for
    // runtime shutdown in main, within the global eight-second gateway budget.
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    rpc.abort();
    web.abort();
    reconciler.abort();
    let bot_ids = workers.keys().cloned().collect::<Vec<_>>();
    for (_, stop, _) in workers.values() {
        let _ = stop.send(true);
    }
    let mut draining = tokio::task::JoinSet::new();
    for (_, (_, _, task)) in workers {
        draining.spawn(task);
    }
    let _ = tokio::time::timeout_at(deadline, async {
        while draining.join_next().await.is_some() {}
    })
    .await;
    draining.abort_all();
    let _ = tokio::time::timeout_at(
        deadline,
        app.store.blocking(move |store| {
            for bot in bot_ids {
                crate::completion::interrupt_bot(&store, &bot)?;
            }
            Ok(())
        }),
    )
    .await;
    outcome
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{Json, Router, extract::State, routing::post};
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };
    use tokio::sync::Semaphore;

    #[test]
    fn configuration_fingerprint_covers_indexed_and_discovered_manifests() {
        let temp = tempfile::tempdir().unwrap();
        let config = crate::config::Config::new(temp.path().to_path_buf());
        let root = config.home.join("modules/fixture");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("module.json"), b"invalid one").unwrap();
        let first = configuration_fingerprint(&config);
        assert_eq!(first, configuration_fingerprint(&config));
        std::fs::write(root.join("module.json"), b"invalid two").unwrap();
        assert_ne!(first, configuration_fingerprint(&config));
        std::fs::write(
            config.home.join("modules/registry.json"),
            br#"{"api_version":2,"modules":{"fixture":"fixture"}}"#,
        )
        .unwrap();
        let indexed = configuration_fingerprint(&config);
        std::fs::write(root.join("module.json"), b"invalid three").unwrap();
        assert_ne!(indexed, configuration_fingerprint(&config));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn real_bot_lanes_deliver_telegram_while_bark_waits() {
        let temp = tempfile::tempdir().unwrap();
        let config = crate::config::Config::new(temp.path().to_path_buf());
        let store = crate::db::Store::new(&config.db()).unwrap();
        let bot = Bot {
            require_start: false,
            owner: "101".into(),
            credential: "fixture".into(),
            username: String::new(),
            enabled: true,
            menus: vec![],
        };
        let app = App {
            config,
            store: store.clone(),
            snapshot: Arc::new(tokio::sync::RwLock::new(Snapshot {
                settings: crate::config::Settings::default(),
                catalog: crate::catalog::Catalog::default(),
                credentials: BTreeMap::new(),
            })),
            reload_guard: Arc::default(),
        };
        let wanted = menu(&*app.snapshot.read().await, "fixture");
        store
            .put(
                "menu:fixture",
                &json!(crate::digest(serde_json::to_vec(&wanted).unwrap())),
            )
            .unwrap();
        let accepted = Arc::new(Semaphore::new(0));
        let release = Arc::new(Semaphore::new(0));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        async fn bark(
            State((accepted, release)): State<(Arc<Semaphore>, Arc<Semaphore>)>,
        ) -> Json<Value> {
            accepted.add_permits(1);
            let _permit = release.acquire().await.unwrap();
            Json(json!({"code":200}))
        }
        let router = Router::new()
            .route("/push", post(bark))
            .with_state((accepted.clone(), release.clone()));
        let bark_server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        let sends = Arc::new(AtomicUsize::new(0));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let telegram_url = format!("http://{}", listener.local_addr().unwrap());
        async fn telegram(
            axum::extract::Path(method): axum::extract::Path<String>,
            State(sends): State<Arc<AtomicUsize>>,
        ) -> Json<Value> {
            let result = if method == "sendMessage" {
                sends.fetch_add(1, Ordering::Relaxed);
                json!({"message_id":1,"chat":{"id":101}})
            } else {
                json!([])
            };
            Json(json!({"ok":true,"result":result}))
        }
        let router = Router::new()
            .route("/botfixture/{method}", post(telegram))
            .with_state(sends.clone());
        let telegram_server =
            tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        store.submit("fixture", &json!({"id":"bark-held","kind":"bark","payload":{"fixture_transport":format!("http://{address}/push"),"body":{}}}), "fixture").unwrap();
        let (stop, receiver) = watch::channel(false);
        let worker = tokio::spawn(bot_worker_client(
            app,
            "fixture".into(),
            bot,
            Telegram::fixture(telegram_url),
            receiver,
        ));
        let started = tokio::time::timeout(Duration::from_secs(3), accepted.acquire())
            .await
            .is_ok();
        store
            .submit(
                "fixture",
                &json!({"id":"telegram-free","kind":"text","payload":{"text":"fixture"}}),
                "fixture",
            )
            .unwrap();
        let delivered = tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                if store.event("telegram-free").unwrap()["status"] == "delivered" {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await
        .is_ok();
        let held = store.event("bark-held").unwrap()["status"] == "sending";
        release.add_permits(1);
        stop.send(true).unwrap();
        let stopped = tokio::time::timeout(Duration::from_secs(3), worker)
            .await
            .unwrap()
            .unwrap();
        bark_server.abort();
        telegram_server.abort();
        stopped.unwrap();
        assert!(started && delivered && held);
        assert_eq!(sends.load(Ordering::Relaxed), 1);
        assert_eq!(store.event("bark-held").unwrap()["status"], "delivered");
    }
}
