use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use std::{
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

#[derive(Clone)]
pub struct Store {
    pub path: PathBuf,
    pool: Arc<Mutex<Vec<Connection>>>,
    pub(crate) runtime: Arc<crate::optimization::Runtime>,
}
impl Store {
    pub fn new(path: &Path) -> Result<Self> {
        let store = Self::migration_target(path)?;
        {
            let db = store.connect()?;
            crate::migration::internal_state(&db)?;
        }
        Ok(store)
    }
    /// The import transaction owns internal initialization so rejected imports
    /// leave existing state untouched. Normal runtime callers must use `new`.
    pub(crate) fn migration_target(path: &Path) -> Result<Self> {
        std::fs::create_dir_all(path.parent().context("missing database directory")?)?;
        let store = Self {
            path: path.to_path_buf(),
            pool: Arc::new(Mutex::new(Vec::new())),
            runtime: Arc::default(),
        };
        {
            let db = store.connect()?;
            db.execute_batch(include_str!("schema.sql"))?;
        }
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
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
        crate::migration::initialize_uncertain(&tx)?;
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
            Some(event) => Ok(Some(event)),
            None => Ok(self.history(db, id)?.map(|g| g.result())),
        }
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
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let row=tx.query_row("SELECT id,kind,payload,receipt,attempts FROM events WHERE profile=? AND status='pending' AND next_attempt<=? AND (? IS NULL OR (kind='bark')=?) ORDER BY created_at,id LIMIT 1",params![bot,crate::now(),bark,bark],|r|{
            Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,Option<String>>(3)?,r.get::<_,i64>(4)?))
        }).optional()?;
        let Some((id, kind, payload, receipt, attempts)) = row else {
            let due = tx.query_row(
                "SELECT min(next_attempt) FROM events WHERE profile=? AND status='pending' AND (? IS NULL OR (kind='bark')=?)",
                params![bot, bark, bark], |r| r.get::<_, Option<f64>>(0),
            )?;
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
