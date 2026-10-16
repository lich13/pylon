//! Private, durable completion mailbox. Reconciliation never performs remote work.
use crate::db::Store;
use anyhow::{Context, Result, ensure};
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::{Mutex, OnceLock},
    time::Duration,
};
use tokio::sync::watch;

const LIMIT: u64 = 16 * 1024 * 1024;
static ACTIVE: OnceLock<Mutex<BTreeSet<PathBuf>>> = OnceLock::new();
// Serialize checkpoint replacement with commit/unlink, never SQLite lock waits.
static FILES: Mutex<()> = Mutex::new(());

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Completion {
    version: u32,
    nonce: String,
    result: Outcome,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(tag = "type", deny_unknown_fields)]
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
            version: 1,
            nonce: uuid::Uuid::new_v4().to_string(),
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
            version: 1,
            nonce: uuid::Uuid::new_v4().to_string(),
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

    fn name(&self) -> String {
        let key = match &self.result {
            Outcome::Delivery { id, bot, .. } => format!("delivery:{bot}:{id}"),
            Outcome::Command { bot, update, .. } => format!("command:{bot}:{update}"),
        };
        format!("{}.json", crate::digest(key))
    }
}

pub(crate) fn directory(store: &Store) -> Result<PathBuf> {
    let path = store
        .path
        .parent()
        .context("missing state directory")?
        .join("completions");
    match fs::DirBuilder::new().mode(0o700).create(&path) {
        Ok(()) => {
            File::open(path.parent().context("missing state directory")?)?.sync_all()?;
        }
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(e) => return Err(e.into()),
    }
    let metadata = fs::symlink_metadata(&path)?;
    ensure!(
        metadata.is_dir()
            && metadata.uid() == unsafe { libc::geteuid() }
            && metadata.permissions().mode() & 0o777 == 0o700,
        "unsafe completion directory"
    );
    Ok(path)
}

/// Inspect the private mailbox without creating it or reading result payloads.
/// A disappearing entry was reconciled concurrently; an unsafe entry is not empty.
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

/// Prevent the reconciler from finalizing a still-owned partial delivery.
pub(crate) struct Hold(PathBuf);
impl Hold {
    pub(crate) async fn delivery(
        store: &Store,
        event: &Value,
        stop: &mut watch::Receiver<bool>,
    ) -> Result<Self> {
        Self::new(
            store,
            &Completion::delivery(event, "sending", None, "", 0.0)?,
            stop,
        )
        .await
    }
    pub(crate) async fn new(
        store: &Store,
        result: &Completion,
        stop: &mut watch::Receiver<bool>,
    ) -> Result<Self> {
        let path = directory(store)?.join(result.name());
        let deadline = tokio::time::Instant::now() + Duration::from_secs(60);
        loop {
            {
                let mut active = ACTIVE
                    .get_or_init(Default::default)
                    .lock()
                    .map_err(|_| anyhow::anyhow!("completion ownership unavailable"))?;
                if active.insert(path.clone()) {
                    return Ok(Self(path));
                }
            }
            // Reconciliation may just have committed pending, allowing the next
            // claim before its file unlink completes. Wait for ownership transfer.
            if *stop.borrow() {
                return Err(crate::errors::ReconcileRequired.into());
            }
            tokio::select! {
                _ = stop.changed() => return Err(crate::errors::ReconcileRequired.into()),
                _ = tokio::time::sleep_until(deadline) => return Err(crate::errors::ReconcileRequired.into()),
                _ = tokio::time::sleep(Duration::from_millis(10)) => {},
            }
        }
    }
}
impl Drop for Hold {
    fn drop(&mut self) {
        if let Ok(mut active) = ACTIVE.get_or_init(Default::default).lock() {
            active.remove(&self.0);
        }
    }
}

fn read(path: &Path) -> Result<Completion> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let metadata = file.metadata()?;
    ensure!(
        metadata.is_file()
            && metadata.uid() == unsafe { libc::geteuid() }
            && metadata.nlink() == 1
            && metadata.permissions().mode() & 0o777 == 0o600
            && metadata.len() <= LIMIT,
        "unsafe completion file"
    );
    let mut bytes = Vec::new();
    (&mut file).take(LIMIT + 1).read_to_end(&mut bytes)?;
    ensure!(bytes.len() as u64 <= LIMIT, "completion too large");
    let value: Completion = serde_json::from_slice(&bytes)?;
    ensure!(
        value.version == 1
            && path.file_name().and_then(|s| s.to_str()) == Some(value.name().as_str()),
        "invalid completion identity"
    );
    Ok(value)
}

fn save(store: &Store, record: &Completion) -> Result<PathBuf> {
    let _files = FILES
        .lock()
        .map_err(|_| anyhow::anyhow!("completion writer unavailable"))?;
    let root = directory(store)?;
    let path = root.join(record.name());
    if fs::symlink_metadata(&path).is_ok() {
        read(&path)?;
    }
    let temporary = root.join(format!(".{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&temporary)?;
        let bytes = serde_json::to_vec(record)?;
        ensure!(bytes.len() as u64 <= LIMIT, "completion too large");
        file.write_all(&bytes)?;
        file.sync_all()?;
        fs::rename(&temporary, &path)?;
        File::open(&root)?.sync_all()?;
        Ok(())
    })();
    if temporary.exists() {
        fs::remove_file(&temporary)?;
    }
    result?;
    Ok(path)
}

fn apply(store: &Store, path: &Path, recovering: bool) -> Result<()> {
    let mut db = store.connect()?;
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let _files = FILES
        .lock()
        .map_err(|_| anyhow::anyhow!("completion writer unavailable"))?;
    // Read after acquiring the write transaction, so an older queued attempt cannot
    // overwrite a later checkpoint. Missing files were already committed/removed.
    let record = match read(path) {
        Ok(value) => value,
        Err(error)
            if error
                .downcast_ref::<std::io::Error>()
                .is_some_and(|e| e.kind() == std::io::ErrorKind::NotFound) =>
        {
            return Ok(());
        }
        Err(error) => return Err(error),
    };
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
                let status = if status == "sending" && (recovering || current == "uncertain") {
                    "uncertain"
                } else {
                    status
                };
                tx.execute("UPDATE events SET status=?,receipt=COALESCE(?,receipt),error=?,next_attempt=?,updated_at=? WHERE id=?",
                    params![status, receipt.as_ref().map(Value::to_string), reason, next_attempt, crate::now(), id])?;
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
            }
        }
    }
    tx.commit()?;
    // Keep partial checkpoints until the next outcome or reconciliation. A
    // blocking writer may finish after its async waiter times out; deleting this
    // checkpoint could otherwise leave an ownerless `sending` row forever.
    let partial = !recovering
        && matches!(&record.result, Outcome::Delivery { status, .. } if status == "sending");
    // A crash here is harmless: applying the same completed record is idempotent.
    if !partial && read(path).is_ok_and(|current| current.nonce == record.nonce) {
        fs::remove_file(path)?;
        File::open(path.parent().context("missing completion directory")?)?.sync_all()?;
    }
    Ok(())
}

/// False means durably deferred, not failed or permission to repeat remote work.
pub(crate) async fn persist(
    store: &Store,
    record: Completion,
    stop: &mut watch::Receiver<bool>,
) -> Result<bool> {
    persist_for(store, record, stop, Duration::from_secs(60)).await
}

async fn persist_for(
    store: &Store,
    record: Completion,
    stop: &mut watch::Receiver<bool>,
    budget: Duration,
) -> Result<bool> {
    let path = stage(store, record).await?;
    let deadline = tokio::time::Instant::now() + budget;
    let mut backoff = Duration::from_millis(250);
    loop {
        let path = path.clone();
        let attempt = store.blocking(move |store| apply(&store, &path, false));
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

pub(crate) async fn stage(store: &Store, record: Completion) -> Result<PathBuf> {
    let saved = store.clone();
    tokio::task::spawn_blocking(move || save(&saved, &record))
        .await
        .context("completion writer failed")?
}

pub(crate) fn reconcile(store: &Store) -> Result<()> {
    let root = directory(store)?;
    for entry in fs::read_dir(root)? {
        let path = entry?.path();
        if path.extension().and_then(|s| s.to_str()) != Some("json") {
            continue;
        }
        let mut active = ACTIVE
            .get_or_init(Default::default)
            .lock()
            .map_err(|_| anyhow::anyhow!("completion ownership unavailable"))?;
        if !active.insert(path.clone()) {
            continue;
        }
        drop(active);
        let _hold = Hold(path.clone());
        if let Err(error) = apply(store, &path, true) {
            crate::errors::log("completion-reconcile", &error);
            if crate::errors::retryable(&error) {
                return Err(error);
            }
        }
    }
    Ok(())
}

pub(crate) fn interrupt_bot(store: &Store, bot: &str) -> Result<()> {
    // Known results win over generic interrupted-state reconciliation.
    reconcile(store)?;
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
