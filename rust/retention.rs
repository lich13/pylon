//! Bounded, cooperative maintenance; no retention work runs on a request path.
use crate::{config::Config, db::Store};
use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
    sync::{
        Mutex, OnceLock,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::sync::watch;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Policy {
    pub enabled: bool,
    pub updates_per_bot: u32,
    pub runs_days: u32,
    pub events_days: u32,
    pub requests_days: u32,
    pub placeholders_days: u32,
    pub expired_days: u32,
}
impl Default for Policy {
    fn default() -> Self {
        Self {
            enabled: true,
            updates_per_bot: 5000,
            runs_days: 30,
            events_days: 7,
            requests_days: 14,
            placeholders_days: 90,
            expired_days: 1,
        }
    }
}
impl Policy {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            (1..=1_000_000).contains(&self.updates_per_bot)
                && [
                    self.runs_days,
                    self.events_days,
                    self.requests_days,
                    self.placeholders_days,
                    self.expired_days
                ]
                .iter()
                .all(|days| (1..=36500).contains(days)),
            "invalid retention policy"
        );
        Ok(())
    }
}

#[derive(Default, Serialize)]
struct Report {
    status: &'static str,
    started_at: Option<f64>,
    finished_at: Option<f64>,
    deleted: BTreeMap<String, usize>,
    written: BTreeMap<String, usize>,
    candidates: usize,
    batches: usize,
    max_batch_seconds: f64,
    error: Option<&'static str>,
}
#[derive(Default)]
pub(crate) struct Metrics {
    report: Mutex<Report>,
    pub hits: AtomicU64,
}
impl Metrics {
    pub fn snapshot(&self) -> Value {
        let report = self.report.lock().unwrap_or_else(|e| e.into_inner());
        let mut value = serde_json::to_value(&*report).unwrap_or(Value::Null);
        if report.status.is_empty() {
            value["status"] = json!("not_run");
        }
        value["placeholder_hits"] = json!(self.hits.load(Ordering::Relaxed));
        value
    }
    fn begin(&self) {
        *self.report.lock().unwrap_or_else(|e| e.into_inner()) = Report {
            status: "running",
            started_at: Some(crate::now()),
            ..Report::default()
        };
    }
    fn batch(&self, batch: &Batch, seconds: f64) {
        let mut report = self.report.lock().unwrap_or_else(|e| e.into_inner());
        report.batches += 1;
        report.candidates += batch.scanned;
        report.max_batch_seconds = report.max_batch_seconds.max(seconds);
        for (kind, count) in &batch.deleted {
            if kind == "placeholder_writes" {
                *report.written.entry("placeholders".into()).or_default() += count;
            } else {
                *report.deleted.entry(kind.clone()).or_default() += count;
            }
        }
        if batch.limited {
            report.error = Some("association_limit");
        }
    }
    fn finish(&self, status: &'static str, error: Option<&'static str>) {
        let mut report = self.report.lock().unwrap_or_else(|e| e.into_inner());
        report.status = status;
        report.finished_at = Some(crate::now());
        if error.is_some() {
            report.error = error;
        }
    }
}

fn bytes(path: &Path, absent_zero: bool) -> Value {
    match std::fs::symlink_metadata(path) {
        Ok(m) if m.file_type().is_symlink() => json!({"bytes":null,"error":"unsafe_path"}),
        Ok(m) => json!({"bytes":m.len(),"error":null}),
        Err(e) if absent_zero && e.kind() == std::io::ErrorKind::NotFound => {
            json!({"bytes":0,"error":null})
        }
        Err(_) => json!({"bytes":null,"error":"stat_unavailable"}),
    }
}
pub(crate) fn storage(db: &Path, run: &Path) -> Value {
    let mut wal = db.as_os_str().to_owned();
    wal.push("-wal");
    json!({"database":bytes(db,false),"wal":bytes(Path::new(&wal),true),"run_dir":bytes(run,false),"wal_checkpoint":crate::db::last_checkpoint(db),"sampled_at":crate::now()})
}
pub(crate) fn gateway_storage(config: &Config) -> Value {
    let mut value = storage(&config.db(), &config.home.join("run"));
    let summary = crate::queue::public(config);
    value["run_dir_entries"] = summary["metrics"]["storage"]["locks"]["directory_entries"].clone();
    value["run_dir_entries_sampled_at"] =
        summary["metrics"]["storage"]["locks"]["directory_sampled_at"].clone();
    value["run_dir_entries_scan_started_at"] =
        summary["metrics"]["storage"]["locks"]["directory_scan_started_at"].clone();
    value["run_dir_entries_error"] = summary["metrics"]["storage"]["locks"]["scan_error"].clone();
    if value["run_dir_entries"].is_null() && value["run_dir_entries_error"].is_null() {
        value["run_dir_entries_error"] = json!("not_sampled");
    }
    value
}

#[derive(Default)]
struct ExecutorMetrics {
    size: Value,
    retention: Value,
    locks: Value,
}
fn executor_metrics() -> &'static Mutex<BTreeMap<std::path::PathBuf, ExecutorMetrics>> {
    static METRICS: OnceLock<Mutex<BTreeMap<std::path::PathBuf, ExecutorMetrics>>> =
        OnceLock::new();
    METRICS.get_or_init(Default::default)
}
pub(crate) fn executor_storage(config: &Config) -> Value {
    let mut all = executor_metrics().lock().unwrap_or_else(|e| e.into_inner());
    let metrics = all.entry(config.home.clone()).or_default();
    if crate::now() - metrics.size["sampled_at"].as_f64().unwrap_or(0.0) >= 60.0 {
        metrics.size = storage(
            &config.home.join("supervisor/task-state.sqlite3"),
            &config.home.join("run"),
        );
    }
    let mut value = metrics.size.clone();
    value["retention"] = metrics.retention.clone();
    value["locks"] = metrics.locks.clone();
    value
}
pub(crate) fn publish_locks(config: &Config, locks: Value) {
    executor_metrics()
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .entry(config.home.clone())
        .or_default()
        .locks = locks;
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub(crate) enum Kind {
    Updates,
    Runs,
    Events,
    WebTokens,
    WebSessions,
    Contexts,
    History,
    Orphans,
    Requests,
}
impl Kind {
    fn table(self) -> &'static str {
        match self {
            Self::Updates => "updates",
            Self::Runs => "runs",
            Self::Events => "events",
            Self::WebTokens => "web_tokens",
            Self::WebSessions => "web_sessions",
            Self::Contexts => "contexts",
            Self::History | Self::Orphans => "kv",
            Self::Requests => "requests",
        }
    }
}
#[derive(Clone, Default)]
pub(crate) struct Cursor {
    row: i64,
    key: String,
}
#[derive(Default)]
pub(crate) struct Batch {
    pub scanned: usize,
    pub deleted: BTreeMap<String, usize>,
    pub done: bool,
    limited: bool,
}
impl Batch {
    fn add(&mut self, kind: &str, count: usize) {
        if count > 0 {
            *self.deleted.entry(kind.into()).or_default() += count;
        }
    }
}
const BATCH: usize = 500;
const ASSOCIATIONS: usize = 5000;

#[derive(Debug)]
struct CompletionPending;
impl std::fmt::Display for CompletionPending {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("completion_pending")
    }
}
impl std::error::Error for CompletionPending {}

pub(crate) fn batch(
    db: &mut Connection,
    kind: Kind,
    cursor: &mut Cursor,
    policy: &Policy,
    now: f64,
    limit: usize,
) -> Result<Batch> {
    let limit = limit.min(BATCH);
    ensure!(limit > 0, "invalid retention batch limit");
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    // A terminal row may still have a committed completion whose unlink failed.
    // Check after taking the writer lock: new work cannot become an eligible
    // terminal row between this check and deletion.
    if matches!(kind, Kind::Events | Kind::Updates)
        && crate::completion::pending_count(Path::new(tx.path().context("state path missing")?))?
            > 0
    {
        return Err(CompletionPending.into());
    }
    let mut result = Batch::default();
    let mut next = cursor.clone();
    if matches!(kind, Kind::History | Kind::Orphans) {
        // Two adjacent history prefixes share this exact ASCII range.
        let (lower, upper) = if kind == Kind::History {
            ("event-gone-", "event-gone;")
        } else {
            ("run-result:", "run-result;")
        };
        let mut q = tx.prepare(
            "SELECT key,value FROM kv WHERE key>=?1 AND key<?2 AND key>?3 ORDER BY key LIMIT ?4",
        )?;
        let rows = q
            .query_map(params![lower, upper, cursor.key, limit], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        result.scanned = rows.len();
        for (key, raw) in rows {
            next.key = key.clone();
            let delete = if kind == Kind::History {
                if !(key.starts_with(crate::event_history::PREFIX)
                    || key.starts_with(crate::event_history::MESSAGE_PREFIX))
                {
                    continue;
                }
                crate::event_history::expires(&key, &raw)? <= now
            } else {
                !tx.query_row(
                    "SELECT EXISTS(SELECT 1 FROM runs WHERE id=?)",
                    [key.trim_start_matches("run-result:")],
                    |r| r.get::<_, bool>(0),
                )?
            };
            if delete {
                result.add(
                    if kind == Kind::History {
                        "placeholders"
                    } else {
                        "run_results"
                    },
                    tx.execute("DELETE FROM kv WHERE key=?", [&key])?,
                );
            }
        }
    } else {
        // Select candidate primary keys before predicates. Every batch has a
        // bounded scan even when all the old rows must be retained.
        let sql = format!(
            "SELECT rowid FROM {} WHERE rowid>?1 ORDER BY rowid LIMIT ?2",
            kind.table()
        );
        let rows = tx
            .prepare(&sql)?
            .query_map(params![cursor.row, limit], |r| r.get::<_, i64>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        result.scanned = rows.len();
        let referenced = if kind == Kind::Events && !rows.is_empty() {
            let slots = std::iter::repeat_n("?", rows.len())
                .collect::<Vec<_>>()
                .join(",");
            let sql = format!(
                "SELECT DISTINCT event_id FROM run_events WHERE event_id IN (SELECT id FROM events WHERE rowid IN ({slots}))"
            );
            tx.prepare(&sql)?
                .query_map(rusqlite::params_from_iter(&rows), |r| r.get::<_, String>(0))?
                .collect::<rusqlite::Result<BTreeSet<_>>>()?
        } else {
            BTreeSet::new()
        };
        let mut cutoffs: BTreeMap<String, Option<i64>> = BTreeMap::new();
        for row in rows {
            next.row = row;
            match kind {
                Kind::Updates => {
                    let (bot, id, status): (String, i64, String) = tx.query_row(
                        "SELECT profile,update_id,status FROM updates WHERE rowid=?",
                        [row],
                        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                    )?;
                    if !matches!(status.as_str(), "done" | "failed" | "suppressed") {
                        continue;
                    }
                    let cutoff = if let Some(cutoff) = cutoffs.get(&bot) {
                        *cutoff
                    } else {
                        let cutoff = tx.query_row("SELECT update_id FROM updates WHERE profile=? ORDER BY update_id DESC LIMIT 1 OFFSET ?",params![bot,policy.updates_per_bot-1],|r|r.get::<_,i64>(0)).optional()?;
                        cutoffs.insert(bot, cutoff);
                        cutoff
                    };
                    if cutoff.is_some_and(|cutoff| id < cutoff) {
                        result.add(
                            "updates",
                            tx.execute("DELETE FROM updates WHERE rowid=?", [row])?,
                        );
                    }
                }
                Kind::Runs => {
                    let cutoff = now - f64::from(policy.runs_days) * 86400.0;
                    let id:Option<String> = tx.query_row("SELECT id FROM runs r WHERE rowid=? AND status IN ('ok','failed') AND started_at>=0 AND started_at<?2 AND finished_at>=started_at AND finished_at<?2 AND started_at<(SELECT max(started_at) FROM runs WHERE job_id=r.job_id)",params![row,cutoff],|r|r.get(0)).optional()?;
                    let Some(id) = id else {
                        continue;
                    };
                    let children = tx
                        .prepare("SELECT event_id FROM run_events WHERE run_id=? LIMIT 5001")?
                        .query_map([&id], |r| r.get::<_, String>(0))?
                        .collect::<rusqlite::Result<Vec<_>>>()?;
                    if children.len() + result.deleted.get("run_events").copied().unwrap_or(0)
                        > ASSOCIATIONS
                    {
                        result.limited = true;
                        continue;
                    }
                    result.add(
                        "run_events",
                        tx.execute("DELETE FROM run_events WHERE run_id=?", [&id])?,
                    );
                    result.add(
                        "run_results",
                        tx.execute("DELETE FROM kv WHERE key=?", [format!("run-result:{id}")])?,
                    );
                    result.add("runs", tx.execute("DELETE FROM runs WHERE id=?", [id])?);
                }
                Kind::Events => {
                    let cutoff = now - f64::from(policy.events_days) * 86400.0;
                    let event = tx.query_row("SELECT id,profile,module,job_id,kind,payload,status,attempts,created_at,updated_at,next_attempt,receipt,error FROM events WHERE rowid=? AND status IN ('delivered','failed','suppressed') AND created_at<?2 AND updated_at<?2 AND job_id<>'uncertain-summary' AND length(payload)<=1048576 AND (receipt IS NULL OR length(receipt)<=1048576)",params![row,cutoff],|r| {
                        let payload:String=r.get(5)?; let receipt:Option<String>=r.get(11)?;
                        Ok(json!({"id":r.get::<_,String>(0)?,"profile":r.get::<_,String>(1)?,"module":r.get::<_,String>(2)?,"job_id":r.get::<_,String>(3)?,"kind":r.get::<_,String>(4)?,"payload":serde_json::from_str::<Value>(&payload).unwrap_or(Value::Null),"status":r.get::<_,String>(6)?,"attempts":r.get::<_,i64>(7)?,"created_at":r.get::<_,f64>(8)?,"updated_at":r.get::<_,f64>(9)?,"next_attempt":r.get::<_,f64>(10)?,"receipt":receipt.map(|r|serde_json::from_str::<Value>(&r).unwrap_or(json!("invalid"))).unwrap_or(Value::Null),"error":r.get::<_,Option<String>>(12)?}))
                    }).optional()?;
                    let Some(event) = event else {
                        continue;
                    };
                    if referenced.contains(event["id"].as_str().unwrap_or("")) {
                        continue;
                    }
                    let Some(gone) =
                        crate::event_history::from_event(&event, now, policy.placeholders_days)
                    else {
                        continue;
                    };
                    crate::event_history::write(&tx, &gone)?;
                    result.add(
                        "placeholder_writes",
                        if gone.receipt["message_id"].is_number() {
                            2
                        } else {
                            1
                        },
                    );
                    result.add(
                        "events",
                        tx.execute("DELETE FROM events WHERE rowid=?", [row])?,
                    );
                }
                Kind::Requests => {
                    let cutoff = now - f64::from(policy.requests_days) * 86400.0;
                    // Older mailboxes discarded the aggregate result after handoff.
                    // A nonempty detail can represent uncertainty even when the
                    // process and callback both returned successfully; retain it.
                    let id:Option<String> = tx.query_row("SELECT id FROM requests WHERE rowid=? AND state='completed' AND created_at>=0 AND created_at<?2 AND updated_at>=created_at AND updated_at<?2 AND execution_status IN ('ok','failed','skipped') AND completion_status='ok' AND completion_input IS NULL AND report IS NULL AND token IS NULL AND detail=''",params![row,cutoff],|r|r.get(0)).optional()?;
                    let Some(id) = id else {
                        continue;
                    };
                    let children = tx
                        .prepare("SELECT id FROM triggers WHERE request_id=? LIMIT 5001")?
                        .query_map([&id], |r| r.get::<_, String>(0))?
                        .collect::<rusqlite::Result<Vec<_>>>()?;
                    if children.len() + result.deleted.get("triggers").copied().unwrap_or(0)
                        > ASSOCIATIONS
                    {
                        result.limited = true;
                        continue;
                    }
                    result.add(
                        "triggers",
                        tx.execute("DELETE FROM triggers WHERE request_id=?", [&id])?,
                    );
                    result.add(
                        "requests",
                        tx.execute("DELETE FROM requests WHERE id=?", [&id])?,
                    );
                }
                Kind::WebTokens | Kind::WebSessions | Kind::Contexts => {
                    let cutoff = now - f64::from(policy.expired_days) * 86400.0;
                    let predicate = match kind {
                        Kind::WebTokens => "(used_at IS NOT NULL OR expires_at<?2)",
                        Kind::WebSessions => "expires_at<?2",
                        _ => "expires_at>0 AND expires_at<?2",
                    };
                    result.add(
                        kind.table(),
                        tx.execute(
                            &format!(
                                "DELETE FROM {} WHERE rowid=?1 AND {predicate}",
                                kind.table()
                            ),
                            params![row, cutoff],
                        )?,
                    );
                }
                _ => unreachable!(),
            }
        }
    }
    tx.commit()?;
    result.done = result.scanned < limit;
    *cursor = if result.done { Cursor::default() } else { next };
    Ok(result)
}

fn error_code(error: &anyhow::Error) -> &'static str {
    if error.is::<CompletionPending>() {
        return "completion_pending";
    }
    if error.downcast_ref::<rusqlite::Error>().is_some_and(|e| {
        matches!(
            e.sqlite_error_code(),
            Some(rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked)
        )
    }) {
        "database_busy"
    } else {
        "retention_state_invalid"
    }
}
fn interrupted(config: &Config, stop: &watch::Receiver<bool>) -> bool {
    *stop.borrow() || config.home.join("run/freeze").exists()
}
async fn wait(stop: &mut watch::Receiver<bool>, seconds: f64) -> bool {
    if *stop.borrow() {
        return true;
    }
    tokio::select! { _=stop.changed()=>true, _=tokio::time::sleep(Duration::from_secs_f64(seconds))=>false }
}

async fn sweep(
    config: &Config,
    store: Option<&Store>,
    policy: &Policy,
    kinds: &[Kind],
    cursors: &mut BTreeMap<Kind, Cursor>,
    metrics: &Metrics,
    stop: &mut watch::Receiver<bool>,
) {
    metrics.begin();
    for kind in kinds {
        let mut scanned = 0;
        let mut deleted = 0;
        while scanned < 50_000 && deleted < 20_000 {
            if interrupted(config, stop) {
                metrics.finish("interrupted", None);
                return;
            }
            let kind = *kind;
            let mut cursor = cursors.get(&kind).cloned().unwrap_or_default();
            let config = config.clone();
            let store = store.cloned();
            let policy = policy.clone();
            let stopping = stop.clone();
            let started = Instant::now();
            let result = crate::blocking(move || {
                if interrupted(&config, &stopping) {
                    return Ok((None, cursor));
                }
                let mut db = match &store {
                    Some(store) => store.connect()?,
                    None => crate::queue::connect(&config)?,
                };
                let result = batch(
                    &mut db,
                    kind,
                    &mut cursor,
                    &policy,
                    crate::now(),
                    (20_000 - deleted).min(50_000 - scanned),
                )?;
                Ok((Some(result), cursor))
            })
            .await;
            match result {
                Ok((Some(batch), cursor)) => {
                    scanned += batch.scanned;
                    deleted += batch
                        .deleted
                        .get(match kind {
                            Kind::History => "placeholders",
                            Kind::Orphans => "run_results",
                            _ => kind.table(),
                        })
                        .copied()
                        .unwrap_or(0);
                    metrics.batch(&batch, started.elapsed().as_secs_f64());
                    cursors.insert(kind, cursor);
                    if batch.done {
                        break;
                    }
                }
                Ok((None, _)) => {
                    metrics.finish("interrupted", None);
                    return;
                }
                Err(error) => {
                    metrics.finish(
                        if error.is::<CompletionPending>() {
                            "deferred"
                        } else {
                            "failed"
                        },
                        Some(error_code(&error)),
                    );
                    return;
                }
            }
            if wait(stop, 0.05).await {
                metrics.finish("interrupted", None);
                return;
            }
        }
    }
    metrics.finish("completed", None);
}

fn wal_threshold(config: &Config) -> u64 {
    config
        .settings()
        .map(|s| s.admission.wal_checkpoint_bytes)
        .unwrap_or(32 * 1024 * 1024)
}
fn maybe_checkpoint(config: &Config, path: &Path, force: bool) {
    let _ =
        crate::db::checkpoint_database(config.home.as_path(), path, wal_threshold(config), force);
}

pub(crate) async fn gateway(app: crate::api::App, mut stop: watch::Receiver<bool>) {
    let mut cursors = BTreeMap::new();
    let mut next_orphans = 0.0;
    if wait(&mut stop, 600.0).await {
        return;
    }
    loop {
        let policy = app.snapshot.read().await.settings.retention.clone();
        if policy.enabled && !interrupted(&app.config, &stop) {
            let mut kinds = vec![
                Kind::Updates,
                Kind::Runs,
                Kind::Events,
                Kind::WebTokens,
                Kind::WebSessions,
                Kind::Contexts,
                Kind::History,
            ];
            let scan_orphans = crate::now() >= next_orphans;
            if scan_orphans {
                kinds.push(Kind::Orphans);
            }
            sweep(
                &app.config,
                Some(&app.store),
                &policy,
                &kinds,
                &mut cursors,
                &app.store.runtime.retention,
                &mut stop,
            )
            .await;
            if scan_orphans && app.store.runtime.retention.snapshot()["status"] == "completed" {
                next_orphans = crate::now() + 86400.0;
            }
            if !interrupted(&app.config, &stop) {
                let path = app.config.db();
                let config = app.config.clone();
                let _ = crate::blocking(move || {
                    maybe_checkpoint(&config, &path, false);
                    Ok(())
                })
                .await;
            }
        }
        if wait(&mut stop, 3600.0).await {
            break;
        }
    }
}
pub(crate) async fn executor(config: Config, mut stop: watch::Receiver<bool>) {
    let mut cursors = BTreeMap::new();
    let metrics = Metrics::default();
    let mut locks = crate::lock_reaper::Scan::default();
    let mut saw_freeze = config.home.join("run/freeze").exists();
    if wait(&mut stop, 600.0).await {
        return;
    }
    loop {
        let frozen = interrupted(&config, &stop);
        let thawed = saw_freeze && !frozen;
        saw_freeze = frozen;
        if !frozen {
            let read = config.clone();
            match crate::blocking(move || Ok(read.settings()?.retention)).await {
                Ok(policy) if policy.enabled => {
                    sweep(
                        &config,
                        None,
                        &policy,
                        &[Kind::Requests],
                        &mut cursors,
                        &metrics,
                        &mut stop,
                    )
                    .await;
                    let report = metrics.snapshot();
                    let completed = report["status"] == "completed";
                    let continue_locks = locks.in_progress() || completed || thawed;
                    executor_metrics()
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .entry(config.home.clone())
                        .or_default()
                        .retention = report;
                    // Keep reclaiming while a directory scan is unfinished, and always
                    // after thaw so overnight freezes do not wait for the next hour.
                    if completed || continue_locks {
                        locks = crate::lock_reaper::run(&config, locks, &mut stop).await;
                    }
                    let force = thawed || crate::queue::take_post_recovery_checkpoint();
                    let path = config.home.join("supervisor/task-state.sqlite3");
                    let config_copy = config.clone();
                    let outcome = crate::blocking(move || {
                        Ok(crate::db::checkpoint_database(
                            config_copy.home.as_path(),
                            &path,
                            wal_threshold(&config_copy),
                            force,
                        ))
                    })
                    .await;
                    if force
                        && let Ok(result) = outcome
                        && !matches!(
                            result["status"].as_str(),
                            Some("truncated" | "skipped_frozen" | "skipped_small" | "missing")
                        )
                    {
                        crate::queue::request_post_recovery_checkpoint();
                    }
                }
                Ok(_) => {}
                Err(_) => {
                    metrics.finish("failed", Some("configuration_invalid"));
                    executor_metrics()
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .entry(config.home.clone())
                        .or_default()
                        .retention = metrics.snapshot();
                }
            }
        }
        // Unfinished lock scans, post-thaw catch-up, and pending WAL shrink retry soon.
        let pause =
            if locks.in_progress() || thawed || crate::queue::post_recovery_checkpoint_pending() {
                60.0
            } else {
                3600.0
            };
        if wait(&mut stop, pause).await {
            break;
        }
    }
}

#[cfg(test)]
#[path = "retention_tests.rs"]
mod tests;
