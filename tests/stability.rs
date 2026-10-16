use pylon::{config::Config, db::Store, queue};
use serde_json::{Value, json};

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
    let store = Store::new(&temp.path().join("events.sqlite3")).unwrap();
    let event = |id| json!({"id":id,"kind":"text","payload":{"text":"fixture"}});
    store
        .submit("fixture", &event("historic"), "fixture")
        .unwrap();
    store
        .finish("historic", "uncertain", None, "fixture", 0.0)
        .unwrap();
    store.uncertain_digest("fixture").unwrap();
    let count = || {
        store
            .connect()
            .unwrap()
            .query_row(
                "SELECT count(*) FROM events WHERE job_id='uncertain-summary'",
                [],
                |r| r.get::<_, i64>(0),
            )
            .unwrap()
    };
    assert_eq!(count(), 0);
    store
        .submit("fixture", &event("new-first"), "fixture")
        .unwrap();
    store
        .finish("new-first", "uncertain", None, "fixture", 0.0)
        .unwrap();
    store.uncertain_digest("fixture").unwrap();
    assert_eq!(count(), 1);
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
    assert_eq!(count(), 1);
    assert_eq!(store.event("historic").unwrap()["attempts"], 0);
    assert_eq!(store.event("historic").unwrap()["status"], "uncertain");
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 1);
    store.uncertain_digest_day("fixture", "2099-01-02").unwrap();
    assert_eq!(count(), 2);
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 0);
    store.uncertain_digest_day("fixture", "2099-01-03").unwrap();
    assert_eq!(count(), 2);
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
    let store = Store::new(&path).unwrap();
    let event = |id| json!({"id":id,"kind":"text","payload":{"text":"fixture"}});
    for id in ["historical", "unreported"] {
        store.submit("fixture", &event(id), "fixture").unwrap();
        store.finish(id, "uncertain", None, "fixture", 0.0).unwrap();
    }
    store
        .put("uncertain:watermark", &json!(["historical"]))
        .unwrap();
    store
        .put("uncertain:summary:2099-01-01", &json!(true))
        .unwrap();
    store.uncertain_digest_day("fixture", "2099-01-01").unwrap();
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 1);
    store
        .finish("unreported", "uncertain", None, "fixture", 0.0)
        .unwrap();
    assert_eq!(store.get("uncertain:counter:v2").unwrap()["pending"], 1);
    let reopened = Store::new(&path).unwrap();
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
    store
        .put("uncertain:watermark", &json!(["old-version-event"]))
        .unwrap();
    let upgraded = Store::new(&path).unwrap();
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
