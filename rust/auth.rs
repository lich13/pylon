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

/// The root supervisor verifies caches and process ownership under its service
/// lock. The gateway only deletes the exact hashes for the specified service.
pub(crate) fn retire_service(store: &Store, p: &Value) -> Result<Value> {
    let module = p["module"].as_str().context("module required")?;
    let service = p["service"].as_str().context("service required")?;
    ensure!(
        crate::identifier(module) && crate::identifier(service),
        "invalid service identity"
    );
    let hashes = p["hashes"].as_array().context("context hashes required")?;
    ensure!(hashes.len() <= 1000, "too many contexts");
    let mut db = store.connect()?;
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let mut removed = 0;
    for value in hashes {
        let hash = value.as_str().context("invalid context hash")?;
        ensure!(
            hash.len() == 64
                && hash
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
            "invalid context hash"
        );
        removed += tx.execute(
            "DELETE FROM contexts WHERE hash=? AND module=? AND origin=?",
            params![hash, module, format!("service:{service}")],
        )?;
    }
    tx.commit()?;
    Ok(json!({"removed":removed}))
}

pub async fn request(app: &App, ctx: &Value, method: &str, p: &Value) -> Result<Value> {
    let snapshot = app.snapshot.read().await.clone();
    let bot_id = ctx["bot"].as_str().context("bot missing")?;
    let bot = snapshot.settings.bots.get(bot_id).context("bot removed")?;
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
        _ => anyhow::bail!("unknown auth method"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retirement_is_exact_scoped_and_atomic_on_failure() {
        let temporary = tempfile::tempdir().unwrap();
        let store = Store::new(&temporary.path().join("state.sqlite3")).unwrap();
        let db = store.connect().unwrap();
        for (token, module, origin) in [
            ("old", "fixture", "service:relay"),
            ("active", "fixture", "service:relay"),
            ("job", "fixture", "job:fixture"),
            ("other", "other", "service:relay"),
        ] {
            db.execute(
                "INSERT INTO contexts VALUES(?,?, 'fixture','[]',0,?,'fixture')",
                params![crate::digest(token), module, origin],
            )
            .unwrap();
        }
        let valid = json!({"module":"fixture","service":"relay","hashes":[crate::digest("old"), crate::digest("job"), crate::digest("other")]});
        let mut invalid = valid.clone();
        invalid["hashes"]
            .as_array_mut()
            .unwrap()
            .push(json!("invalid"));
        assert!(retire_service(&store, &invalid).is_err());
        assert_eq!(
            db.query_row("SELECT count(*) FROM contexts", [], |r| r.get::<_, u64>(0))
                .unwrap(),
            4
        );
        assert_eq!(retire_service(&store, &valid).unwrap()["removed"], 1);
        assert_eq!(retire_service(&store, &valid).unwrap()["removed"], 0);
        assert_eq!(
            db.query_row("SELECT count(*) FROM contexts", [], |r| r.get::<_, u64>(0))
                .unwrap(),
            3
        );
    }
}
