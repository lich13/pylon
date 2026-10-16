use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use std::{
    ops::{Deref, DerefMut},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};

#[derive(Clone)]
pub struct Store {
    pub path: PathBuf,
    pool: Arc<Mutex<Vec<Connection>>>,
}
impl Store {
    pub fn new(path: &Path) -> Result<Self> {
        std::fs::create_dir_all(path.parent().context("missing database directory")?)?;
        let store = Self {
            path: path.to_path_buf(),
            pool: Arc::new(Mutex::new(Vec::new())),
        };
        store.connect()?.execute_batch(include_str!("schema.sql"))?;
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
        Ok(store)
    }
    pub fn connect(&self) -> Result<PooledConnection> {
        let mut pool = self
            .pool
            .lock()
            .map_err(|_| anyhow::anyhow!("database pool unavailable"))?;
        let db = if let Some(db) = pool.pop() {
            db
        } else {
            let db = Connection::open(&self.path)?;
            db.busy_timeout(Duration::from_millis(500))?;
            db.execute_batch("PRAGMA foreign_keys=ON; PRAGMA synchronous=FULL;")?;
            db
        };
        Ok(PooledConnection {
            db: Some(db),
            pool: self.pool.clone(),
        })
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
        for (bot, ids, count) in interrupted {
            let id = crate::digest(format!("{bot}:recovery:{ids}"));
            let payload = json!({"text":format!("服务重启，{count} 条操作结果待核对。请打开对应入口查看已有记录；没有重复执行。"),"expires_at":crate::now()+600.0});
            tx.execute("INSERT OR IGNORE INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,module) VALUES(?,?,'system','text',?,'pending',0,0,?,?,'gateway')", params![&id[..32],bot,payload.to_string(),crate::now(),crate::now()])?;
        }
        tx.commit()?;
        Ok(())
    }
    pub fn uncertain_digest(&self, target: &str) -> Result<()> {
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let known: Option<String> = tx
            .query_row(
                "SELECT value FROM kv WHERE key='uncertain:watermark'",
                [],
                |r| r.get(0),
            )
            .optional()?;
        let current: Vec<String> = {
            let mut q = tx.prepare(
                "SELECT id FROM events WHERE status='uncertain' AND job_id<>'uncertain-summary'",
            )?;
            q.query_map([], |r| r.get(0))?
                .collect::<std::result::Result<Vec<_>, _>>()?
        };
        let Some(known) = known else {
            tx.execute(
                "INSERT INTO kv VALUES('uncertain:watermark',?)",
                [json!(current).to_string()],
            )?;
            tx.commit()?;
            return Ok(());
        };
        let mut seen: std::collections::BTreeSet<String> = serde_json::from_str(&known)?;
        let new: Vec<_> = current.iter().filter(|id| !seen.contains(*id)).collect();
        let day = chrono::Utc::now()
            .with_timezone(&chrono_tz::Asia::Taipei)
            .format("%Y-%m-%d")
            .to_string();
        let key = format!("uncertain:summary:{day}");
        let sent: bool =
            tx.query_row("SELECT EXISTS(SELECT 1 FROM kv WHERE key=?)", [&key], |r| {
                r.get(0)
            })?;
        if !new.is_empty() && !sent {
            let payload = json!({"text":format!("Pylon 有 {} 条新增投递结果待核对。原消息未自动重发，可在状态页查看。",new.len()),"expires_at":crate::now()+600.0});
            tx.execute("INSERT OR IGNORE INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,module) VALUES(?,?,'uncertain-summary','text',?,'pending',0,0,?,?,'gateway')",params![&crate::digest(&key)[..32],target,payload.to_string(),crate::now(),crate::now()])?;
            seen.extend(current);
            tx.execute("INSERT INTO kv VALUES(?, 'true')", [key])?;
            tx.execute(
                "UPDATE kv SET value=? WHERE key='uncertain:watermark'",
                [json!(seen).to_string()],
            )?;
        }
        tx.commit()?;
        Ok(())
    }
    pub fn complete_update(
        &self,
        bot: &str,
        update: i64,
        owner: &str,
        reply: &Value,
        status: &str,
    ) -> Result<()> {
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        if reply["text"].as_str().is_some_and(|s| !s.is_empty()) {
            let id = crate::digest(format!("{bot}:text:reply:{update}"));
            tx.execute("INSERT OR IGNORE INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,module) VALUES(?,?,'bot','text',?,'pending',0,0,?,?,?)",params![&id[..32],bot,reply.to_string(),crate::now(),crate::now(),owner])?;
        }
        tx.execute(
            "UPDATE updates SET status=? WHERE profile=? AND update_id=?",
            params![status, bot, update],
        )?;
        tx.commit()?;
        Ok(())
    }
    pub fn has_event(&self, id: &str) -> Result<bool> {
        Ok(self.connect()?.query_row(
            "SELECT EXISTS(SELECT 1 FROM events WHERE id=?)",
            [id],
            |r| r.get(0),
        )?)
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
        if let Some((owner, payload, job)) = existing {
            ensure!(owner == module, "event belongs to another module");
            let original: Value = serde_json::from_str(&payload)?;
            ensure!(
                job == "cancelled" || original == value["payload"],
                "event id already has different content"
            );
        } else {
            tx.execute("INSERT INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,module) VALUES(?,?,?,?,?,'pending',0,?,?,?,?)",
                params![id,bot,value["job_id"].as_str().unwrap_or("manual"),kind,value["payload"].to_string(),value["next_attempt"].as_f64().unwrap_or(0.0),value["created_at"].as_f64().unwrap_or(crate::now()),crate::now(),module])?;
        }
        tx.commit()?;
        self.event(id)
    }
    pub fn event(&self, id: &str) -> Result<Value> {
        self.connect()?.query_row("SELECT id,profile,status,attempts,next_attempt,updated_at,receipt,error,module FROM events WHERE id=?",[id],|r|{
            let receipt:Option<String>=r.get(6)?;
            Ok(json!({"id":r.get::<_,String>(0)?,"profile":r.get::<_,String>(1)?,"status":r.get::<_,String>(2)?,"attempts":r.get::<_,i64>(3)?,"next_attempt":r.get::<_,f64>(4)?,"updated_at":r.get::<_,f64>(5)?,"receipt":receipt.and_then(|s|serde_json::from_str::<Value>(&s).ok()),"error":r.get::<_,Option<String>>(7)?,"module":r.get::<_,String>(8)?}))
        }).optional()?.context("unknown event")
    }
    pub fn cancel(&self, module: &str, id: &str) -> Result<Value> {
        let db = self.connect()?;
        let current = self.event(id)?;
        ensure!(
            current["module"] == module,
            "event belongs to another module"
        );
        db.execute("UPDATE events SET status='suppressed',error='cancelled before send',updated_at=? WHERE id=? AND status='pending' AND attempts=0 AND receipt IS NULL",params![crate::now(),id])?;
        self.event(id)
    }
    pub fn claim(&self, bot: &str) -> Result<Option<Value>> {
        self.claim_lane(bot, None)
    }
    pub fn claim_lane(&self, bot: &str, bark: Option<bool>) -> Result<Option<Value>> {
        let mut db = self.connect()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let row=tx.query_row("SELECT id,kind,payload,receipt,attempts FROM events WHERE profile=? AND status='pending' AND next_attempt<=? AND (? IS NULL OR (kind='bark')=?) ORDER BY created_at,id LIMIT 1",params![bot,crate::now(),bark,bark],|r|{
            Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,Option<String>>(3)?,r.get::<_,i64>(4)?))
        }).optional()?;
        let Some((id, kind, payload, receipt, attempts)) = row else {
            return Ok(None);
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
                return Ok(None);
            }
        };
        tx.execute(
            "UPDATE events SET status='sending',attempts=attempts+1,updated_at=? WHERE id=?",
            params![crate::now(), id],
        )?;
        tx.commit()?;
        Ok(Some(
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
        self.connect()?.execute("UPDATE events SET status=?,receipt=COALESCE(?,receipt),error=?,next_attempt=?,updated_at=? WHERE id=?",params![status,receipt.map(Value::to_string),error,crate::now()+delay,crate::now(),id])?;
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
        let mut stmt=db.prepare("SELECT payload,receipt FROM events WHERE profile=? AND receipt IS NOT NULL AND (json_extract(receipt,'$[#-1].message_id')=? OR json_extract(receipt,'$.message_id')=?)")?;
        for row in stmt.query_map(params![bot, mid, mid], |r| {
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
        Ok(false)
    }
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
    Store {
        path: path.to_owned(),
        pool,
    }
    .connect()
}
