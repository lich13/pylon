use crate::{config::Config, db::Store};
use anyhow::{Context, Result, ensure};
use rusqlite::params;
use serde_json::{Value, json};
use std::path::Path;

/// Import gateway-owned rows only. Business tables stay at their existing paths.
/// Mapping is supplied by the deployment, never compiled business identifiers.
pub fn import(config: &Config, source: &Path, mapping: &Path) -> Result<Value> {
    ensure!(
        config.home.join("run/freeze").is_file(),
        "freeze new triggers before state migration"
    );
    let mapping: Value = serde_json::from_slice(&std::fs::read(mapping)?)?;
    let settings = config.settings()?;
    let store = Store::new(&config.db())?;
    ensure!(
        store.get("migration:complete")?.is_null(),
        "migration already completed; never overwrite live state"
    );
    let mut db = store.connect()?;
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    ensure!(
        tx.query_row("SELECT count(*) FROM events", [], |r| r.get::<_, i64>(0))? == 0,
        "target contains events; state merge required"
    );
    tx.execute(
        "ATTACH DATABASE ? AS previous",
        [source.to_str().context("invalid source path")?],
    )?;
    let inflight: i64 = tx.query_row(
        "SELECT count(*) FROM previous.runs WHERE status='running' AND finished_at IS NULL",
        [],
        |r| r.get(0),
    )?;
    ensure!(
        inflight == 0,
        "unfinished runs require reconciliation before migration"
    );
    for table in ["updates", "runs", "run_events"] {
        tx.execute(
            &format!("INSERT INTO {table} SELECT * FROM previous.{table}"),
            [],
        )?;
    }
    tx.execute("INSERT INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,receipt,error) SELECT id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,receipt,error FROM previous.events",[])?;
    for (job, module) in mapping["jobs"]
        .as_object()
        .context("job ownership mapping required")?
    {
        let module = module.as_str().context("invalid module mapping")?;
        tx.execute(
            "UPDATE events SET module=? WHERE job_id=?",
            params![module, job],
        )?;
    }
    tx.execute("UPDATE events SET module='gateway' WHERE job_id='bot'", [])?;
    for entry in mapping["event_owners"].as_array().unwrap_or(&vec![]) {
        tx.execute(
            "UPDATE events SET module=? WHERE job_id=?",
            params![
                entry["module"].as_str().context("module required")?,
                entry["job_id"].as_str().context("job id required")?
            ],
        )?;
    }
    for entry in mapping["event_payload_owners"]
        .as_array()
        .unwrap_or(&vec![])
    {
        let field = entry["field"].as_str().context("payload field required")?;
        ensure!(crate::identifier(field), "invalid payload field");
        tx.execute(
            "UPDATE events SET module=? WHERE job_id=? AND json_extract(payload,?) IS NOT NULL",
            params![
                entry["module"].as_str().context("module required")?,
                entry["job_id"].as_str().context("job id required")?,
                format!("$.{field}")
            ],
        )?;
    }
    tx.execute("INSERT INTO kv SELECT key,value FROM previous.kv WHERE key LIKE 'offset:%' OR key='commands_not_before'",[])?;
    let tables = {
        let mut q = tx.prepare("SELECT name FROM previous.sqlite_master WHERE type='table'")?;
        q.query_map([], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?
    };
    if tables.iter().any(|s| s == "market_web_tokens") {
        for (bot, value) in &settings.bots {
            tx.execute("INSERT INTO web_tokens SELECT hash,?,profile,?,expires_at,used_at FROM previous.market_web_tokens WHERE profile=?",params![value.owner,mapping["legacy_login_workspace"].as_str().unwrap_or("market"),bot])?;
            tx.execute("INSERT INTO web_sessions SELECT hash,?,profile,csrf,expires_at FROM previous.market_web_sessions WHERE profile=?",params![value.owner,bot])?;
        }
    }
    let counts = json!({"events":tx.query_row("SELECT count(*) FROM events",[],|r|r.get::<_,i64>(0))?,"uncertain":tx.query_row("SELECT count(*) FROM events WHERE status='uncertain'",[],|r|r.get::<_,i64>(0))?,"sessions":tx.query_row("SELECT count(*) FROM web_sessions",[],|r|r.get::<_,i64>(0))?});
    tx.execute(
        "INSERT INTO kv VALUES('migration:complete',?)",
        [counts.to_string()],
    )?;
    tx.commit()?;
    Ok(counts)
}

/// Idempotent internal diagnostics migration; public tables and IDs stay unchanged.
pub fn internal_state(db: &rusqlite::Connection) -> Result<()> {
    db.execute_batch(
            "CREATE TRIGGER IF NOT EXISTS uncertain_count_update_v2 AFTER UPDATE OF status ON events
             WHEN NEW.status='uncertain' AND OLD.status<>'uncertain' AND NEW.job_id<>'uncertain-summary'
             BEGIN UPDATE kv SET value=json_set(value,'$.pending',json_extract(value,'$.pending')+1)
               WHERE key='uncertain:counter:v2'; END;
             CREATE TRIGGER IF NOT EXISTS uncertain_count_insert_v2 AFTER INSERT ON events
             WHEN NEW.status='uncertain' AND NEW.job_id<>'uncertain-summary'
             BEGIN UPDATE kv SET value=json_set(value,'$.pending',json_extract(value,'$.pending')+1)
               WHERE key='uncertain:counter:v2'; END;"
        )?;
    Ok(())
}
