use pylon::{config::Config, db::Store, queue};
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::path::Path;

fn insert_uncertain_event(db: &Connection, id: &str, job_id: &str) {
    db.execute(
        "INSERT INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at)
         VALUES(?, 'fixture', ?, 'text', '{}', 'uncertain', 0, 0, 1.0, 1.0)",
        params![id, job_id],
    )
    .unwrap();
}

fn create_legacy_db(path: &Path, uncertain_events: &[(&str, &str)], watermark: Option<Value>) {
    let db = Connection::open(path).unwrap();
    // The legacy schema has the same public tables, but no v2 counter or triggers.
    db.execute_batch(include_str!("../rust/schema.sql"))
        .unwrap();
    let counter: i64 = db
        .query_row(
            "SELECT count(*) FROM kv WHERE key='uncertain:counter:v2'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let triggers: i64 = db
        .query_row(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'uncertain_count_%'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(counter, 0);
    assert_eq!(triggers, 0);
    for (id, job_id) in uncertain_events {
        insert_uncertain_event(&db, id, job_id);
    }
    if let Some(watermark) = watermark {
        db.execute(
            "INSERT INTO kv(key,value) VALUES('uncertain:watermark',?)",
            [watermark.to_string()],
        )
        .unwrap();
    }
}

fn summary_count(store: &Store) -> u64 {
    store
        .connect()
        .unwrap()
        .query_row(
            "SELECT count(*) FROM events WHERE job_id='uncertain-summary'",
            [],
            |r| r.get(0),
        )
        .unwrap()
}

#[test]
fn delivery_lanes_claim_independently_and_once() {
    let temp = tempfile::tempdir().unwrap();
    let store = Store::new(&temp.path().join("events.sqlite3")).unwrap();
    for (id, kind) in [("bark-old", "bark"), ("text-new", "text")] {
        store
            .submit(
                "fixture",
                &json!({"id":id,"kind":kind,"payload":{}}),
                "fixture",
            )
            .unwrap();
    }
    assert_eq!(
        store.claim_lane("fixture", Some(false)).unwrap().unwrap()["id"],
        "text-new"
    );
    assert_eq!(
        store.claim_lane("fixture", Some(true)).unwrap().unwrap()["id"],
        "bark-old"
    );
    assert!(store.claim_lane("fixture", Some(false)).unwrap().is_none());
    assert!(store.claim_lane("fixture", Some(true)).unwrap().is_none());
}

#[test]
fn uncertain_baseline_and_daily_summary_never_replay_history_or_recurse() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("events.sqlite3");
    create_legacy_db(&path, &[("historic", "fixture")], Some(json!(["historic"])));
    let store = Store::new(&path).unwrap();
    let event = |id| json!({"id":id,"kind":"text","payload":{"text":"fixture"}});
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 0);
    store.uncertain_digest("fixture").unwrap();
    assert_eq!(summary_count(&store), 0);
    store
        .submit("fixture", &event("new-first"), "fixture")
        .unwrap();
    store
        .finish("new-first", "uncertain", None, "fixture", 0.0)
        .unwrap();
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 1);
    store.uncertain_digest("fixture").unwrap();
    assert_eq!(summary_count(&store), 1);
    store
        .connect()
        .unwrap()
        .execute(
            "UPDATE events SET status='uncertain' WHERE job_id='uncertain-summary'",
            [],
        )
        .unwrap();
    store
        .submit("fixture", &event("new-later"), "fixture")
        .unwrap();
    store
        .finish("new-later", "uncertain", None, "fixture", 0.0)
        .unwrap();
    store.uncertain_digest("fixture").unwrap();
    assert_eq!(summary_count(&store), 1);
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 1);
    assert_eq!(store.event("historic").unwrap()["attempts"], 0);
    assert_eq!(store.event("historic").unwrap()["status"], "uncertain");

    // Simulate a summary event committed before its daily marker. It must prevent
    // this day's retry from clearing the pending count or creating another event.
    let blocked_day = "2099-01-02";
    let blocked_id = pylon::digest(format!("uncertain:summary:{blocked_day}"));
    let db = store.connect().unwrap();
    insert_uncertain_event(&db, &blocked_id[..32], "uncertain-summary");
    drop(db);
    assert!(
        store
            .get(&format!("uncertain:summary:{blocked_day}"))
            .unwrap()
            .is_null()
    );
    store.uncertain_digest_day("fixture", blocked_day).unwrap();
    assert_eq!(summary_count(&store), 2);
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 1);
    store.uncertain_digest_day("fixture", "2099-01-02").unwrap();
    assert_eq!(summary_count(&store), 2);
    store.uncertain_digest_day("fixture", "2099-01-03").unwrap();
    assert_eq!(summary_count(&store), 3);
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 0);
    store.uncertain_digest_day("fixture", "2099-01-04").unwrap();
    assert_eq!(summary_count(&store), 3);
}

#[test]
fn fresh_uncertain_store_counts_before_first_digest_and_preserves_existing_counter() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("events.sqlite3");
    let store = Store::new(&path).unwrap();
    let event = json!({
        "id":"fresh-uncertain",
        "kind":"text",
        "payload":{"text":"fixture"}
    });
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 0);
    store.submit("fixture", &event, "fixture").unwrap();
    store
        .finish("fresh-uncertain", "uncertain", None, "fixture", 0.0)
        .unwrap();
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 1);

    // An old watermark that disagrees with the active v2 count cannot reseed it.
    store
        .put("uncertain:watermark", &json!(["fresh-uncertain"]))
        .unwrap();
    let reopened = Store::new(&path).unwrap();
    assert_eq!(reopened.get("uncertain:counter:v2").unwrap()["pending"], 1);
    reopened
        .uncertain_digest_day("fixture", "2099-02-01")
        .unwrap();
    assert_eq!(reopened.get("uncertain:counter:v2").unwrap()["pending"], 0);

    let reopened_again = Store::new(&path).unwrap();
    assert_eq!(
        reopened_again.get("uncertain:counter:v2").unwrap()["pending"],
        0
    );
    assert_eq!(summary_count(&reopened_again), 1);
}

#[test]
fn legacy_uncertain_history_without_watermark_seeds_counter_once() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("events.sqlite3");
    create_legacy_db(
        &path,
        &[("legacy-one", "fixture"), ("legacy-two", "fixture")],
        None,
    );

    let store = Store::new(&path).unwrap();
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 2);
    let reopened = Store::new(&path).unwrap();
    assert_eq!(reopened.get("uncertain:counter:v2").unwrap()["pending"], 2);
    reopened
        .uncertain_digest_day("fixture", "2099-02-02")
        .unwrap();
    assert_eq!(reopened.get("uncertain:counter:v2").unwrap()["pending"], 0);
    assert_eq!(summary_count(&reopened), 1);
}

#[test]
fn uncertain_triggers_rebuild_a_missing_counter_on_insert_and_update() {
    let temp = tempfile::tempdir().unwrap();
    let store = Store::new(&temp.path().join("events.sqlite3")).unwrap();
    let event = |id| json!({"id":id,"kind":"text","payload":{"text":"fixture"}});
    store
        .submit("fixture", &event("already-uncertain"), "fixture")
        .unwrap();
    store
        .finish("already-uncertain", "uncertain", None, "fixture", 0.0)
        .unwrap();
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 1);

    let db = store.connect().unwrap();
    db.execute("DELETE FROM kv WHERE key='uncertain:counter:v2'", [])
        .unwrap();
    insert_uncertain_event(&db, "ignored-summary", "uncertain-summary");
    insert_uncertain_event(&db, "inserted-uncertain", "fixture");
    drop(db);
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 2);

    let db = store.connect().unwrap();
    db.execute("DELETE FROM kv WHERE key='uncertain:counter:v2'", [])
        .unwrap();
    drop(db);
    store
        .submit("fixture", &event("updated-uncertain"), "fixture")
        .unwrap();
    store
        .connect()
        .unwrap()
        .execute(
            "UPDATE events SET status='uncertain' WHERE id='updated-uncertain'",
            [],
        )
        .unwrap();
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 3);

    store
        .connect()
        .unwrap()
        .execute(
            "UPDATE events SET status='uncertain' WHERE id='updated-uncertain'",
            [],
        )
        .unwrap();
    store
        .finish("inserted-uncertain", "uncertain", None, "fixture", 0.0)
        .unwrap();
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 3);
}

#[test]
fn summary_flush_repairs_a_stale_cross_process_file_without_rewriting_stable_bytes() {
    let temp = tempfile::tempdir().unwrap();
    let config = Config::new(temp.path().to_path_buf());
    let path = config.home.join("run/task-summary.json");
    queue::summary_flush(&config).unwrap();
    let empty: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    assert_eq!(empty["requests"], json!([]));

    let id = queue::admit(&config, "fixture-job", "fixture", "topic", false, "trigger").unwrap();
    assert_eq!(id, "trigger");
    // This epoch value loses one ULP with serde_json's approximate parser.
    // Reading an unchanged summary must preserve its exact SQLite timestamps.
    queue::connect(&config)
        .unwrap()
        .execute(
            "UPDATE requests SET created_at=?,updated_at=? WHERE id=?",
            params![1792908531.0390239_f64, 1792908531.0390239_f64, id],
        )
        .unwrap();
    queue::summary_flush(&config).unwrap();
    let expected: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    assert_eq!(expected["requests"].as_array().unwrap().len(), 1);
    assert_eq!(expected["requests"][0]["id"], id);

    // Model another process writing a stale summary after the local writer cached its view.
    std::fs::write(
        &path,
        serde_json::to_vec(&json!({
            "updated_at": pylon::now(),
            "requests": [{"id": "other-process-stale-row"}]
        }))
        .unwrap(),
    )
    .unwrap();

    queue::summary_flush(&config).unwrap();
    let repaired_bytes = std::fs::read(&path).unwrap();
    let repaired: Value = serde_json::from_slice(&repaired_bytes).unwrap();
    assert_eq!(repaired["requests"], expected["requests"]);

    queue::summary_flush(&config).unwrap();
    assert_eq!(std::fs::read(&path).unwrap(), repaired_bytes);
}

#[test]
fn counter_migration_is_atomic_idempotent_and_does_not_count_receipt_retries() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("state.sqlite3");
    create_legacy_db(
        &path,
        &[("historical", "fixture"), ("unreported", "fixture")],
        Some(json!(["historical"])),
    );
    let old_db = Connection::open(&path).unwrap();
    old_db
        .execute(
            "INSERT INTO kv(key,value) VALUES('uncertain:summary:2099-01-01','true')",
            [],
        )
        .unwrap();
    drop(old_db);

    let store = Store::new(&path).unwrap();
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 1);
    store.uncertain_digest_day("fixture", "2099-01-01").unwrap();
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 1);
    store
        .finish("unreported", "uncertain", None, "fixture", 0.0)
        .unwrap();
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 1);
    let reopened = Store::new(&path).unwrap();
    assert_eq!(reopened.get("uncertain:counter:v2").unwrap()["pending"], 1);
    reopened
        .uncertain_digest_day("fixture", "2099-01-02")
        .unwrap();
    assert_eq!(reopened.get("uncertain:counter:v2").unwrap()["pending"], 0);
    let mut db = reopened.connect().unwrap();
    let tx = db.transaction().unwrap();
    tx.execute(
        "UPDATE events SET status='pending' WHERE id='unreported'",
        [],
    )
    .unwrap();
    tx.execute(
        "UPDATE events SET status='uncertain' WHERE id='unreported'",
        [],
    )
    .unwrap();
    tx.rollback().unwrap();
    assert_eq!(reopened.get("uncertain:counter:v2").unwrap()["pending"], 0);
    assert_eq!(reopened.event("historical").unwrap()["status"], "uncertain");
}

#[test]
fn upgrade_retires_only_web_tables_and_preserves_existing_gateway_state() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("state.sqlite3");
    let store = Store::new(&path).unwrap();
    let db = store.connect().unwrap();
    insert_uncertain_event(&db, "fixture-existing", "fixture");
    db.execute_batch("CREATE TABLE web_tokens(hash TEXT); CREATE TABLE web_sessions(hash TEXT);
        INSERT INTO web_tokens VALUES('fixture'); INSERT INTO web_sessions VALUES('fixture');
        INSERT INTO contexts VALUES('fixture-context','fixture','fixture','[]',0,'service:relay','fixture');
        INSERT INTO kv VALUES('legacy_import_completed','true');").unwrap();
    drop(db);
    drop(store);
    for _ in 0..2 {
        let store = Store::new(&path).unwrap();
        let db = store.connect().unwrap();
        let tables: i64 = db
            .query_row(
                "SELECT count(*) FROM sqlite_schema WHERE name IN ('web_tokens','web_sessions')",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(tables, 0);
        assert_eq!(
            store.event("fixture-existing").unwrap()["status"],
            "uncertain"
        );
        assert_eq!(store.get("legacy_import_completed").unwrap(), true);
        assert_eq!(
            db.query_row(
                "SELECT count(*) FROM contexts WHERE hash='fixture-context'",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            1
        );
    }
}

#[test]
fn configuration_rejection_is_transactional_deduplicated_and_expires() {
    let temp = tempfile::tempdir().unwrap();
    let store = Store::new(&temp.path().join("state.sqlite3")).unwrap();
    store
        .configuration_rejected("fixture-hash", Some("fixture"))
        .unwrap();
    store
        .configuration_rejected("fixture-hash", Some("fixture"))
        .unwrap();
    let db = store.connect().unwrap();
    let count: u64 = db
        .query_row("SELECT count(*) FROM events", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 1);
    let event = store.claim("fixture").unwrap().unwrap();
    assert!(event["payload"]["expires_at"].as_f64().unwrap() > pylon::now());
    assert!(event["payload"]["expires_at"].as_f64().unwrap() <= pylon::now() + 600.0);
    store
        .configuration_rejected("fixture-no-route", None)
        .unwrap();
    assert_eq!(
        store.get("configuration-rejected").unwrap()["fingerprint"],
        "fixture-no-route"
    );
    assert_eq!(
        db.query_row("SELECT count(*) FROM events", [], |r| r.get::<_, u64>(0))
            .unwrap(),
        1
    );
}

#[test]
fn rollback_counter_retirement_keeps_old_gateway_reports_from_being_repeated() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("state.sqlite3");
    let store = Store::new(&path).unwrap();
    store.uncertain_digest_day("fixture", "2099-01-01").unwrap();
    // Freeze/stop precede this rollback transaction. Preserve the v2 aggregate,
    // while the old gateway resumes maintaining its own event watermark.
    store
        .connect()
        .unwrap()
        .execute_batch(
            "BEGIN IMMEDIATE;
         INSERT INTO kv VALUES('uncertain:watermark','[]');
         UPDATE kv SET key='uncertain:counter:v2:retired' WHERE key='uncertain:counter:v2';
         COMMIT;",
        )
        .unwrap();
    store
        .submit(
            "fixture",
            &json!({"id":"old-version-event","kind":"text","payload":{}}),
            "fixture",
        )
        .unwrap();
    store
        .finish("old-version-event", "uncertain", None, "fixture", 0.0)
        .unwrap();
    assert!(store.get("uncertain:counter:v2").unwrap().is_null());
    store
        .put("uncertain:watermark", &json!(["old-version-event"]))
        .unwrap();
    let upgraded = Store::new(&path).unwrap();
    assert_eq!(upgraded.get("uncertain:counter:v2").unwrap()["pending"], 0);
    upgraded
        .uncertain_digest_day("fixture", "2099-01-03")
        .unwrap();
    assert_eq!(upgraded.get("uncertain:counter:v2").unwrap()["pending"], 0);
    assert_eq!(
        upgraded.get("uncertain:counter:v2:retired").unwrap()["version"],
        2
    );
    assert_eq!(
        upgraded.event("old-version-event").unwrap()["status"],
        "uncertain"
    );
    let count: u64 = upgraded
        .connect()
        .unwrap()
        .query_row(
            "SELECT count(*) FROM events WHERE job_id='uncertain-summary'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 0);
}
