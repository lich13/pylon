use crate::{
    api::{App, Snapshot},
    catalog::Module,
    config::Bot,
    telegram::{Telegram, authorized},
};
use anyhow::{Context, Result};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};
use std::{collections::BTreeMap, time::Duration};
use tokio::sync::watch;

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
    if app.store.get(&format!("menu:{id}"))? == hash {
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
    app.store.put(&format!("menu:{id}"), &json!(hash))?;
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
    module: &Module,
    bot: &str,
    action: &str,
    payload: Value,
    request_id: &str,
    reply_id: Option<i64>,
) -> Result<Value> {
    let origin = if matches!(action, "text" | "start" | "before_command") {
        "command"
    } else {
        action
    };
    let token = crate::auth::context(&app.store, module, bot, &module.manifest.workspace, origin)?;
    let result=crate::runtime::request_executor(&app.config,&json!({"module":module.manifest.id,"context":token,"action":action,"payload":payload,"request_id":request_id,"reply_id":reply_id})).await;
    crate::auth::revoke(&app.store, &token)?;
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
            module,
            id,
            "callback",
            json!({"query":message}),
            request_id,
            message["message"]["message_id"].as_i64(),
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
            module,
            id,
            "document",
            json!({"message":message}),
            request_id,
            reply_id,
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
                        module,
                        id,
                        "start",
                        json!({"argument":argument}),
                        request_id,
                        reply_id,
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
                hook,
                id,
                "before_command",
                json!({"name":name,"argument":argument}),
                request_id,
                reply_id,
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
            let bot = &snapshot.settings.bots[target];
            return Ok(
                json!({"text":format!("此功能已迁至 @{}，请在那里使用 /{}。",bot.username,command.name)}),
            );
        }
        invoke(
            app,
            module,
            id,
            "command",
            json!({"name":name,"argument":argument,"command_action":command.action}),
            request_id,
            reply_id,
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
                module,
                id,
                "text",
                json!({"text":text,"message":message,"reply_id":reply_id}),
                request_id,
                reply_id,
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
    let row = {
        let mut db = app.store.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let row:Option<(i64,String,String)>=tx.query_row("SELECT update_id,kind,payload FROM updates WHERE profile=? AND status='pending' ORDER BY update_id LIMIT 1",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
        if let Some((uid, _, _)) = &row {
            tx.execute(
                "UPDATE updates SET status='running' WHERE profile=? AND update_id=?",
                params![id, uid],
            )?;
        }
        tx.commit()?;
        row
    };
    let Some((uid, kind, payload)) = row else {
        return Ok(false);
    };
    let message: Value = serde_json::from_str(&payload)?;
    if kind == "callback_query" {
        let _ = tg
            .call(
                "answerCallbackQuery",
                &json!({"callback_query_id":message["id"]}),
                true,
            )
            .await;
    }
    let result = route(app, id, &kind, &message, &format!("telegram:{id}:{uid}")).await;
    let (mut reply, status) = match result {
        Ok(value) => (value, "done"),
        Err(_) => (
            json!({"text":"操作未完成或结果待核对。请打开对应入口查看记录；没有自动重复执行。"}),
            "uncertain",
        ),
    };
    let owner = reply
        .as_object_mut()
        .and_then(|v| v.remove("_module"))
        .and_then(|v| v.as_str().map(str::to_owned))
        .unwrap_or_else(|| "gateway".into());
    if reply["text"].as_str().is_some_and(|s| !s.is_empty()) {
        let key = crate::digest(format!("{id}:text:reply:{uid}"))[..32].to_owned();
        app.store.submit(
            &owner,
            &json!({"id":key,"kind":"text","job_id":"bot","payload":reply}),
            id,
        )?;
    }
    app.store.connect()?.execute(
        "UPDATE updates SET status=? WHERE profile=? AND update_id=?",
        params![status, id, uid],
    )?;
    Ok(true)
}
async fn pause(stop: &mut watch::Receiver<bool>, seconds: f64) -> bool {
    tokio::select! {_=stop.changed()=>true,_=tokio::time::sleep(Duration::from_secs_f64(seconds))=>*stop.borrow()}
}
async fn bot_worker(app: App, id: String, bot: Bot, mut stop: watch::Receiver<bool>) -> Result<()> {
    let tg = Telegram::new(&app.config, &bot)?;
    let poll = {
        let app = app.clone();
        let id = id.clone();
        let bot = bot.clone();
        let tg = tg.clone();
        let mut stop = stop.clone();
        async move {
            while !*stop.borrow() {
                if app.config.home.join("run/freeze").exists() {
                    if pause(&mut stop, 1.0).await {
                        break;
                    }
                    continue;
                }
                let offset = app
                    .store
                    .get(&format!("offset:{id}"))?
                    .as_i64()
                    .unwrap_or(0);
                match tg.call("getUpdates",&json!({"offset":offset,"timeout":25,"allowed_updates":["message","callback_query"]}),true).await {
                Ok(value)=>{app.store.put(&format!("connection:{id}"),&json!({"status":"ok","at":crate::now()}))?;save_updates(&app,&id,&bot,value.as_array().context("invalid updates")?)?;if pause(&mut stop,if value.as_array().is_some_and(|a|a.is_empty()){1.0}else{0.1}).await{break}},
                Err(f)=>{app.store.put(&format!("connection:{id}"),&json!({"status":"failed","at":crate::now(),"retry_after":f.delay.max(5.0)}))?;if pause(&mut stop,f.delay.max(5.0)).await{break}}
            }
            }
            Ok::<(), anyhow::Error>(())
        }
    };
    let commands = {
        let app = app.clone();
        let id = id.clone();
        let tg = tg.clone();
        let mut stop = stop.clone();
        async move {
            while !*stop.borrow() {
                if !app.config.home.join("run/freeze").exists()
                    && command_once(&app, &id, &tg).await?
                {
                    continue;
                }
                if pause(&mut stop, 0.5).await {
                    break;
                }
            }
            Ok::<(), anyhow::Error>(())
        }
    };
    let sends = async move {
        let mut metadata_at = 0.0;
        while !*stop.borrow() {
            if app.config.home.join("run/freeze").exists() {
                if pause(&mut stop, 1.0).await {
                    break;
                }
                continue;
            }
            if crate::now() > metadata_at {
                metadata_at = crate::now() + 30.0;
                let result = synchronize(&app, &id, &bot, &tg).await;
                app.store.put(
                    &format!("menu-state:{id}"),
                    &json!({"status":if result.is_ok(){"ok"}else{"failed"},"at":crate::now()}),
                )?;
            }
            if bot.require_start && app.store.get(&format!("bot_started:{id}"))? != json!(true) {
                if pause(&mut stop, 1.0).await {
                    break;
                }
                continue;
            }
            let delay = if let Some(event) = app.store.claim(&id)? {
                crate::telegram::deliver(&app.store, &tg, &bot, &event).await?
            } else {
                0.5
            };
            if pause(&mut stop, delay).await {
                break;
            }
        }
        Ok::<(), anyhow::Error>(())
    };
    tokio::try_join!(poll, commands, sends)?;
    Ok(())
}
pub async fn run(app: App) -> Result<()> {
    use fs2::FileExt;
    std::fs::create_dir_all(app.config.home.join("gateway"))?;
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(app.config.home.join("gateway/daemon.lock"))?;
    lock.try_lock_exclusive()
        .context("gateway already running")?;
    app.store.recover()?;
    let rpc = tokio::spawn(crate::api::serve_socket(app.clone()));
    let web = tokio::spawn(crate::api::serve_web(app.clone(), 8793));
    type BotWorker = (
        String,
        watch::Sender<bool>,
        tokio::task::JoinHandle<Result<()>>,
    );
    let mut workers: BTreeMap<String, BotWorker> = BTreeMap::new();
    let mut shutdown = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    loop {
        // A malformed replacement keeps the entire last valid configuration active.
        if app.reload().await.is_err() {
            eprintln!("configuration rejected; previous registry remains active");
        }
        let snapshot = app.snapshot.read().await.clone();
        let mut desired = BTreeMap::new();
        for (id, bot) in snapshot.settings.bots.iter().filter(|(_, b)| b.enabled) {
            desired.insert(
                id.clone(),
                crate::digest(format!(
                    "{}:{}",
                    serde_json::to_string(bot)?,
                    crate::digest(app.config.token(bot)?)
                )),
            );
        }
        let remove = workers
            .iter()
            .filter(|(id, (hash, _, task))| desired.get(*id) != Some(hash) || task.is_finished())
            .map(|(id, _)| id.clone())
            .collect::<Vec<_>>();
        for id in remove {
            if let Some((_, stop, task)) = workers.remove(&id) {
                let _ = stop.send(true);
                let _ = task.await;
                // A failed subloop can leave a claimed send/update with no owner.
                // Quarantine it without replay, scoped to this bot only.
                let db = app.store.connect()?;
                db.execute("UPDATE events SET status='uncertain',error='bot worker stopped before acknowledgement',updated_at=? WHERE profile=? AND status='sending'",params![crate::now(),id])?;
                db.execute(
                    "UPDATE updates SET status='uncertain' WHERE profile=? AND status='running'",
                    [&id],
                )?;
                app.store.put(
                    &format!("connection:{id}"),
                    &json!({"status":"restarting","at":crate::now()}),
                )?;
            }
        }
        for (id, hash) in desired {
            workers.entry(id.clone()).or_insert_with(|| {
                let (tx, rx) = watch::channel(false);
                let task = tokio::spawn(bot_worker(
                    app.clone(),
                    id.clone(),
                    snapshot.settings.bots[&id].clone(),
                    rx,
                ));
                (hash, tx, task)
            });
        }
        if rpc.is_finished() {
            rpc.await.context("gateway RPC listener task failed")??;
            anyhow::bail!("gateway RPC listener stopped")
        }
        if web.is_finished() {
            web.await.context("gateway web listener task failed")??;
            anyhow::bail!("gateway web listener stopped")
        }
        tokio::select! {_=shutdown.recv()=>break,_=tokio::signal::ctrl_c()=>break,_=tokio::time::sleep(Duration::from_secs(5))=>{}}
    }
    for (_, (_, stop, task)) in workers {
        let _ = stop.send(true);
        let _ = task.await;
    }
    rpc.abort();
    web.abort();
    Ok(())
}
