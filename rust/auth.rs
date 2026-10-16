use crate::{api::App, catalog::Module, db::Store};
use anyhow::{Context, Result, ensure};
use hmac::{Hmac, Mac};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};
use sha2::Sha256;

pub fn nonce() -> String {
    format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    )
}
pub fn context(
    store: &Store,
    module: &Module,
    bot: &str,
    workspace: &str,
    origin: &str,
) -> Result<String> {
    let token = nonce();
    store.connect()?.execute(
        "INSERT INTO contexts VALUES(?,?,?,?,?,?,?)",
        params![
            crate::digest(&token),
            module.manifest.id,
            bot,
            serde_json::to_string(&module.manifest.capabilities)?,
            0,
            origin,
            workspace
        ],
    )?;
    Ok(token)
}
pub fn inspect(store: &Store, token: &str) -> Result<Value> {
    ensure!(token.len() == 64, "invalid execution context");
    store.connect()?.query_row("SELECT module,bot,capabilities,origin,workspace FROM contexts WHERE hash=? AND (expires_at=0 OR expires_at>?)",params![crate::digest(token),crate::now()],|r|Ok(json!({"module":r.get::<_,String>(0)?,"bot":r.get::<_,String>(1)?,"capabilities":serde_json::from_str::<Value>(&r.get::<_,String>(2)?).unwrap_or(Value::Null),"origin":r.get::<_,String>(3)?,"workspace":r.get::<_,String>(4)?}))).optional()?.context("execution context expired")
}
pub fn revoke(store: &Store, token: &str) -> Result<()> {
    store
        .connect()?
        .execute("DELETE FROM contexts WHERE hash=?", [crate::digest(token)])?;
    Ok(())
}

pub async fn request(app: &App, ctx: &Value, method: &str, p: &Value) -> Result<Value> {
    let snapshot = app.snapshot.read().await;
    let bot_id = ctx["bot"].as_str().context("bot missing")?;
    let bot = snapshot.settings.bots.get(bot_id).context("bot removed")?;
    let workspace = p["workspace"].as_str().unwrap_or("");
    match method {
        "auth.owner" => Ok(json!({"owner":bot.owner,"bot":bot_id})),
        "auth.sign" => {
            let target_bot = p["bot"].as_str().unwrap_or(bot_id);
            let target = snapshot
                .settings
                .bots
                .get(target_bot)
                .context("bot missing")?;
            ensure!(bot.owner == target.owner, "owner mismatch");
            let mut mac = Hmac::<Sha256>::new_from_slice(app.config.token(target)?.as_bytes())?;
            mac.update(
                format!(
                    "{}|{}|{}",
                    target_bot,
                    p["action"].as_str().unwrap_or(""),
                    p["target"].as_str().unwrap_or("")
                )
                .as_bytes(),
            );
            Ok(json!(
                hex::encode(mac.finalize().into_bytes())[..32].to_owned()
            ))
        }
        "auth.callback" => {
            let target_bot = p["bot"].as_str().unwrap_or(bot_id);
            let target = snapshot
                .settings
                .bots
                .get(target_bot)
                .context("bot missing")?;
            ensure!(bot.owner == target.owner, "owner mismatch");
            Ok(json!(app.store.own_callback(
                target_bot,
                &target.owner,
                &p["query"]
            )?))
        }
        "auth.url" => {
            ensure!(
                snapshot
                    .catalog
                    .modules
                    .values()
                    .any(|m| m.manifest.web.contains_key(workspace)),
                "unknown workspace"
            );
            ensure!(
                !snapshot.settings.public_origin.is_empty(),
                "public origin not configured"
            );
            Ok(json!(format!(
                "{}/pylon/{workspace}/",
                snapshot.settings.public_origin.trim_end_matches('/')
            )))
        }
        "auth.issue" => {
            ensure!(
                ctx["origin"] == "command"
                    || ctx["origin"] == "callback"
                    || ctx["origin"] == "document",
                "login requires an authenticated owner request"
            );
            ensure!(
                snapshot
                    .catalog
                    .modules
                    .values()
                    .any(|m| m.manifest.web.contains_key(workspace)),
                "unknown workspace"
            );
            let token = nonce();
            app.store.connect()?.execute(
                "INSERT INTO web_tokens VALUES(?,?,?,?,?,NULL)",
                params![
                    crate::digest(&token),
                    bot.owner,
                    bot_id,
                    workspace,
                    crate::now() + 600.0
                ],
            )?;
            Ok(json!(token))
        }
        "auth.redeem" => {
            let token = p["token"].as_str().context("login token required")?;
            ensure!((32..=100).contains(&token.len()), "invalid login token");
            let mut db = app.store.connect()?;
            let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
            let row:Option<(String,String)>=tx.query_row("SELECT owner,bot FROM web_tokens WHERE hash=? AND used_at IS NULL AND expires_at>?",params![crate::digest(token),crate::now()],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
            let (owner, source) = row.context("login expired or already used")?;
            ensure!(
                snapshot
                    .settings
                    .bots
                    .get(&source)
                    .is_some_and(|b| b.owner == owner)
                    && owner == bot.owner,
                "login owner changed"
            );
            let session = nonce();
            let csrf = nonce();
            tx.execute(
                "UPDATE web_tokens SET used_at=? WHERE hash=?",
                params![crate::now(), crate::digest(token)],
            )?;
            tx.execute(
                "INSERT INTO web_sessions VALUES(?,?,?,?,?)",
                params![
                    crate::digest(&session),
                    owner,
                    source,
                    csrf,
                    crate::now() + 7.0 * 86400.0
                ],
            )?;
            tx.commit()?;
            Ok(json!({"cookie":session,"csrf":csrf}))
        }
        "auth.session" => {
            ensure!(
                snapshot
                    .catalog
                    .modules
                    .values()
                    .any(|m| m.manifest.web.contains_key(workspace)),
                "unknown workspace"
            );
            let row:Option<(String,String,String,f64)>=app.store.connect()?.query_row("SELECT owner,bot,csrf,expires_at FROM web_sessions WHERE hash=? AND expires_at>?",params![crate::digest(p["cookie"].as_str().unwrap_or("")),crate::now()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
            let Some((owner, source, csrf, expires_at)) = row else {
                return Ok(Value::Null);
            };
            if owner != bot.owner
                || snapshot
                    .settings
                    .bots
                    .get(&source)
                    .is_none_or(|b| b.owner != owner)
            {
                return Ok(Value::Null);
            }
            Ok(
                json!({"profile":ctx["workspace"],"bot":source,"owner":owner,"csrf":csrf,"expires_at":expires_at}),
            )
        }
        "auth.logout" => {
            app.store.connect()?.execute(
                "DELETE FROM web_sessions WHERE hash=?",
                [crate::digest(p["cookie"].as_str().unwrap_or(""))],
            )?;
            Ok(Value::Null)
        }
        _ => anyhow::bail!("unknown auth method"),
    }
}
