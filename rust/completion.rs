//! Direct, transactional completion writes. v0.5.30 removed the file mailbox:
//! a result that cannot be committed leaves its row `sending`/`running`, which
//! worker retirement or startup recovery marks uncertain. Nothing is replayed.
use crate::db::Store;
use anyhow::{Context, Result, ensure};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::Path,
    time::Duration,
};
use tokio::sync::watch;

#[derive(Clone)]
pub(crate) struct Completion {
    result: Outcome,
}

#[derive(Clone)]
enum Outcome {
    Delivery {
        id: String,
        bot: String,
        content: String,
        attempts: u64,
        status: String,
        receipt: Option<Value>,
        reason: String,
        next_attempt: f64,
    },
    Command {
        bot: String,
        update: i64,
        content: String,
        owner: String,
        reply: Value,
        status: String,
        contexts: Vec<String>,
    },
}

impl Completion {
    pub(crate) fn delivery(
        event: &Value,
        status: &str,
        receipt: Option<Value>,
        reason: &str,
        delay: f64,
    ) -> Result<Self> {
        Ok(Self {
            result: Outcome::Delivery {
                id: event["id"].as_str().context("event id missing")?.into(),
                bot: event["profile"]
                    .as_str()
                    .context("event bot missing")?
                    .into(),
                content: crate::digest(serde_json::to_vec(&event["payload"])?),
                attempts: event["attempts"]
                    .as_u64()
                    .context("event attempt missing")?,
                status: status.into(),
                receipt,
                reason: reason.into(),
                next_attempt: crate::now() + delay,
            },
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn command(
        bot: &str,
        update: i64,
        payload: &str,
        owner: &str,
        reply: Value,
        status: &str,
        contexts: &[String],
    ) -> Self {
        Self {
            result: Outcome::Command {
                bot: bot.into(),
                update,
                content: crate::digest(payload),
                owner: owner.into(),
                reply,
                status: status.into(),
                contexts: contexts.iter().map(crate::digest).collect(),
            },
        }
    }
}

/// Count records left in the mailbox of v0.5.29 and older without reading
/// them. This release cannot apply them; deployment requires zero.
pub(crate) fn pending_count(database: &Path) -> Result<u64> {
    let state = fs::symlink_metadata(database)?;
    ensure!(
        state.is_file() && state.nlink() == 1 && state.permissions().mode() & 0o777 == 0o600,
        "unsafe completion state"
    );
    let path = database
        .parent()
        .context("missing state directory")?
        .join("completions");
    let metadata = match fs::symlink_metadata(&path) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(error) => return Err(error.into()),
    };
    ensure!(
        metadata.is_dir()
            && metadata.uid() == state.uid()
            && metadata.permissions().mode() & 0o777 == 0o700,
        "unsafe completion directory"
    );
    let mut count = 0;
    for entry in fs::read_dir(&path)? {
        let entry = entry?;
        let metadata = match fs::symlink_metadata(entry.path()) {
            Ok(value) => value,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error.into()),
        };
        ensure!(
            metadata.is_file()
                && metadata.nlink() == 1
                && metadata.uid() == state.uid()
                && metadata.permissions().mode() & 0o777 == 0o600,
            "unsafe completion file"
        );
        count += 1;
    }
    // Detect replacement of the mailbox while enumerating it.
    let after = fs::symlink_metadata(&path)?;
    ensure!(
        after.is_dir()
            && after.dev() == metadata.dev()
            && after.ino() == metadata.ino()
            && after.uid() == metadata.uid()
            && after.permissions().mode() & 0o777 == 0o700,
        "completion directory changed"
    );
    Ok(count)
}

fn apply(store: &Store, record: &Completion) -> Result<()> {
    let mut db = store.connect()?;
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let mut ready = None;
    match &record.result {
        Outcome::Delivery {
            id,
            bot,
            content,
            attempts,
            status,
            receipt,
            reason,
            next_attempt,
        } => {
            ensure!(
                matches!(
                    status.as_str(),
                    "sending" | "delivered" | "pending" | "uncertain" | "failed" | "suppressed"
                ),
                "invalid completion status"
            );
            let row: Option<(String, String, u64, String, Option<String>)> = tx
                .query_row(
                    "SELECT profile,payload,attempts,status,receipt FROM events WHERE id=?",
                    [id],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
                )
                .optional()?;
            let (actual_bot, payload, actual_attempts, current, previous) =
                row.context("completion event missing")?;
            let payload: Value = serde_json::from_str(&payload)?;
            ensure!(
                actual_bot == *bot && crate::digest(serde_json::to_vec(&payload)?) == *content,
                "completion event changed"
            );
            ensure!(actual_attempts >= *attempts, "completion attempt invalid");
            if actual_attempts == *attempts
                && !matches!(current.as_str(), "delivered" | "failed" | "suppressed")
            {
                let previous: Option<Value> =
                    previous.map(|s| serde_json::from_str(&s)).transpose()?;
                if let (Some(Value::Array(old)), Some(Value::Array(new))) = (&previous, receipt) {
                    ensure!(new.starts_with(old), "completion receipt regressed");
                }
                let status = if status == "sending" && current == "uncertain" {
                    "uncertain"
                } else {
                    status
                };
                tx.execute("UPDATE events SET status=?,receipt=COALESCE(?,receipt),error=?,next_attempt=?,updated_at=? WHERE id=?",
                    params![status, receipt.as_ref().map(Value::to_string), reason, next_attempt, crate::now(), id])?;
                if status == "pending" {
                    let bark =
                        tx.query_row("SELECT kind='bark' FROM events WHERE id=?", [id], |r| {
                            r.get(0)
                        })?;
                    ready = Some((bot.clone(), bark));
                }
            }
        }
        Outcome::Command {
            bot,
            update,
            content,
            owner,
            reply,
            status,
            contexts,
        } => {
            ensure!(
                matches!(status.as_str(), "done" | "uncertain"),
                "invalid command completion"
            );
            let (payload, current): (String, String) = tx.query_row(
                "SELECT payload,status FROM updates WHERE profile=? AND update_id=?",
                params![bot, update],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )?;
            ensure!(
                crate::digest(payload) == *content,
                "completion update changed"
            );
            if current != "done" {
                crate::db::complete_update_transaction(
                    &tx, bot, *update, owner, reply, status, contexts,
                )?;
                if reply["text"].as_str().is_some_and(|s| !s.is_empty()) {
                    ready = Some((bot.clone(), false));
                }
            }
        }
    }
    tx.commit()?;
    if let Some((bot, bark)) = ready {
        store
            .runtime
            .delivery_ready(&bot, crate::optimization::Lane::delivery(bark));
    }
    Ok(())
}

/// False means not committed before stop/deadline; never permission to repeat
/// remote work. The claimed row stays for uncertain reconciliation.
pub(crate) async fn persist(
    store: &Store,
    record: Completion,
    stop: &mut watch::Receiver<bool>,
) -> Result<bool> {
    persist_for(store, record, stop, Duration::from_secs(75)).await
}

async fn persist_for(
    store: &Store,
    record: Completion,
    stop: &mut watch::Receiver<bool>,
    budget: Duration,
) -> Result<bool> {
    let record = std::sync::Arc::new(record);
    let deadline = tokio::time::Instant::now() + budget;
    let mut backoff = Duration::from_millis(250);
    loop {
        let record = record.clone();
        let attempt = store.blocking(move |store| apply(&store, &record));
        if *stop.borrow() {
            // Give an available database a brief chance to commit the final
            // result; a busy database cannot hold up worker retirement.
            return Ok(matches!(
                tokio::time::timeout(Duration::from_millis(250), attempt).await,
                Ok(Ok(()))
            ));
        }
        let result = tokio::select! {
            biased;
            _ = stop.changed() => return Ok(false),
            _ = tokio::time::sleep_until(deadline) => return Ok(false),
            result = attempt => result,
        };
        if result.is_ok() {
            return Ok(true);
        }
        if let Err(error) = result {
            crate::errors::log("completion-persistence", &error);
        }
        tokio::select! {
            biased;
            _ = stop.changed() => return Ok(false),
            _ = tokio::time::sleep_until(deadline) => return Ok(false),
            _ = tokio::time::sleep(backoff) => {},
        }
        backoff = (backoff * 2).min(Duration::from_secs(5));
    }
}

pub(crate) fn interrupt_bot(store: &Store, bot: &str) -> Result<()> {
    let mut db = store.connect()?;
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    tx.execute("UPDATE events SET status='uncertain',error='delivery worker interrupted',updated_at=? WHERE profile=? AND status='sending'", params![crate::now(),bot])?;
    tx.execute(
        "UPDATE updates SET status='uncertain' WHERE profile=? AND status='running'",
        [bot],
    )?;
    tx.commit()?;
    store.put(
        &format!("connection:{bot}"),
        &json!({"status":"restarting","at":crate::now()}),
    )
}

#[cfg(test)]
#[path = "completion_tests.rs"]
mod tests;
