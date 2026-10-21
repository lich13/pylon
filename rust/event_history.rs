//! Minimal terminal delivery records. Invalid records fail closed on every read.
use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, OptionalExtension, Transaction, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

pub(crate) const PREFIX: &str = "event-gone:";
pub(crate) const MESSAGE_PREFIX: &str = "event-gone-msg:";

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Gone {
    version: u32,
    pub id: String,
    pub module: String,
    pub profile: String,
    pub kind: String,
    pub cancelled: bool,
    pub status: String,
    pub attempts: u64,
    pub created_at: f64,
    pub updated_at: f64,
    pub next_attempt: f64,
    pub expires_at: f64,
    pub payload_sha256: String,
    pub receipt: Value,
    pub error: Option<String>,
    pub callbacks: Vec<String>,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Message {
    version: u32,
    event_id: String,
    expires_at: f64,
}

fn hash_valid(hash: &str) -> bool {
    hash.len() == 64
        && hash
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

/// Value equality distinguishes integer and floating representations, but treats
/// both floating zeros as equal. Map keys are already sorted by serde_json.
pub(crate) fn payload_digest(value: &Value) -> String {
    fn normalize(value: &mut Value) {
        match value {
            Value::Number(n) if n.is_f64() && n.as_f64() == Some(0.0) => *value = json!(0.0),
            Value::Array(a) => a.iter_mut().for_each(normalize),
            Value::Object(o) => o.values_mut().for_each(normalize),
            _ => {}
        }
    }
    let mut value = value.clone();
    normalize(&mut value);
    crate::digest(value.to_string())
}

fn telegram_receipt(receipt: &Value) -> bool {
    receipt.is_object()
        && receipt["message_id"].as_i64().is_some_and(|id| id > 0)
        && !crate::telegram::numeric_id(&receipt["chat_id"]).is_empty()
        && receipt["chat_id"].as_i64().is_some_and(|id| id > 0)
}

impl Gone {
    pub fn decode(raw: &str, id: &str) -> Result<Self> {
        let gone: Self = serde_json::from_str(raw).context("invalid event history")?;
        ensure!(
            gone.version == 1
                && gone.id == id
                && crate::identifier(id)
                && crate::identifier(&gone.module)
                && crate::identifier(&gone.profile)
                && matches!(gone.kind.as_str(), "text" | "document" | "bark")
                && matches!(gone.status.as_str(), "delivered" | "failed" | "suppressed")
                && gone.created_at.is_finite()
                && gone.created_at >= 0.0
                && gone.updated_at.is_finite()
                && gone.updated_at >= gone.created_at
                && gone.next_attempt.is_finite()
                && gone.expires_at.is_finite()
                && gone.expires_at > gone.updated_at
                && hash_valid(&gone.payload_sha256)
                && gone.callbacks.iter().all(|v| hash_valid(v))
                && gone
                    .error
                    .as_deref()
                    .is_none_or(|e| e == "retained_terminal")
                && ((gone.status != "delivered" && gone.receipt.is_null())
                    || (gone.status == "delivered"
                        && ((gone.kind == "bark" && gone.receipt == json!({"code":200}))
                            || (gone.kind != "bark"
                                && telegram_receipt(&gone.receipt)
                                && gone.receipt.as_object().is_some_and(|r| r.len() == 2))))),
            "invalid event history"
        );
        Ok(gone)
    }
    pub fn result(&self) -> Value {
        json!({"id":self.id,"profile":self.profile,"status":self.status,"attempts":self.attempts,
            "next_attempt":self.next_attempt,"updated_at":self.updated_at,"receipt":self.receipt,
            "error":self.error,"module":self.module})
    }
    pub fn verify_submit(&self, module: &str, payload: &Value) -> Result<()> {
        ensure!(self.module == module, "event belongs to another module");
        ensure!(
            self.cancelled || self.payload_sha256 == payload_digest(payload),
            "event id already has different content"
        );
        Ok(())
    }
    fn message_key(&self) -> Option<String> {
        (self.kind != "bark")
            .then(|| self.receipt["message_id"].as_i64())
            .flatten()
            .map(|mid| format!("{MESSAGE_PREFIX}{}:{mid}:{}", self.profile, self.id))
    }
}

pub(crate) fn read(db: &Connection, id: &str, now: f64) -> Result<Option<Gone>> {
    let raw: Option<String> = db
        .query_row(
            "SELECT value FROM kv WHERE key=?",
            [format!("{PREFIX}{id}")],
            |r| r.get(0),
        )
        .optional()?;
    let gone = raw.map(|raw| Gone::decode(&raw, id)).transpose()?;
    Ok(gone.filter(|gone| gone.expires_at > now))
}

/// Called only inside the event writer's transaction, before ID reuse.
pub(crate) fn remove_expired(tx: &Transaction<'_>, id: &str, now: f64) -> Result<()> {
    let key = format!("{PREFIX}{id}");
    let raw: Option<String> = tx
        .query_row("SELECT value FROM kv WHERE key=?", [&key], |r| r.get(0))
        .optional()?;
    if let Some(raw) = raw {
        let gone = Gone::decode(&raw, id)?;
        ensure!(gone.expires_at <= now, "event history still active");
        if let Some(key) = gone.message_key() {
            let raw: Option<String> = tx
                .query_row("SELECT value FROM kv WHERE key=?", [&key], |r| r.get(0))
                .optional()?;
            if let Some(raw) = raw {
                ensure!(
                    expires(&key, &raw)? == gone.expires_at,
                    "event history conflict"
                );
            }
            tx.execute("DELETE FROM kv WHERE key=?", [key])?;
        }
        tx.execute("DELETE FROM kv WHERE key=?", [key])?;
    }
    Ok(())
}

pub(crate) fn write(tx: &Transaction<'_>, gone: &Gone) -> Result<()> {
    let key = format!("{PREFIX}{}", gone.id);
    let raw: Option<String> = tx
        .query_row("SELECT value FROM kv WHERE key=?", [&key], |r| r.get(0))
        .optional()?;
    if let Some(raw) = raw {
        let previous = Gone::decode(&raw, &gone.id)?;
        // An event and a live tombstone must never disagree. Do not extend TTL.
        let mut expected = gone.clone();
        expected.expires_at = previous.expires_at;
        ensure!(
            previous == expected && previous.expires_at > crate::now(),
            "event history conflict"
        );
        if let Some(key) = previous.message_key() {
            let raw: String =
                tx.query_row("SELECT value FROM kv WHERE key=?", [&key], |r| r.get(0))?;
            ensure!(
                expires(&key, &raw)? == previous.expires_at,
                "event history conflict"
            );
        }
        return Ok(());
    }
    tx.execute(
        "INSERT INTO kv(key,value) VALUES(?,?)",
        params![key, serde_json::to_string(gone)?],
    )?;
    if let Some(key) = gone.message_key() {
        let value = Message {
            version: 1,
            event_id: gone.id.clone(),
            expires_at: gone.expires_at,
        };
        tx.execute(
            "INSERT INTO kv(key,value) VALUES(?,?)",
            params![key, serde_json::to_string(&value)?],
        )?;
    }
    Ok(())
}

pub(crate) fn expires(key: &str, raw: &str) -> Result<f64> {
    if let Some(id) = key.strip_prefix(PREFIX) {
        return Ok(Gone::decode(raw, id)?.expires_at);
    }
    let message: Message = serde_json::from_str(raw).context("invalid event history")?;
    let parts = key
        .strip_prefix(MESSAGE_PREFIX)
        .context("invalid event history")?
        .split(':')
        .collect::<Vec<_>>();
    ensure!(
        parts.len() == 3
            && crate::identifier(parts[0])
            && parts[1]
                .parse::<i64>()
                .is_ok_and(|mid| mid > 0 && mid.to_string() == parts[1])
            && parts[2] == message.event_id
            && message.version == 1
            && crate::identifier(&message.event_id)
            && message.expires_at.is_finite()
            && message.expires_at > 0.0,
        "invalid event history"
    );
    Ok(message.expires_at)
}

pub(crate) fn owns(
    db: &Connection,
    bot: &str,
    owner: &str,
    mid: i64,
    data: &Value,
    now: f64,
) -> Result<bool> {
    let prefix = format!("{MESSAGE_PREFIX}{bot}:{mid}:");
    let upper = format!("{MESSAGE_PREFIX}{bot}:{mid};");
    let mut q = db.prepare("SELECT key,value FROM kv WHERE key>=? AND key<?")?;
    for row in q.query_map(params![prefix, upper], |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
    })? {
        let (key, raw) = row?;
        if expires(&key, &raw)? <= now {
            continue;
        }
        let message: Message = serde_json::from_str(&raw)?;
        let gone = read(db, &message.event_id, now)?.context("event history missing")?;
        ensure!(
            gone.message_key().as_ref() == Some(&key) && gone.expires_at == message.expires_at,
            "event history conflict"
        );
        if crate::telegram::numeric_id(&gone.receipt["chat_id"]) == owner
            && gone.callbacks.contains(&payload_digest(data))
        {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Return None for uncertain, malformed or partial results; those keep their body.
pub(crate) fn from_event(event: &Value, now: f64, days: u32) -> Option<Gone> {
    let kind = event["kind"].as_str()?;
    let payload = &event["payload"];
    let status = event["status"].as_str()?;
    let receipt = &event["receipt"];
    let minimal = match (kind, status) {
        (_, "failed" | "suppressed") if receipt.is_null() => Value::Null,
        ("text", "delivered")
            if receipt.is_object()
                && telegram_receipt(receipt)
                && crate::telegram::chunks(payload["text"].as_str()?).len() == 1 =>
        {
            json!({"message_id":receipt["message_id"],"chat_id":receipt["chat_id"]})
        }
        ("text", "delivered") => {
            let parts = crate::telegram::chunks(payload["text"].as_str()?);
            let receipts = receipt.as_array()?;
            if receipts.is_empty()
                || receipts.len() != parts.len()
                || !receipts.iter().all(telegram_receipt)
            {
                return None;
            }
            let last = receipts.last()?;
            if !receipts.iter().all(|r| r["chat_id"] == last["chat_id"]) {
                return None;
            }
            json!({"message_id":last["message_id"],"chat_id":last["chat_id"]})
        }
        ("document", "delivered") if telegram_receipt(receipt) => {
            json!({"message_id":receipt["message_id"],"chat_id":receipt["chat_id"]})
        }
        ("bark", "delivered") if receipt["code"] == 200 => json!({"code":200}),
        _ => return None,
    };
    if !payload.is_object() {
        return None;
    }
    let mut callbacks = Vec::new();
    if let Some(rows) = payload["reply_markup"]["inline_keyboard"].as_array() {
        for row in rows {
            for button in row.as_array()? {
                if let Some(data) = button.get("callback_data") {
                    callbacks.push(payload_digest(data));
                }
            }
        }
    }
    callbacks.sort();
    callbacks.dedup();
    let gone = Gone {
        version: 1,
        id: event["id"].as_str()?.into(),
        module: event["module"].as_str()?.into(),
        profile: event["profile"].as_str()?.into(),
        kind: kind.into(),
        cancelled: event["job_id"] == "cancelled",
        status: status.into(),
        attempts: event["attempts"].as_u64()?,
        created_at: event["created_at"].as_f64()?,
        updated_at: event["updated_at"].as_f64()?,
        next_attempt: event["next_attempt"].as_f64()?,
        expires_at: now + f64::from(days) * 86400.0,
        payload_sha256: payload_digest(payload),
        receipt: minimal,
        error: event["error"]
            .as_str()
            .filter(|s| !s.is_empty())
            .map(|_| "retained_terminal".into()),
        callbacks,
    };
    Gone::decode(&serde_json::to_string(&gone).ok()?, &gone.id).ok()
}
