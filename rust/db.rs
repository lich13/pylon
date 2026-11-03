use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    ops::{Deref, DerefMut},
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

static BUSY: AtomicU64 = AtomicU64::new(0);
pub fn busy_count() -> u64 {
    BUSY.load(Ordering::Relaxed)
}
fn wait_for_writer(attempt: i32) -> bool {
    if attempt == 0 {
        BUSY.fetch_add(1, Ordering::Relaxed);
    }
    if attempt >= 100 {
        return false;
    }
    std::thread::sleep(Duration::from_millis(50));
    true
}

/// Root may only hand a root-owned, singly linked regular file back to the
/// non-root owner of its directory (the gateway service account).
fn should_align(directory: (u32, u32), file: (u32, u64, bool)) -> Option<(u32, u32)> {
    let (dir_uid, dir_gid) = directory;
    let (file_uid, links, regular) = file;
    (dir_uid != 0 && file_uid == 0 && links == 1 && regular).then_some((dir_uid, dir_gid))
}

/// The gateway service owns its private state database, but root processes
/// (broker, CLI) open it too. If root created the file or a WAL sidecar, give
/// it back to the directory owner so the gateway never loses write access.
/// Mode stays 0600: no other account gains access.
pub(crate) fn align_owner(path: &Path) -> Result<()> {
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    let parent = std::fs::symlink_metadata(path.parent().context("missing database directory")?)?;
    if !parent.is_dir() || parent.uid() == 0 {
        return Ok(());
    }
    let mut candidates = vec![path.to_path_buf()];
    for suffix in ["-wal", "-shm", "-journal"] {
        let mut name = path.as_os_str().to_owned();
        name.push(suffix);
        candidates.push(name.into());
    }
    for candidate in candidates {
        let file = match std::fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
            .open(&candidate)
        {
            Ok(file) => file,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => return Err(e.into()),
        };
        // Decide on the opened inode, so a swapped name cannot redirect fchown.
        let meta = file.metadata()?;
        if let Some((uid, gid)) = should_align(
            (parent.uid(), parent.gid()),
            (meta.uid(), meta.nlink(), meta.is_file()),
        ) {
            std::os::unix::fs::fchown(&file, Some(uid), Some(gid))?;
        }
    }
    Ok(())
}

/// Distinct chats in receipts of deliveries the bot completed.
fn source_chats(db: &Connection, bot: &str) -> Result<BTreeSet<String>> {
    let mut query = db.prepare(
        "SELECT receipt FROM events WHERE profile=? AND status='delivered' AND receipt IS NOT NULL",
    )?;
    let mut chats = BTreeSet::new();
    for receipt in query.query_map([bot], |r| r.get::<_, String>(0))? {
        let Ok(receipt) = serde_json::from_str::<Value>(&receipt?) else {
            continue;
        };
        let parts = match receipt {
            Value::Array(parts) => parts,
            other => vec![other],
        };
        for part in parts {
            let chat = crate::telegram::numeric_id(&part["chat_id"]);
            if !chat.is_empty() {
                chats.insert(chat);
            }
        }
    }
    Ok(chats)
}

/// Runtime state key families that belong to one bot. Delivery history and
/// shared rows are deliberately absent.
const BOT_STATE_PREFIXES: &[&str] = &[
    "connection:",
    "menu:",
    "menu-state:",
    "offset:",
    "bot_started:",
];
const BOT_LANES: &[&str] = &["bark", "command", "menu", "poll", "telegram"];
pub(crate) fn bot_state_key(key: &str) -> Option<&str> {
    for prefix in BOT_STATE_PREFIXES {
        if let Some(bot) = key.strip_prefix(prefix) {
            return crate::identifier(bot).then_some(bot);
        }
    }
    for prefix in ["worker:", "delivery-timing:"] {
        if let Some((bot, lane)) = key.strip_prefix(prefix).and_then(|k| k.split_once(':'))
            && BOT_LANES.contains(&lane)
            && crate::identifier(bot)
        {
            return Some(bot);
        }
    }
    None
}
#[derive(Debug, Default, serde::Serialize)]
pub struct StaleState {
    pub kv: Vec<String>,
    /// Finished updates that would be removed, by bot.
    pub updates: BTreeMap<String, i64>,
    /// Pending or running updates that are kept, by bot.
    pub updates_kept: BTreeMap<String, i64>,
}
pub(crate) struct RetargetRule<'a> {
    pub target: &'a str,
    pub target_owner: &'a str,
    /// Configured and enabled bots; their deliveries never move.
    pub live: &'a [String],
    /// Configured owners of sources that are still configured.
    pub known_owners: &'a BTreeMap<String, String>,
}
/// Selects deliveries by id or, at apply time, every pending one of a bot.
#[derive(Clone, Debug, Default, serde::Serialize, serde::Deserialize)]
pub struct Selection {
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub ids: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub from_bot: Option<String>,
}
impl Selection {
    fn resolve(&self, db: &Connection) -> Result<Vec<String>> {
        let mut ids = self.ids.clone();
        if let Some(bot) = &self.from_bot {
            for id in ids_for_bot(db, bot, "pending")? {
                if !ids.contains(&id) {
                    ids.push(id);
                }
            }
        }
        Ok(ids)
    }
}
#[derive(Clone, Debug, Default, serde::Serialize, serde::Deserialize)]
pub struct RetargetStep {
    #[serde(flatten)]
    pub select: Selection,
    pub to: String,
    #[serde(skip)]
    pub to_owner: String,
}
impl RetargetStep {
    fn resolve(&self, db: &Connection) -> Result<Vec<String>> {
        self.select.resolve(db)
    }
}
#[derive(Clone, Debug, Default, serde::Serialize, serde::Deserialize)]
pub struct DiscardStep {
    #[serde(flatten)]
    pub select: Selection,
    pub reason: String,
}
impl DiscardStep {
    fn resolve(&self, db: &Connection) -> Result<Vec<String>> {
        self.select.resolve(db)
    }
}
/// Database part of an operator change. Applied after configuration reload.
#[derive(Clone, Debug, Default, serde::Serialize, serde::Deserialize)]
pub struct OperatorPlan {
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub retarget: Vec<RetargetStep>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub discard: Vec<DiscardStep>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub delete_kv: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub delete_updates_for: Vec<String>,
}
impl OperatorPlan {
    pub fn is_empty(&self) -> bool {
        self.retarget.is_empty()
            && self.discard.is_empty()
            && self.delete_kv.is_empty()
            && self.delete_updates_for.is_empty()
    }
}
fn ids_for_bot(db: &Connection, bot: &str, status: &str) -> Result<Vec<String>> {
    let mut query =
        db.prepare("SELECT id FROM events WHERE status=? AND profile=? ORDER BY created_at,id")?;
    let ids = query
        .query_map([status, bot], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(ids)
}
fn resolution_on(db: &Connection, id: &str) -> Result<Value> {
    let old: Option<String> = db
        .query_row(
            "SELECT value FROM kv WHERE key=?",
            [format!("{RESOLUTION_PREFIX}{id}")],
            |r| r.get(0),
        )
        .optional()?;
    Ok(old
        .and_then(|v| serde_json::from_str::<Value>(&v).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({})))
}
fn put_on(db: &Connection, key: &str, value: &Value) -> Result<()> {
    db.execute(
        "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        params![key, value.to_string()],
    )?;
    Ok(())
}
/// Operator output carries metadata only; receipts can name chats.
fn public_event(mut event: Value) -> Value {
    if let Some(object) = event.as_object_mut() {
        let receipt = object.remove("receipt").unwrap_or(Value::Null);
        object.insert("has_receipt".into(), json!(!receipt.is_null()));
    }
    event
}

pub(crate) const RESOLUTION_PREFIX: &str = "delivery-resolution:";
pub const DELIVERY_STATUSES: &[&str] = &[
    "pending",
    "sending",
    "delivered",
    "uncertain",
    "failed",
    "suppressed",
];
#[derive(Clone)]
pub struct Store {
    pub path: PathBuf,
    pool: Arc<Mutex<Vec<Connection>>>,
    pub(crate) runtime: Arc<crate::optimization::Runtime>,
}
impl Store {
    pub fn new(path: &Path) -> Result<Self> {
        std::fs::create_dir_all(path.parent().context("missing database directory")?)?;
        let store = Self {
            path: path.to_path_buf(),
            pool: Arc::new(Mutex::new(Vec::new())),
            runtime: Arc::default(),
        };
        {
            let db = store.connect()?;
            db.execute_batch(include_str!("schema.sql"))?;
            internal_state(&db)?;
        }
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
        if unsafe { libc::geteuid() } == 0 {
            align_owner(path)?;
        }
        Ok(store)
    }
    pub fn connect(&self) -> Result<PooledConnection> {
        connect(&self.path, &self.pool)
    }
    pub async fn blocking<T: Send + 'static>(
        &self,
        operation: impl FnOnce(Store) -> Result<T> + Send + 'static,
    ) -> Result<T> {
        let store = self.clone();
        crate::blocking(move || operation(store)).await
    }

    pub fn get(&self, key: &str) -> Result<Value> {
        let value: Option<String> = self
            .connect()?
            .query_row("SELECT value FROM kv WHERE key=?", [key], |r| r.get(0))
            .optional()?;
        Ok(value
            .map(|v| serde_json::from_str(&v))
            .transpose()?
            .unwrap_or(Value::Null))
    }
    pub fn put(&self, key: &str, value: &Value) -> Result<()> {
        self.connect()?.execute(
            "INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            params![key, value.to_string()],
        )?;
        Ok(())
    }
    pub fn recover(&self) -> Result<()> {
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let interrupted = {
            let mut query = tx.prepare("SELECT profile,group_concat(update_id),count(*) FROM updates WHERE status='running' GROUP BY profile")?;
            query
                .query_map([], |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, i64>(2)?,
                    ))
                })?
                .collect::<std::result::Result<Vec<_>, _>>()?
        };
        tx.execute("UPDATE events SET status='uncertain',error='sender restarted before acknowledgement',updated_at=? WHERE status='sending'",[crate::now()])?;
        tx.execute(
            "UPDATE updates SET status='uncertain' WHERE status='running'",
            [],
        )?;
        for (bot, ids, count) in &interrupted {
            let id = crate::digest(format!("{bot}:recovery:{ids}"));
            let payload = json!({"text":format!("服务重启，{count} 条操作结果待核对。请打开对应入口查看已有记录；没有重复执行。"),"expires_at":crate::now()+600.0});
            tx.execute("INSERT OR IGNORE INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,module) VALUES(?,?,'system','text',?,'pending',0,0,?,?,'gateway')", params![&id[..32],bot,payload.to_string(),crate::now(),crate::now()])?;
        }
        tx.commit()?;
        for (bot, _, _) in interrupted {
            self.runtime
                .delivery_ready(&bot, crate::optimization::Lane::Telegram);
        }
        Ok(())
    }
    pub fn uncertain_digest(&self, target: &str) -> Result<()> {
        let day = utc8_day(std::time::SystemTime::now());
        self.uncertain_digest_day(target, &day)
    }
    pub fn uncertain_digest_day(&self, target: &str, day: &str) -> Result<()> {
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        initialize_uncertain(&tx)?;
        let value: String = tx.query_row(
            "SELECT value FROM kv WHERE key='uncertain:counter:v2'",
            [],
            |r| r.get(0),
        )?;
        let mut counter: Value = serde_json::from_str(&value)?;
        let key = format!("uncertain:summary:{day}");
        let event_id = crate::digest(&key)[..32].to_owned();
        let sent: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM kv WHERE key=?) OR EXISTS(SELECT 1 FROM events WHERE id=?)",
            params![key, event_id], |r| r.get(0),
        )?;
        let pending = counter["pending"].as_u64().unwrap_or(0);
        let inserted = pending > 0 && counter["last_day"] != day && !sent;
        if inserted {
            let payload = json!({"text":format!("Pylon 有 {pending} 条新增投递结果待核对。原消息未自动重发，可在状态页查看。"),"expires_at":crate::now()+600.0});
            tx.execute("INSERT INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,module) VALUES(?,?,'uncertain-summary','text',?,'pending',0,0,?,?,'gateway')",params![event_id,target,payload.to_string(),crate::now(),crate::now()])?;
            counter["pending"] = json!(0);
            counter["last_day"] = json!(day);
        }
        tx.execute("INSERT INTO kv VALUES('uncertain:counter:v2',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [counter.to_string()])?;
        tx.commit()?;
        if inserted {
            self.runtime
                .delivery_ready(target, crate::optimization::Lane::Telegram);
        }
        Ok(())
    }
    pub fn configuration_rejected(&self, fingerprint: &str, target: Option<&str>) -> Result<()> {
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let value = json!({"fingerprint":fingerprint,"at":crate::now()});
        tx.execute("INSERT INTO kv VALUES('configuration-rejected',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [value.to_string()])?;
        if let Some(target) = target {
            let id = crate::digest(format!("configuration-rejected:{fingerprint}"));
            let payload = json!({"text":"Pylon 配置未能加载，继续使用上一有效配置。可在状态页查看诊断。","expires_at":crate::now()+600.0});
            tx.execute("INSERT OR IGNORE INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,module) VALUES(?,?,'system','text',?,'pending',0,0,?,?,'gateway')",params![&id[..32],target,payload.to_string(),crate::now(),crate::now()])?;
        }
        tx.commit()?;
        if let Some(target) = target {
            self.runtime
                .delivery_ready(target, crate::optimization::Lane::Telegram);
        }
        Ok(())
    }
    /// The last alert is sufficient for both restart-safe deduplication and the
    /// actual minimum repeat interval. Do not accumulate one KV key per bucket.
    pub fn freeze_alert(
        &self,
        target: &str,
        age_seconds: u64,
        started_at: u64,
        repeat: u64,
    ) -> Result<()> {
        self.freeze_alert_at(target, age_seconds, started_at, repeat, crate::now())
    }
    fn freeze_alert_at(
        &self,
        target: &str,
        age_seconds: u64,
        started_at: u64,
        repeat: u64,
        now: f64,
    ) -> Result<()> {
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let last: Option<String> = tx
            .query_row(
                "SELECT value FROM kv WHERE key='freeze-alert:last'",
                [],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(raw) = last {
            let last: Value = serde_json::from_str(&raw)?;
            // Recognize the previous release's timestamp:bucket fingerprint.
            let previous_start = last["started_at"].as_u64().or_else(|| {
                last["fingerprint"]
                    .as_str()?
                    .split(':')
                    .next()?
                    .parse()
                    .ok()
            });
            if previous_start == Some(started_at)
                && now - last["at"].as_f64().context("invalid freeze alert time")? < repeat as f64
            {
                return Ok(());
            }
        }
        let minutes = age_seconds / 60;
        let id = crate::digest(format!("freeze-alert:{started_at}:{now}"));
        let payload = json!({"text":format!("Pylon 新触发仍处于冻结状态，已持续约 {minutes} 分钟。升级成功后可解冻；失败升级应保留冻结并先排查。"),"expires_at":now+600.0});
        let record = json!({"at":now,"age_seconds":age_seconds,"started_at":started_at,"fingerprint":format!("{started_at}:{}", age_seconds / repeat.max(1))});
        tx.execute("INSERT INTO kv VALUES('freeze-alert:last',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [record.to_string()])?;
        tx.execute("INSERT OR IGNORE INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,module) VALUES(?,?,'freeze-alert','text',?,'pending',0,0,?,?,'gateway')", params![&id[..32],target,payload.to_string(),now,now])?;
        tx.commit()?;
        self.runtime
            .delivery_ready(target, crate::optimization::Lane::Telegram);
        Ok(())
    }
    pub fn complete_update(
        &self,
        bot: &str,
        update: i64,
        owner: &str,
        reply: &Value,
        status: &str,
        contexts: &[String],
    ) -> Result<()> {
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        complete_update_transaction(
            &tx,
            bot,
            update,
            owner,
            reply,
            status,
            &contexts.iter().map(crate::digest).collect::<Vec<_>>(),
        )?;
        tx.commit()?;
        if reply["text"].as_str().is_some_and(|s| !s.is_empty()) {
            self.runtime
                .delivery_ready(bot, crate::optimization::Lane::Telegram);
        }
        Ok(())
    }
    pub fn has_event(&self, id: &str) -> Result<bool> {
        let db = self.connect()?;
        let exists: bool = db.query_row(
            "SELECT EXISTS(SELECT 1 FROM events WHERE id=?)",
            [id],
            |r| r.get(0),
        )?;
        Ok(exists || self.history(&db, id)?.is_some())
    }
    fn history(&self, db: &Connection, id: &str) -> Result<Option<crate::event_history::Gone>> {
        let gone = crate::event_history::read(db, id, crate::now())?;
        if gone.is_some() {
            self.runtime.retention.hits.fetch_add(1, Ordering::Relaxed);
        }
        Ok(gone)
    }
    pub(crate) fn retained_submit(&self, module: &str, value: &Value) -> Result<Option<Value>> {
        let mut db = self.connect()?;
        let tx = db.transaction()?;
        let id = value["id"].as_str().context("event id required")?;
        ensure!(crate::identifier(id) && id.len() <= 64, "invalid event id");
        ensure!(
            matches!(value["kind"].as_str(), Some("text" | "document" | "bark")),
            "invalid event kind"
        );
        if let Some(gone) = self.history(&tx, id)? {
            gone.verify_submit(module, &value["payload"])?;
            return Ok(Some(gone.result()));
        }
        if value["kind"] == "bark" {
            let existing: Option<(String, String, String)> = tx
                .query_row(
                    "SELECT module,payload,job_id FROM events WHERE id=?",
                    [id],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .optional()?;
            if let Some((owner, payload, job)) = existing {
                ensure!(owner == module, "event belongs to another module");
                ensure!(
                    job == "cancelled"
                        || serde_json::from_str::<Value>(&payload)? == value["payload"],
                    "event id already has different content"
                );
                return self.event_on(&tx, id);
            }
        }
        Ok(None)
    }
    pub fn submit(&self, module: &str, value: &Value, bot: &str) -> Result<Value> {
        let id = value["id"].as_str().context("event id required")?;
        ensure!(crate::identifier(id) && id.len() <= 64, "invalid event id");
        let kind = value["kind"].as_str().context("kind required")?;
        ensure!(
            ["text", "document", "bark"].contains(&kind),
            "invalid event kind"
        );
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let existing: Option<(String, String, String)> = tx
            .query_row(
                "SELECT module,payload,job_id FROM events WHERE id=?",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()?;
        let inserted = existing.is_none();
        if let Some((owner, payload, job)) = existing {
            ensure!(owner == module, "event belongs to another module");
            let original: Value = serde_json::from_str(&payload)?;
            ensure!(
                job == "cancelled" || original == value["payload"],
                "event id already has different content"
            );
        } else if let Some(gone) = self.history(&tx, id)? {
            gone.verify_submit(module, &value["payload"])?;
            return Ok(gone.result());
        } else {
            crate::event_history::remove_expired(&tx, id, crate::now())?;
            tx.execute("INSERT INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,module) VALUES(?,?,?,?,?,'pending',0,?,?,?,?)",
                params![id,bot,value["job_id"].as_str().unwrap_or("manual"),kind,value["payload"].to_string(),value["next_attempt"].as_f64().unwrap_or(0.0),value["created_at"].as_f64().unwrap_or(crate::now()),crate::now(),module])?;
        }
        tx.commit()?;
        if inserted {
            self.runtime
                .delivery_ready(bot, crate::optimization::Lane::delivery(kind == "bark"));
        }
        self.event(id)
    }
    pub fn event(&self, id: &str) -> Result<Value> {
        let db = self.connect()?;
        self.event_on(&db, id)?.context("unknown event")
    }
    fn event_on(&self, db: &Connection, id: &str) -> Result<Option<Value>> {
        let event = db.query_row("SELECT id,profile,status,attempts,next_attempt,updated_at,receipt,error,module FROM events WHERE id=?",[id],|r|{
            let receipt:Option<String>=r.get(6)?;
            Ok(json!({"id":r.get::<_,String>(0)?,"profile":r.get::<_,String>(1)?,"status":r.get::<_,String>(2)?,"attempts":r.get::<_,i64>(3)?,"next_attempt":r.get::<_,f64>(4)?,"updated_at":r.get::<_,f64>(5)?,"receipt":receipt.and_then(|s|serde_json::from_str::<Value>(&s).ok()),"error":r.get::<_,Option<String>>(7)?,"module":r.get::<_,String>(8)?}))
        }).optional()?;
        match event {
            Some(mut event) => {
                // Present only after an operator resolved an uncertain delivery.
                let resolution: Option<String> = db
                    .query_row(
                        "SELECT value FROM kv WHERE key=?",
                        [format!("{RESOLUTION_PREFIX}{id}")],
                        |r| r.get(0),
                    )
                    .optional()?;
                if let Some(value) = resolution.and_then(|v| serde_json::from_str::<Value>(&v).ok())
                {
                    event["resolution"] = value;
                }
                Ok(Some(event))
            }
            None => Ok(self.history(db, id)?.map(|g| g.result())),
        }
    }
    /// Delivery metadata for operators. Payloads and receipts are never read.
    pub fn deliveries(&self, status: &str, limit: usize) -> Result<Vec<Value>> {
        ensure!(
            DELIVERY_STATUSES.contains(&status),
            "invalid delivery status"
        );
        let db = self.connect()?;
        let mut query = db.prepare("SELECT e.id,e.profile,e.module,e.job_id,e.kind,e.status,e.error,e.attempts,e.created_at,e.updated_at,e.receipt IS NOT NULL,CASE WHEN json_valid(e.receipt) AND json_type(e.receipt)='array' THEN json_array_length(e.receipt) WHEN e.receipt IS NOT NULL THEN 1 ELSE 0 END,k.value FROM events e LEFT JOIN kv k ON k.key='delivery-resolution:'||e.id WHERE e.status=? ORDER BY e.updated_at,e.id LIMIT ?")?;
        let rows = query
            .query_map(params![status, limit.clamp(1, 1000)], |r| {
                let resolution: Option<String> = r.get(12)?;
                Ok(json!({"id":r.get::<_,String>(0)?,"bot":r.get::<_,String>(1)?,"module":r.get::<_,String>(2)?,"job_id":r.get::<_,String>(3)?,"kind":r.get::<_,String>(4)?,"status":r.get::<_,String>(5)?,"error":r.get::<_,Option<String>>(6)?,"attempts":r.get::<_,i64>(7)?,"created_at":r.get::<_,f64>(8)?,"updated_at":r.get::<_,f64>(9)?,"has_receipt":r.get::<_,bool>(10)?,"receipt_parts":r.get::<_,i64>(11)?,"resolution":resolution.and_then(|v|serde_json::from_str::<Value>(&v).ok())}))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }
    /// Operator resolution of one uncertain delivery, in one transaction.
    /// `confirm` marks it delivered, `discard` suppresses it, and `resend`
    /// returns it to pending once; a resend can duplicate a message.
    pub fn resolve_delivery(&self, id: &str, action: &str, actor: &str) -> Result<Value> {
        let (status, error) = match action {
            "confirm" => ("delivered", "operator confirmed delivered"),
            "discard" => ("suppressed", "operator discarded"),
            "resend" => ("pending", "operator resend"),
            _ => anyhow::bail!("invalid delivery resolution"),
        };
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let row: Option<(String, Option<String>, String, String)> = tx
            .query_row(
                "SELECT status,error,profile,kind FROM events WHERE id=?",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .optional()?;
        let (previous, previous_error, bot, kind) = row.context("unknown event")?;
        ensure!(
            previous == "uncertain",
            "only uncertain deliveries can be resolved"
        );
        let key = format!("{RESOLUTION_PREFIX}{id}");
        let old: Option<String> = tx
            .query_row("SELECT value FROM kv WHERE key=?", [&key], |r| r.get(0))
            .optional()?;
        let old = old
            .and_then(|v| serde_json::from_str::<Value>(&v).ok())
            .unwrap_or(Value::Null);
        let resends = old["resend_count"].as_u64().unwrap_or(0);
        ensure!(
            action != "resend" || resends == 0,
            "delivery was already resent once"
        );
        let now = crate::now();
        let changed = tx.execute(
            "UPDATE events SET status=?,error=?,next_attempt=?,updated_at=? WHERE id=? AND status='uncertain'",
            params![status, error, now, now, id],
        )?;
        ensure!(changed == 1, "delivery changed concurrently");
        let resolution = json!({"resolution":action,"resolved_at":now,"resolved_by":actor,
            "previous_status":previous,"previous_error":previous_error,
            "resend_count":resends + u64::from(action == "resend")});
        tx.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            params![key, resolution.to_string()],
        )?;
        let mut result = self.event_on(&tx, id)?.context("unknown event")?;
        tx.commit()?;
        // Operator output carries metadata only; receipts can name chats.
        if let Some(object) = result.as_object_mut() {
            let receipt = object.remove("receipt").unwrap_or(Value::Null);
            object.insert("has_receipt".into(), json!(!receipt.is_null()));
        }
        if action == "resend" {
            self.runtime
                .delivery_ready(&bot, crate::optimization::Lane::delivery(kind == "bark"));
        }
        Ok(result)
    }
    /// Moves pending deliveries whose bot is no longer live (removed from
    /// configuration, or disabled) to another enabled bot, in one transaction
    /// (all or nothing). Only untouched text/document items without receipts
    /// move, and only to the chat the source bot delivered to: `known_owners`
    /// gives the configured owner of a still-configured source; otherwise every
    /// chat in the source's receipts must equal `target_owner`. The destination
    /// chat never changes and nothing already sent is re-sent. Operator output
    /// carries metadata only.
    pub fn retarget_deliveries(
        &self,
        ids: &[String],
        target: &str,
        target_owner: &str,
        live: &[String],
        actor: &str,
    ) -> Result<Vec<Value>> {
        let rule = RetargetRule {
            target,
            target_owner,
            live,
            known_owners: &BTreeMap::new(),
        };
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let results = self.retarget_on(&tx, ids, &rule, actor)?;
        tx.commit()?;
        self.runtime
            .delivery_ready(target, crate::optimization::Lane::delivery(false));
        Ok(results)
    }
    pub(crate) fn retarget_on(
        &self,
        tx: &Connection,
        ids: &[String],
        rule: &RetargetRule<'_>,
        actor: &str,
    ) -> Result<Vec<Value>> {
        let target = rule.target;
        ensure!(!ids.is_empty(), "no deliveries selected");
        ensure!(
            rule.live.iter().any(|b| b == target) && !rule.target_owner.is_empty(),
            "target bot is not configured"
        );
        let now = crate::now();
        let mut checked_sources = BTreeSet::new();
        let mut results = Vec::new();
        for id in ids {
            let row: Option<(String, String, String, Option<String>, String)> = tx
                .query_row(
                    "SELECT status,profile,kind,receipt,payload FROM events WHERE id=?",
                    [id],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
                )
                .optional()?;
            let (status, source, kind, receipt, payload) = row.context("unknown event")?;
            ensure!(
                status == "pending",
                "only pending deliveries can be retargeted"
            );
            ensure!(source != target, "delivery already uses the target bot");
            ensure!(
                !rule.live.contains(&source),
                "source bot is still configured"
            );
            ensure!(
                kind == "text" || kind == "document",
                "only text or document deliveries can be retargeted"
            );
            let partial = receipt
                .as_deref()
                .and_then(|r| serde_json::from_str::<Value>(r).ok())
                .is_some_and(|r| !r.is_null() && r.as_array().is_none_or(|a| !a.is_empty()));
            ensure!(!partial, "delivery already has a receipt");
            if !checked_sources.contains(&source) {
                if let Some(owner) = rule.known_owners.get(&source) {
                    ensure!(
                        owner == rule.target_owner,
                        "target bot delivers to a different chat"
                    );
                } else {
                    let chats = source_chats(tx, &source)?;
                    ensure!(
                        !chats.is_empty(),
                        "original chat of the removed bot is unknown"
                    );
                    ensure!(
                        chats.len() == 1 && chats.contains(rule.target_owner),
                        "target bot delivers to a different chat"
                    );
                }
                checked_sources.insert(source.clone());
            }
            // Message ids belong to the old bot's conversation; a reply
            // reference would fail or point elsewhere through another bot.
            let mut payload: Value =
                serde_json::from_str(&payload).context("invalid delivery payload")?;
            let reply_removed = payload
                .as_object_mut()
                .and_then(|p| p.remove("reply_to_message_id"))
                .is_some();
            let changed = tx.execute(
                "UPDATE events SET profile=?,payload=?,next_attempt=?,updated_at=? WHERE id=? AND status='pending' AND profile=?",
                params![target, payload.to_string(), now, now, id, source],
            )?;
            ensure!(changed == 1, "delivery changed concurrently");
            let mut resolution = resolution_on(tx, id)?;
            resolution["retargeted_from"] = json!(source);
            resolution["retargeted_to"] = json!(target);
            resolution["retargeted_at"] = json!(now);
            resolution["retargeted_by"] = json!(actor);
            if reply_removed {
                resolution["reply_reference_removed"] = json!(true);
            }
            put_on(tx, &format!("{RESOLUTION_PREFIX}{id}"), &resolution)?;
            results.push(public_event(
                self.event_on(tx, id)?.context("unknown event")?,
            ));
        }
        Ok(results)
    }
    /// Suppresses pending deliveries; nothing is sent. Records who and why.
    pub(crate) fn discard_pending_on(
        &self,
        tx: &Connection,
        ids: &[String],
        reason: &str,
        actor: &str,
    ) -> Result<Vec<Value>> {
        let now = crate::now();
        let mut results = Vec::new();
        for id in ids {
            let previous: Option<Option<String>> = tx
                .query_row(
                    "SELECT error FROM events WHERE id=? AND status='pending'",
                    [id],
                    |r| r.get(0),
                )
                .optional()?;
            let previous_error = previous.context("only pending deliveries can be discarded")?;
            let changed = tx.execute(
                "UPDATE events SET status='suppressed',error=?,updated_at=? WHERE id=? AND status='pending'",
                params![reason, now, id],
            )?;
            ensure!(changed == 1, "delivery changed concurrently");
            let mut resolution = resolution_on(tx, id)?;
            resolution["resolution"] = json!("discard");
            resolution["resolved_at"] = json!(now);
            resolution["resolved_by"] = json!(actor);
            resolution["previous_status"] = json!("pending");
            resolution["previous_error"] = json!(previous_error);
            resolution["reason"] = json!(reason);
            put_on(tx, &format!("{RESOLUTION_PREFIX}{id}"), &resolution)?;
            results.push(public_event(
                self.event_on(tx, id)?.context("unknown event")?,
            ));
        }
        Ok(results)
    }
    /// Delivery ids of a bot in one status, oldest first.
    pub fn deliveries_for_bot(&self, bot: &str, status: &str) -> Result<Vec<String>> {
        let db = self.connect()?;
        ids_for_bot(&db, bot, status)
    }
    /// Per-bot runtime state rows of bots that are not configured: kv rows of
    /// the per-bot families and finished Telegram updates. Delivery history
    /// (`event-gone*`), delivery resolutions and shared rows such as
    /// `uncertain:summary:<day>` are never selected; pending or running updates
    /// are kept. `only` restricts the selection to the named bots.
    pub fn stale_bot_state(
        &self,
        configured: &BTreeSet<String>,
        only: Option<&[String]>,
    ) -> Result<StaleState> {
        let db = self.connect()?;
        let selected = |bot: &str| {
            crate::identifier(bot)
                && !configured.contains(bot)
                && only.is_none_or(|o| o.iter().any(|b| b == bot))
        };
        let mut state = StaleState::default();
        let mut query = db.prepare("SELECT key FROM kv ORDER BY key")?;
        for key in query.query_map([], |r| r.get::<_, String>(0))? {
            let key = key?;
            if let Some(bot) = bot_state_key(&key)
                && selected(bot)
            {
                state.kv.push(key);
            }
        }
        let mut query = db.prepare(
            "SELECT profile,status IN ('pending','running'),count(*) FROM updates GROUP BY profile,status IN ('pending','running') ORDER BY profile",
        )?;
        let rows = query
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, bool>(1)?,
                    r.get::<_, i64>(2)?,
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        for (bot, open, count) in rows {
            if selected(&bot) {
                let entry = if open {
                    &mut state.updates_kept
                } else {
                    &mut state.updates
                };
                *entry.entry(bot).or_default() += count;
            }
        }
        Ok(state)
    }
    /// Applies one operator plan in a single IMMEDIATE transaction.
    pub fn apply_plan(
        &self,
        plan: &OperatorPlan,
        live: &[String],
        known_owners: &BTreeMap<String, String>,
        actor: &str,
    ) -> Result<Value> {
        self.run_plan(plan, live, known_owners, actor, true)
    }
    /// Run every check of `apply_plan` and roll back, so refusals happen
    /// before any backup or configuration write.
    pub fn check_plan(
        &self,
        plan: &OperatorPlan,
        live: &[String],
        known_owners: &BTreeMap<String, String>,
        actor: &str,
    ) -> Result<Value> {
        self.run_plan(plan, live, known_owners, actor, false)
    }
    fn run_plan(
        &self,
        plan: &OperatorPlan,
        live: &[String],
        known_owners: &BTreeMap<String, String>,
        actor: &str,
        commit: bool,
    ) -> Result<Value> {
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let mut result = json!({"retargeted":0,"discarded":0,"kv_deleted":0,"updates_deleted":0});
        let mut ready = BTreeSet::new();
        let mut rows = Vec::new();
        for step in &plan.retarget {
            let ids = step.resolve(&tx)?;
            if ids.is_empty() {
                continue;
            }
            let rule = RetargetRule {
                target: &step.to,
                target_owner: &step.to_owner,
                live,
                known_owners,
            };
            let moved = self.retarget_on(&tx, &ids, &rule, actor)?;
            result["retargeted"] =
                json!(result["retargeted"].as_u64().unwrap_or(0) + moved.len() as u64);
            rows.extend(moved);
            ready.insert(step.to.clone());
        }
        for step in &plan.discard {
            let ids = step.resolve(&tx)?;
            if ids.is_empty() {
                continue;
            }
            let reason = if step.reason.is_empty() {
                "operator discarded"
            } else {
                step.reason.as_str()
            };
            let gone = self.discard_pending_on(&tx, &ids, reason, actor)?;
            result["discarded"] =
                json!(result["discarded"].as_u64().unwrap_or(0) + gone.len() as u64);
        }
        let mut kv = 0;
        for key in &plan.delete_kv {
            ensure!(
                bot_state_key(key).is_some(),
                "only per-bot state can be pruned"
            );
            kv += tx.execute("DELETE FROM kv WHERE key=?", [key])?;
        }
        let mut updates = 0;
        for bot in &plan.delete_updates_for {
            updates += tx.execute(
                "DELETE FROM updates WHERE profile=? AND status NOT IN ('pending','running')",
                [bot],
            )?;
        }
        result["deliveries"] = json!(rows);
        result["kv_deleted"] = json!(kv);
        result["updates_deleted"] = json!(updates);
        if !commit {
            tx.rollback()?;
            return Ok(result);
        }
        tx.commit()?;
        for bot in ready {
            self.runtime
                .delivery_ready(&bot, crate::optimization::Lane::delivery(false));
        }
        Ok(result)
    }
    /// Pending delivery ids still bound to `bot`.
    pub fn pending_for_bot(&self, bot: &str) -> Result<Vec<String>> {
        let db = self.connect()?;
        let mut query = db.prepare(
            "SELECT id FROM events WHERE status='pending' AND profile=? ORDER BY created_at,id",
        )?;
        let ids = query
            .query_map([bot], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(ids)
    }
    pub fn cancel(&self, module: &str, id: &str) -> Result<Value> {
        self.cancel_or_insert(module, id, None)
    }
    pub(crate) fn cancel_or_insert(
        &self,
        module: &str,
        id: &str,
        bot: Option<&str>,
    ) -> Result<Value> {
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let mut current = self.event_on(&tx, id)?;
        if current.is_none()
            && let Some(bot) = bot
        {
            ensure!(crate::identifier(id) && id.len() <= 64, "invalid event id");
            crate::event_history::remove_expired(&tx, id, crate::now())?;
            tx.execute("INSERT INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,module) VALUES(?,?,'cancelled','text','{}','suppressed',0,0,?,?,?)",params![id,bot,crate::now(),crate::now(),module])?;
            current = self.event_on(&tx, id)?;
        }
        let current = current.context("unknown event")?;
        ensure!(
            current["module"] == module,
            "event belongs to another module"
        );
        tx.execute("UPDATE events SET status='suppressed',error='cancelled before send',updated_at=? WHERE id=? AND status='pending' AND attempts=0 AND receipt IS NULL",params![crate::now(),id])?;
        let result = self.event_on(&tx, id)?.context("unknown event")?;
        tx.commit()?;
        Ok(result)
    }
    pub fn claim(&self, bot: &str) -> Result<Option<Value>> {
        self.claim_lane(bot, None)
    }
    pub fn claim_lane(&self, bot: &str, bark: Option<bool>) -> Result<Option<Value>> {
        Ok(match self.claim_ready(bot, bark)? {
            Claim::Event(event) => Some(event),
            Claim::Empty(_) | Claim::Discarded => None,
        })
    }
    pub(crate) fn claim_ready(&self, bot: &str, bark: Option<bool>) -> Result<Claim> {
        self.claim_ready_filtered(bot, bark, false)
    }
    pub(crate) fn claim_ready_filtered(
        &self,
        bot: &str,
        bark: Option<bool>,
        system_only: bool,
    ) -> Result<Claim> {
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let query = if system_only {
            "SELECT id,kind,payload,receipt,attempts FROM events WHERE profile=? AND status='pending' AND next_attempt<=? AND (? IS NULL OR (kind='bark')=?) AND module='gateway' AND job_id='freeze-alert' ORDER BY created_at,id LIMIT 1"
        } else {
            "SELECT id,kind,payload,receipt,attempts FROM events WHERE profile=? AND status='pending' AND next_attempt<=? AND (? IS NULL OR (kind='bark')=?) ORDER BY created_at,id LIMIT 1"
        };
        let row = tx
            .query_row(query, params![bot, crate::now(), bark, bark], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, Option<String>>(3)?,
                    r.get::<_, i64>(4)?,
                ))
            })
            .optional()?;
        let Some((id, kind, payload, receipt, attempts)) = row else {
            let due = if system_only {
                tx.query_row(
                    "SELECT min(next_attempt) FROM events WHERE profile=? AND status='pending' AND (? IS NULL OR (kind='bark')=?) AND module='gateway' AND job_id='freeze-alert'",
                    params![bot, bark, bark],
                    |r| r.get::<_, Option<f64>>(0),
                )?
            } else {
                tx.query_row(
                    "SELECT min(next_attempt) FROM events WHERE profile=? AND status='pending' AND (? IS NULL OR (kind='bark')=?)",
                    params![bot, bark, bark],
                    |r| r.get::<_, Option<f64>>(0),
                )?
            };
            if bark.is_some() {
                self.runtime
                    .metrics
                    .delivery_empty_claims
                    .fetch_add(1, Ordering::Relaxed);
            }
            return Ok(Claim::Empty(due));
        };
        let parsed: Result<(Value, Option<Value>), serde_json::Error> = (|| {
            Ok((
                serde_json::from_str(&payload)?,
                receipt
                    .as_ref()
                    .map(|s| serde_json::from_str(s))
                    .transpose()?,
            ))
        })();
        let (payload, receipt) = match parsed {
            Ok(value) => value,
            Err(_) => {
                tx.execute("UPDATE events SET status=?,error='stored delivery payload invalid',updated_at=? WHERE id=?",params![if receipt.is_some(){"uncertain"}else{"failed"},crate::now(),id])?;
                tx.commit()?;
                return Ok(Claim::Discarded);
            }
        };
        tx.execute(
            "UPDATE events SET status='sending',attempts=attempts+1,updated_at=? WHERE id=?",
            params![crate::now(), id],
        )?;
        tx.commit()?;
        Ok(Claim::Event(
            json!({"id":id,"profile":bot,"kind":kind,"payload":payload,"receipt":receipt,"attempts":attempts+1}),
        ))
    }
    pub fn finish(
        &self,
        id: &str,
        status: &str,
        receipt: Option<&Value>,
        error: &str,
        delay: f64,
    ) -> Result<()> {
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        tx.execute("UPDATE events SET status=?,receipt=COALESCE(?,receipt),error=?,next_attempt=?,updated_at=? WHERE id=?",params![status,receipt.map(Value::to_string),error,crate::now()+delay,crate::now(),id])?;
        let lane: Option<(String, bool)> = if status == "pending" {
            tx.query_row(
                "SELECT profile,kind='bark' FROM events WHERE id=?",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?
        } else {
            None
        };
        tx.commit()?;
        if let Some((bot, bark)) = lane {
            self.runtime
                .delivery_ready(&bot, crate::optimization::Lane::delivery(bark));
        }
        Ok(())
    }
    pub fn own_callback(&self, bot: &str, owner: &str, query: &Value) -> Result<bool> {
        let message = &query["message"];
        if !crate::telegram::authorized(
            owner,
            &json!({"chat":message["chat"],"from":query["from"]}),
        ) {
            return Ok(false);
        }
        let Some(mid) = message["message_id"].as_i64() else {
            return Ok(false);
        };
        let db = self.connect()?;
        let mut stmt=db.prepare("SELECT payload,receipt FROM events WHERE id IN (SELECT id FROM events WHERE profile=?1 AND receipt IS NOT NULL AND json_extract(receipt,'$[#-1].message_id')=?2 UNION SELECT id FROM events WHERE profile=?1 AND receipt IS NOT NULL AND json_extract(receipt,'$.message_id')=?2)")?;
        for row in stmt.query_map(params![bot, mid], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
        })? {
            let (p, r) = row?;
            let p: Value = serde_json::from_str(&p)?;
            let r: Value = serde_json::from_str(&r)?;
            let r = r.as_array().and_then(|a| a.last()).unwrap_or(&r);
            if crate::telegram::numeric_id(&r["chat_id"]) != owner {
                continue;
            }
            if p["reply_markup"]["inline_keyboard"]
                .as_array()
                .is_some_and(|rows| {
                    rows.iter().any(|row| {
                        row.as_array().is_some_and(|items| {
                            items.iter().any(|b| b["callback_data"] == query["data"])
                        })
                    })
                })
            {
                return Ok(true);
            }
        }
        let owned = crate::event_history::owns(&db, bot, owner, mid, &query["data"], crate::now())?;
        if owned {
            self.runtime.retention.hits.fetch_add(1, Ordering::Relaxed);
        }
        Ok(owned)
    }
}

pub(crate) fn complete_update_transaction(
    tx: &rusqlite::Transaction<'_>,
    bot: &str,
    update: i64,
    owner: &str,
    reply: &Value,
    status: &str,
    contexts: &[String],
) -> Result<()> {
    if reply["text"].as_str().is_some_and(|s| !s.is_empty()) {
        let id = crate::digest(format!("{bot}:text:reply:{update}"));
        tx.execute("INSERT OR IGNORE INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,module) VALUES(?,?,'bot','text',?,'pending',0,0,?,?,?)",params![&id[..32],bot,reply.to_string(),crate::now(),crate::now(),owner])?;
    }
    tx.execute(
        "UPDATE updates SET status=? WHERE profile=? AND update_id=?",
        params![status, bot, update],
    )?;
    for hash in contexts {
        tx.execute("DELETE FROM contexts WHERE hash=?", [hash])?;
    }
    Ok(())
}

pub struct PooledConnection {
    db: Option<Connection>,
    pool: Arc<Mutex<Vec<Connection>>>,
}
impl Deref for PooledConnection {
    type Target = Connection;
    fn deref(&self) -> &Connection {
        self.db.as_ref().expect("connection checked out")
    }
}
impl DerefMut for PooledConnection {
    fn deref_mut(&mut self) -> &mut Connection {
        self.db.as_mut().expect("connection checked out")
    }
}
impl Drop for PooledConnection {
    fn drop(&mut self) {
        if let Some(db) = self.db.take() {
            if !db.is_autocommit() && db.execute_batch("ROLLBACK").is_err() {
                return;
            }
            if let Ok(mut pool) = self.pool.lock()
                && pool.len() < 4
            {
                pool.push(db);
            }
        }
    }
}

pub(crate) fn pooled(path: &Path) -> Result<PooledConnection> {
    type Pools = Mutex<std::collections::BTreeMap<PathBuf, Arc<Mutex<Vec<Connection>>>>>;
    static POOLS: std::sync::OnceLock<Pools> = std::sync::OnceLock::new();
    let pool = POOLS
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| anyhow::anyhow!("database pool unavailable"))?
        .entry(path.to_owned())
        .or_default()
        .clone();
    connect(path, &pool)
}

fn connect(path: &Path, pool: &Arc<Mutex<Vec<Connection>>>) -> Result<PooledConnection> {
    let cached = pool
        .lock()
        .map_err(|_| anyhow::anyhow!("database pool unavailable"))?
        .pop();
    // Opening and SQLite waits must not serialize other borrowers of this pool.
    let db = if let Some(db) = cached {
        db
    } else {
        let db = Connection::open(path)?;
        db.busy_handler(Some(wait_for_writer))?;
        db.execute_batch("PRAGMA foreign_keys=ON; PRAGMA synchronous=FULL;")?;
        db
    };
    Ok(PooledConnection {
        db: Some(db),
        pool: pool.clone(),
    })
}

/// Fail-fast WAL maintenance. Uses a private connection so pool busy waits cannot
/// pin a dispatch or delivery lane while checkpointing.
pub(crate) fn checkpoint_database(home: &Path, path: &Path, threshold: u64, force: bool) -> Value {
    let at = crate::now();
    let mut wal_path = path.as_os_str().to_owned();
    wal_path.push("-wal");
    let wal_path = PathBuf::from(wal_path);
    let wal_bytes = match std::fs::symlink_metadata(&wal_path) {
        Ok(m) if m.file_type().is_symlink() => {
            return record_checkpoint(
                path,
                json!({"status":"unsafe_path","wal_bytes_before":null,"sampled_at":at}),
            );
        }
        Ok(m) => m.len(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => 0,
        Err(_) => {
            return record_checkpoint(
                path,
                json!({"status":"stat_unavailable","wal_bytes_before":null,"sampled_at":at}),
            );
        }
    };
    if home.join("run/freeze").exists() {
        return record_checkpoint(
            path,
            json!({"status":"skipped_frozen","wal_bytes_before":wal_bytes,"sampled_at":at}),
        );
    }
    if !force && wal_bytes < threshold {
        return record_checkpoint(
            path,
            json!({"status":"skipped_small","wal_bytes_before":wal_bytes,"sampled_at":at}),
        );
    }
    if !path.is_file() {
        return record_checkpoint(
            path,
            json!({"status":"missing","wal_bytes_before":wal_bytes,"sampled_at":at}),
        );
    }
    let outcome = (|| -> Result<&'static str> {
        let db = Connection::open(path)?;
        // Abandon immediately on contention; never inherit the pool's 5s writer wait.
        db.busy_handler(Some(|_| false))?;
        db.execute_batch("PRAGMA query_only=OFF;")?;
        // PASSIVE first so a quiet database shrinks without waiting on readers.
        let _ = db.query_row("PRAGMA wal_checkpoint(PASSIVE)", [], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, i64>(2)?,
            ))
        });
        match db.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, i64>(2)?,
            ))
        }) {
            Ok((busy, log, _)) if busy == 0 && log == 0 => Ok("truncated"),
            Ok((busy, _, _)) if busy != 0 => Ok("busy"),
            Ok(_) => Ok("partial"),
            Err(error)
                if matches!(
                    error.sqlite_error_code(),
                    Some(
                        rusqlite::ffi::ErrorCode::DatabaseBusy
                            | rusqlite::ffi::ErrorCode::DatabaseLocked
                    )
                ) =>
            {
                Ok("busy")
            }
            Err(_) => Ok("error"),
        }
    })();
    let status = outcome.unwrap_or("error");
    record_checkpoint(
        path,
        json!({"status":status,"wal_bytes_before":wal_bytes,"sampled_at":at}),
    )
}

fn record_checkpoint(path: &Path, value: Value) -> Value {
    last_checkpoints()
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .insert(path.to_owned(), value.clone());
    value
}

fn last_checkpoints() -> &'static Mutex<std::collections::BTreeMap<PathBuf, Value>> {
    static LAST: std::sync::OnceLock<Mutex<std::collections::BTreeMap<PathBuf, Value>>> =
        std::sync::OnceLock::new();
    LAST.get_or_init(Default::default)
}

pub(crate) fn last_checkpoint(path: &Path) -> Value {
    last_checkpoints()
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .get(path)
        .cloned()
        .unwrap_or(Value::Null)
}

pub(crate) enum Claim {
    Event(Value),
    Empty(Option<f64>),
    Discarded,
}

pub(crate) fn utc8_day(time: std::time::SystemTime) -> String {
    chrono::DateTime::<chrono::Utc>::from(time)
        .with_timezone(&chrono::FixedOffset::east_opt(8 * 60 * 60).expect("valid UTC+8 offset"))
        .format("%Y-%m-%d")
        .to_string()
}

#[cfg(test)]
mod checkpoint_tests {
    use super::*;

    #[test]
    fn truncate_shrinks_wal_and_records_outcome() {
        let temp = tempfile::tempdir().unwrap();
        let home = temp.path();
        std::fs::create_dir_all(home.join("run")).unwrap();
        std::fs::create_dir_all(home.join("gateway")).unwrap();
        let path = home.join("gateway/state.sqlite3");
        {
            let db = Connection::open(&path).unwrap();
            db.execute_batch(
                "PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; CREATE TABLE t(x);",
            )
            .unwrap();
            let tx = db.unchecked_transaction().unwrap();
            for i in 0..4000i32 {
                tx.execute("INSERT INTO t VALUES(?1)", [i]).unwrap();
            }
            tx.commit().unwrap();
            let mut wal = path.as_os_str().to_owned();
            wal.push("-wal");
            let before = std::fs::metadata(PathBuf::from(wal)).unwrap().len();
            assert!(
                before > 4096,
                "fixture must leave a WAL while writer is open: {before}"
            );
            // Keep the writer open so the sidecar stays large, then checkpoint via a
            // separate fail-fast connection; busy is acceptable and retried below.
            let first = checkpoint_database(home, &path, 1, true);
            assert!(
                matches!(
                    first["status"].as_str(),
                    Some("truncated" | "busy" | "partial")
                ),
                "{first}"
            );
        }
        let result = checkpoint_database(home, &path, 1, true);
        assert_eq!(result["status"], "truncated", "{result}");
        let mut wal = path.as_os_str().to_owned();
        wal.push("-wal");
        let after = match std::fs::metadata(PathBuf::from(wal)) {
            Ok(m) => m.len(),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => 0,
            Err(e) => panic!("{e}"),
        };
        assert_eq!(after, 0, "truncated must clear the WAL sidecar");
        assert_eq!(last_checkpoint(&path)["status"], "truncated");
    }

    #[test]
    fn freeze_and_small_wal_skip_without_blocking() {
        let temp = tempfile::tempdir().unwrap();
        let home = temp.path();
        std::fs::create_dir_all(home.join("run")).unwrap();
        let path = home.join("task.sqlite3");
        Connection::open(&path)
            .unwrap()
            .execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE t(x);")
            .unwrap();
        let small = checkpoint_database(home, &path, u64::MAX, false);
        assert_eq!(small["status"], "skipped_small");
        std::fs::File::create(home.join("run/freeze")).unwrap();
        let frozen = checkpoint_database(home, &path, 1, true);
        assert_eq!(frozen["status"], "skipped_frozen");
    }
}

#[cfg(test)]
mod freeze_alert_tests {
    use super::*;

    #[test]
    fn actual_repeat_interval_survives_reopening_and_uses_one_key() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("state.sqlite3");
        let store = Store::new(&path).unwrap();
        store
            .freeze_alert_at("fixture", 900, 100, 1800, 1000.0)
            .unwrap();
        // The old bucket scheme sent a second alert at age 1800, only 900s later.
        let reopened = Store::new(&path).unwrap();
        reopened
            .freeze_alert_at("fixture", 1800, 100, 1800, 1900.0)
            .unwrap();
        reopened
            .freeze_alert_at("fixture", 2699, 100, 1800, 2799.0)
            .unwrap();
        let count = || {
            reopened
                .connect()
                .unwrap()
                .query_row("SELECT count(*) FROM events", [], |r| r.get::<_, i64>(0))
                .unwrap()
        };
        assert_eq!(count(), 1);
        reopened
            .freeze_alert_at("fixture", 2700, 100, 1800, 2800.0)
            .unwrap();
        assert_eq!(count(), 2);
        reopened
            .freeze_alert_at("fixture", 900, 2000, 1800, 2900.0)
            .unwrap();
        assert_eq!(count(), 3, "a different freeze has its own first alert");
        assert_eq!(
            reopened
                .connect()
                .unwrap()
                .query_row(
                    "SELECT count(*) FROM kv WHERE key LIKE 'freeze-alert:%'",
                    [],
                    |r| r.get::<_, i64>(0)
                )
                .unwrap(),
            1
        );
    }

    #[test]
    fn legacy_last_alert_still_enforces_repeat_interval() {
        let temp = tempfile::tempdir().unwrap();
        let store = Store::new(&temp.path().join("state.sqlite3")).unwrap();
        store
            .put(
                "freeze-alert:last",
                &json!({"at":1000.0,"fingerprint":"100:0"}),
            )
            .unwrap();
        store
            .freeze_alert_at("fixture", 1800, 100, 1800, 1900.0)
            .unwrap();
        assert_eq!(
            store
                .connect()
                .unwrap()
                .query_row("SELECT count(*) FROM events", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            0
        );
    }
}

/// Idempotent internal diagnostics migration; public tables and IDs stay unchanged.
fn internal_state(db: &rusqlite::Connection) -> Result<()> {
    let tx = rusqlite::Transaction::new_unchecked(db, rusqlite::TransactionBehavior::Immediate)?;
    internal_state_transaction(&tx)?;
    tx.commit()?;
    Ok(())
}

fn internal_state_transaction(tx: &rusqlite::Transaction<'_>) -> Result<()> {
    tx.execute_batch("DROP TABLE IF EXISTS web_tokens; DROP TABLE IF EXISTS web_sessions;")?;
    initialize_uncertain(tx)?;
    let installed: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM kv WHERE key='uncertain:triggers' AND value='3')",
        [],
        |r| r.get(0),
    )?;
    if !installed {
        tx.execute_batch(
            "DROP TRIGGER IF EXISTS uncertain_count_update_v2;
                          DROP TRIGGER IF EXISTS uncertain_count_insert_v2;",
        )?;
        for (name, action, condition) in [
            (
                "uncertain_count_update_v2",
                "UPDATE OF status",
                "AND OLD.status<>'uncertain'",
            ),
            ("uncertain_count_insert_v2", "INSERT", ""),
        ] {
            tx.execute_batch(&format!(
                "CREATE TRIGGER {name} AFTER {action} ON events
                 WHEN NEW.status='uncertain' {condition} AND NEW.job_id<>'uncertain-summary'
                 BEGIN
                   UPDATE kv SET value=json_set(value,'$.pending',json_extract(value,'$.pending')+1)
                     WHERE key='uncertain:counter:v2';
                   {seed}
                 END;",
                // A deliberately retired counter must stay inactive while an older
                // binary owns the database. The next Store::new seeds it explicitly.
                seed = UNCERTAIN_SEED.replace("WHERE NOT EXISTS", "WHERE NOT EXISTS (SELECT 1 FROM kv WHERE key='uncertain:counter:v2:retired') AND NOT EXISTS"),
            ))?;
        }
        tx.execute("INSERT INTO kv VALUES('uncertain:triggers','3') ON CONFLICT(key) DO UPDATE SET value=excluded.value", [])?;
    }
    Ok(())
}

const UNCERTAIN_SEED: &str = "INSERT INTO kv(key,value)
 SELECT 'uncertain:counter:v2',json_object('version',2,'pending',
   (SELECT count(*) FROM events WHERE status='uncertain' AND job_id<>'uncertain-summary'
    AND id NOT IN (SELECT value FROM json_each(COALESCE(
      (SELECT value FROM kv WHERE key='uncertain:watermark'),'[]')))), 'last_day','')
 WHERE NOT EXISTS (SELECT 1 FROM kv WHERE key='uncertain:counter:v2');";

pub(crate) fn initialize_uncertain(db: &rusqlite::Connection) -> Result<()> {
    db.execute_batch(UNCERTAIN_SEED)?;
    Ok(())
}

#[cfg(test)]
mod owner_tests {
    use super::should_align;

    #[test]
    fn only_root_owned_single_link_files_in_service_directories_are_aligned() {
        assert_eq!(should_align((993, 986), (0, 1, true)), Some((993, 986)));
        // Already owned by the service, linked elsewhere, not a file, or in a root directory.
        assert_eq!(should_align((993, 986), (993, 1, true)), None);
        assert_eq!(should_align((993, 986), (0, 2, true)), None);
        assert_eq!(should_align((993, 986), (0, 1, false)), None);
        assert_eq!(should_align((0, 0), (0, 1, true)), None);
    }
}

#[cfg(test)]
mod resolution_tests {
    use super::*;

    fn uncertain(store: &Store, id: &str) {
        store
            .submit(
                "fixture",
                &json!({"id":id,"kind":"text","payload":{"text":"private body"}}),
                "fixture",
            )
            .unwrap();
        let event = store.claim("fixture").unwrap().unwrap();
        assert_eq!(event["id"], id);
        store
            .finish(
                id,
                "uncertain",
                Some(&json!([{"message_id":1,"chat_id":4242}])),
                "Telegram 回执未确认",
                0.0,
            )
            .unwrap();
    }

    fn queued(store: &Store, id: &str, bot: &str) {
        store
            .submit(
                "fixture",
                &json!({"id":id,"kind":"text","payload":{"text":"private body"}}),
                bot,
            )
            .unwrap();
    }

    fn delivered(store: &Store, id: &str, bot: &str, chat: i64) {
        queued(store, id, bot);
        assert_eq!(store.claim(bot).unwrap().unwrap()["id"], id);
        store
            .finish(
                id,
                "delivered",
                Some(&json!([{"message_id":9,"chat_id":chat}])),
                "",
                0.0,
            )
            .unwrap();
    }

    #[test]
    fn retarget_moves_orphaned_pending_deliveries_to_same_chat_bot() {
        let temporary = tempfile::tempdir().unwrap();
        let store = Store::new(&temporary.path().join("state.sqlite3")).unwrap();
        let configured = vec!["new".to_string(), "other".to_string()];
        delivered(&store, "old-history", "old", 4242);
        queued(&store, "orphan-a", "old");
        store
            .submit(
                "fixture",
                &json!({"id":"orphan-b","kind":"text","payload":{"text":"private body","reply_to_message_id":9}}),
                "old",
            )
            .unwrap();
        queued(&store, "live", "other");
        queued(&store, "unknown-history", "ghost");
        assert_eq!(
            store.pending_for_bot("old").unwrap(),
            vec!["orphan-a".to_string(), "orphan-b".to_string()]
        );
        let ids = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();

        // A different destination chat refuses everything; nothing moves.
        let error = store
            .retarget_deliveries(
                &ids(&["orphan-a", "orphan-b"]),
                "new",
                "999",
                &configured,
                "uid:0",
            )
            .unwrap_err();
        assert!(error.to_string().contains("different chat"));
        assert!(!error.to_string().contains("4242"));
        assert_eq!(store.event("orphan-a").unwrap()["profile"], "old");
        // Bots still configured, unknown history, unknown targets and empty
        // selections are refused; a refusal anywhere rolls back the batch.
        assert!(
            store
                .retarget_deliveries(&ids(&["live"]), "new", "4242", &configured, "uid:0")
                .is_err()
        );
        assert!(
            store
                .retarget_deliveries(
                    &ids(&["unknown-history"]),
                    "new",
                    "4242",
                    &configured,
                    "uid:0"
                )
                .is_err()
        );
        assert!(
            store
                .retarget_deliveries(&ids(&["orphan-a"]), "gone", "4242", &configured, "uid:0")
                .is_err()
        );
        assert!(
            store
                .retarget_deliveries(&[], "new", "4242", &configured, "uid:0")
                .is_err()
        );
        assert!(
            store
                .retarget_deliveries(
                    &ids(&["orphan-a", "old-history"]),
                    "new",
                    "4242",
                    &configured,
                    "uid:0"
                )
                .is_err()
        );
        assert_eq!(store.event("orphan-a").unwrap()["profile"], "old");

        let moved = store
            .retarget_deliveries(
                &ids(&["orphan-a", "orphan-b"]),
                "new",
                "4242",
                &configured,
                "uid:0",
            )
            .unwrap();
        assert_eq!(moved.len(), 2);
        let text = serde_json::to_string(&moved).unwrap();
        assert!(!text.contains("4242") && !text.contains("private body"));
        assert_eq!(moved[0]["profile"], "new");
        assert_eq!(moved[0]["status"], "pending");
        assert_eq!(moved[0]["resolution"]["retargeted_from"], "old");
        assert_eq!(moved[0]["resolution"]["retargeted_by"], "uid:0");
        // Modules see the move through delivery.get; it applies once.
        assert_eq!(
            store.event("orphan-b").unwrap()["resolution"]["retargeted_to"],
            "new"
        );
        assert!(
            store
                .retarget_deliveries(&ids(&["orphan-a"]), "new", "4242", &configured, "uid:0")
                .is_err()
        );
        assert!(store.pending_for_bot("old").unwrap().is_empty());
        assert!(store.claim("old").unwrap().is_none());
        assert!(
            moved[0]["resolution"]
                .get("reply_reference_removed")
                .is_none()
        );
        assert_eq!(moved[1]["resolution"]["reply_reference_removed"], true);
        assert_eq!(store.claim("new").unwrap().unwrap()["id"], "orphan-a");
        let claimed = store.claim("new").unwrap().unwrap();
        assert_eq!(claimed["id"], "orphan-b");
        assert!(claimed["payload"].get("reply_to_message_id").is_none());
        assert_eq!(claimed["payload"]["text"], "private body");
        assert!(store.claim("new").unwrap().is_none());
    }

    #[test]
    fn retarget_keeps_earlier_resolution_and_refuses_partial_receipts() {
        let temporary = tempfile::tempdir().unwrap();
        let store = Store::new(&temporary.path().join("state.sqlite3")).unwrap();
        let configured = vec!["new".to_string()];
        delivered(&store, "history", "fixture", 4242);
        uncertain(&store, "resent");
        store.resolve_delivery("resent", "resend", "uid:0").unwrap();
        // The resent item carries the receipt of its first attempt.
        assert!(
            store
                .retarget_deliveries(&["resent".to_string()], "new", "4242", &configured, "uid:0")
                .unwrap_err()
                .to_string()
                .contains("receipt")
        );
        queued(&store, "fresh", "fixture");
        store
            .put(
                "delivery-resolution:fresh",
                &json!({"resolution":"resend","resend_count":1}),
            )
            .unwrap();
        let moved = store
            .retarget_deliveries(&["fresh".to_string()], "new", "4242", &configured, "uid:0")
            .unwrap();
        assert_eq!(moved[0]["resolution"]["resend_count"], 1);
        assert_eq!(moved[0]["resolution"]["retargeted_from"], "fixture");
    }

    #[test]
    fn operator_resolutions_apply_once_to_uncertain_deliveries_only() {
        let temporary = tempfile::tempdir().unwrap();
        let store = Store::new(&temporary.path().join("state.sqlite3")).unwrap();
        for id in ["resolve-a", "resolve-b", "resolve-c"] {
            uncertain(&store, id);
        }
        let listed = store.deliveries("uncertain", 50).unwrap();
        assert_eq!(listed.len(), 3);
        let text = serde_json::to_string(&listed).unwrap();
        assert!(!text.contains("private body") && !text.contains("4242"));
        assert_eq!(listed[0]["has_receipt"], true);
        assert_eq!(listed[0]["receipt_parts"], 1);
        assert!(store.deliveries("bogus", 5).is_err());

        let confirmed = store
            .resolve_delivery("resolve-a", "confirm", "uid:0")
            .unwrap();
        assert_eq!(confirmed["status"], "delivered");
        assert!(confirmed.get("receipt").is_none());
        assert_eq!(confirmed["resolution"]["resolution"], "confirm");
        assert_eq!(confirmed["resolution"]["previous_status"], "uncertain");
        assert!(
            store
                .resolve_delivery("resolve-a", "discard", "uid:0")
                .is_err()
        );
        // Modules read the resolution through delivery.get.
        assert_eq!(
            store.event("resolve-a").unwrap()["resolution"]["resolved_by"],
            "uid:0"
        );

        let discarded = store
            .resolve_delivery("resolve-b", "discard", "uid:0")
            .unwrap();
        assert_eq!(discarded["status"], "suppressed");

        let resent = store
            .resolve_delivery("resolve-c", "resend", "uid:0")
            .unwrap();
        assert_eq!(resent["status"], "pending");
        assert_eq!(resent["resolution"]["resend_count"], 1);
        let claimed = store.claim("fixture").unwrap().unwrap();
        assert_eq!(claimed["id"], "resolve-c");
        assert_eq!(claimed["receipt"][0]["message_id"], 1);
        store
            .finish("resolve-c", "uncertain", None, "Telegram 回执未确认", 0.0)
            .unwrap();
        let error = store
            .resolve_delivery("resolve-c", "resend", "uid:0")
            .unwrap_err();
        assert!(error.to_string().contains("already resent"));
        assert!(
            store
                .resolve_delivery("resolve-c", "bogus", "uid:0")
                .is_err()
        );
        assert!(
            store
                .resolve_delivery("missing", "confirm", "uid:0")
                .is_err()
        );
        assert!(store.event("plain").is_err());
        assert_eq!(store.deliveries("uncertain", 50).unwrap().len(), 1);
    }

    #[test]
    fn events_without_resolution_keep_their_shape() {
        let temporary = tempfile::tempdir().unwrap();
        let store = Store::new(&temporary.path().join("state.sqlite3")).unwrap();
        uncertain(&store, "plain-event");
        assert!(
            store
                .event("plain-event")
                .unwrap()
                .get("resolution")
                .is_none()
        );
    }
}
