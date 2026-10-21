use super::*;
use crate::{event_history, optimization::Lane};
use std::os::unix::fs::PermissionsExt;

fn store() -> (tempfile::TempDir, Store) {
    let temp = tempfile::tempdir().unwrap();
    let store = Store::new(&temp.path().join("gateway/state.sqlite3")).unwrap();
    (temp, store)
}
fn event(
    store: &Store,
    id: &str,
    status: &str,
    kind: &str,
    payload: Value,
    receipt: Value,
) -> Value {
    let value = json!({"id":id,"kind":kind,"payload":payload});
    store.submit("fixture", &value, "bot").unwrap();
    store
        .finish(
            id,
            status,
            (!receipt.is_null()).then_some(&receipt),
            "fixture private error",
            0.0,
        )
        .unwrap();
    let old = crate::now() - 86400.0 * 40.0;
    store
        .connect()
        .unwrap()
        .execute(
            "UPDATE events SET created_at=?,updated_at=? WHERE id=?",
            params![old, old, id],
        )
        .unwrap();
    value
}
fn clean(store: &Store, kind: Kind) -> Batch {
    batch(
        &mut store.connect().unwrap(),
        kind,
        &mut Cursor::default(),
        &Policy::default(),
        crate::now(),
        BATCH,
    )
    .unwrap()
}
fn receipt() -> Value {
    json!([{"message_id":17,"chat_id":101}])
}

#[test]
fn policy_defaults_and_invalid_values() {
    let policy: Policy = serde_json::from_value(json!({})).unwrap();
    assert_eq!(policy.events_days, 7);
    assert_eq!(policy.placeholders_days, 90);
    assert!(policy.enabled);
    for raw in [
        json!({"events_days":0}),
        json!({"requests_days":36501}),
        json!({"updates_per_bot":0}),
    ] {
        assert!(
            serde_json::from_value::<Policy>(raw)
                .unwrap()
                .validate()
                .is_err()
        );
    }
    assert!(serde_json::from_value::<Policy>(json!({"runs_days":-1})).is_err());
}

#[test]
fn terminal_bodies_become_minimal_idempotent_results_and_protected_events_stay() {
    let (_temp, store) = store();
    let payload = json!({"text":"fixture body","reply_markup":{"inline_keyboard":[[{"text":"fixture button","callback_data":"fixture:action"}]]},"number":-0.0});
    let original = event(
        &store,
        "delivered",
        "delivered",
        "text",
        payload.clone(),
        receipt(),
    );
    for status in ["pending", "sending", "uncertain", "unknown"] {
        event(
            &store,
            status,
            status,
            "text",
            json!({"text":"fixture"}),
            Value::Null,
        );
    }
    event(
        &store,
        "partial",
        "failed",
        "text",
        json!({"text":"fixture"}),
        receipt(),
    );
    event(
        &store,
        "incomplete",
        "delivered",
        "text",
        json!({"text":"x".repeat(10000)}),
        receipt(),
    );
    event(
        &store,
        "reference",
        "delivered",
        "text",
        json!({"text":"fixture"}),
        receipt(),
    );
    store
        .connect()
        .unwrap()
        .execute("INSERT INTO run_events VALUES('protected','reference')", [])
        .unwrap();
    event(
        &store,
        "summary",
        "delivered",
        "text",
        json!({"text":"fixture"}),
        receipt(),
    );
    store
        .connect()
        .unwrap()
        .execute(
            "UPDATE events SET job_id='uncertain-summary' WHERE id='summary'",
            [],
        )
        .unwrap();
    event(&store, "recent", "failed", "text", json!({}), Value::Null);
    store
        .connect()
        .unwrap()
        .execute(
            "UPDATE events SET updated_at=? WHERE id='recent'",
            [crate::now()],
        )
        .unwrap();
    event(
        &store,
        "invalid",
        "delivered",
        "text",
        json!({"text":"fixture"}),
        json!([{"message_id":0,"chat_id":101}]),
    );
    event(
        &store,
        "bark",
        "delivered",
        "bark",
        json!({"body":"fixture private body"}),
        json!({"code":200,"http_status":200}),
    );
    event(
        &store,
        "document",
        "delivered",
        "document",
        json!({"path":"/fixture/private","caption":"fixture caption"}),
        json!({"message_id":18,"chat_id":101,"document":{"file_id":"fixture attachment"}}),
    );
    let (_wake, changed) = store.runtime.delivery_listener("bot", Lane::Telegram);
    let cleaned = clean(&store, Kind::Events);
    assert_eq!(cleaned.deleted["events"], 3);
    assert!(!changed.has_changed().unwrap());
    assert_eq!(
        store.event("delivered").unwrap()["receipt"],
        json!({"message_id":17,"chat_id":101})
    );
    assert_eq!(
        store.event("delivered").unwrap()["error"],
        "retained_terminal"
    );
    assert_eq!(
        store.submit("fixture", &original, "bot").unwrap()["status"],
        "delivered"
    );
    let mut equal = original.clone();
    equal["payload"]["number"] = json!(0.0);
    assert_eq!(
        store.submit("fixture", &equal, "bot").unwrap()["status"],
        "delivered"
    );
    assert!(!changed.has_changed().unwrap());
    assert!(store.submit("other", &original, "bot").is_err());
    equal["payload"]["text"] = json!("changed");
    assert!(store.submit("fixture", &equal, "bot").is_err());
    assert!(store.has_event("delivered").unwrap());
    assert_eq!(
        store
            .cancel_or_insert("fixture", "delivered", Some("bot"))
            .unwrap()["status"],
        "delivered"
    );
    assert_eq!(
        store
            .connect()
            .unwrap()
            .query_row(
                "SELECT count(*) FROM events WHERE id='delivered'",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        0
    );
    let raw = store.get("event-gone:delivered").unwrap().to_string();
    for secret in [
        "fixture body",
        "fixture button",
        "fixture:action",
        "fixture private error",
        "reply_markup",
        "caption",
        "document",
    ] {
        assert!(!raw.contains(secret));
    }
    assert!(
        !store
            .get("event-gone:document")
            .unwrap()
            .to_string()
            .contains("/fixture/private")
    );
    let expiry = store.get("event-gone:delivered").unwrap()["expires_at"]
        .as_f64()
        .unwrap();
    assert!((expiry - crate::now() - 90.0 * 86400.0).abs() < 5.0);
    let query = json!({"from":{"id":101},"message":{"message_id":17,"chat":{"id":101,"type":"private"}},"data":"fixture:action"});
    assert!(store.own_callback("bot", "101", &query).unwrap());
    assert!(!store.own_callback("other", "101", &query).unwrap());
    assert!(!store.own_callback("bot", "102", &query).unwrap());
    let mut wrong = query.clone();
    wrong["data"] = json!("fixture:wrong");
    assert!(!store.own_callback("bot", "101", &wrong).unwrap());
    let db = store.connect().unwrap();
    assert!(
        event_history::read(&db, "delivered", expiry)
            .unwrap()
            .is_none()
    );
    assert!(!event_history::owns(&db, "bot", "101", 17, &query["data"], expiry).unwrap());
}

#[test]
fn cancelled_history_and_expiry_reuse_fail_closed_on_damage() {
    let (_temp, store) = store();
    store
        .cancel_or_insert("fixture", "cancelled", Some("bot"))
        .unwrap();
    store
        .connect()
        .unwrap()
        .execute("UPDATE events SET created_at=1,updated_at=1", [])
        .unwrap();
    clean(&store, Kind::Events);
    let different = json!({"id":"cancelled","kind":"text","payload":{"text":"new"}});
    assert_eq!(
        store.submit("fixture", &different, "bot").unwrap()["status"],
        "suppressed"
    );
    let mut gone = store.get("event-gone:cancelled").unwrap();
    gone["expires_at"] = json!(crate::now() - 1.0);
    store.put("event-gone:cancelled", &gone).unwrap();
    assert!(!store.has_event("cancelled").unwrap());
    assert_eq!(
        store.submit("fixture", &different, "bot").unwrap()["status"],
        "pending"
    );
    assert!(store.get("event-gone:cancelled").unwrap().is_null());
    store
        .put("event-gone:broken", &json!({"version":999}))
        .unwrap();
    assert!(store.has_event("broken").is_err());
    assert!(store.event("broken").is_err());
    assert!(
        store
            .submit(
                "fixture",
                &json!({"id":"broken","kind":"text","payload":{}}),
                "bot"
            )
            .is_err()
    );
    assert!(
        store
            .cancel_or_insert("fixture", "broken", Some("bot"))
            .is_err()
    );
}

#[test]
fn deletion_and_placeholder_insertion_rollback_together_without_advancing_cursor() {
    let (_temp, store) = store();
    event(
        &store,
        "first",
        "delivered",
        "text",
        json!({"text":"fixture"}),
        receipt(),
    );
    event(
        &store,
        "second",
        "delivered",
        "text",
        json!({"text":"fixture"}),
        receipt(),
    );
    let mut db = store.connect().unwrap();
    db.execute_batch("CREATE TRIGGER reject_retention BEFORE DELETE ON events WHEN old.id='second' BEGIN SELECT raise(ABORT,'fixture fault'); END").unwrap();
    let mut cursor = Cursor::default();
    assert!(
        batch(
            &mut db,
            Kind::Events,
            &mut cursor,
            &Policy::default(),
            crate::now(),
            BATCH,
        )
        .is_err()
    );
    assert_eq!(cursor.row, 0);
    assert_eq!(
        db.query_row("SELECT count(*) FROM events", [], |r| r.get::<_, u64>(0))
            .unwrap(),
        2
    );
    assert!(store.get("event-gone:first").unwrap().is_null());
    db.execute_batch("DROP TRIGGER reject_retention").unwrap();
    assert_eq!(
        batch(
            &mut db,
            Kind::Events,
            &mut cursor,
            &Policy::default(),
            crate::now(),
            BATCH,
        )
        .unwrap()
        .deleted["events"],
        2
    );
}

#[test]
fn history_retry_preserves_expiry_and_rejects_conflicting_message_records() {
    let (_temp, store) = store();
    let source = json!({"id":"retry","module":"fixture","profile":"bot","kind":"text",
        "job_id":"fixture","status":"delivered","attempts":1,"created_at":1.0,"updated_at":2.0,
        "next_attempt":0.0,"payload":{"text":"fixture"},"receipt":[{"message_id":17,"chat_id":101}]});
    let gone = event_history::from_event(&source, crate::now(), 90).unwrap();
    let mut db = store.connect().unwrap();
    let tx = db.transaction().unwrap();
    event_history::write(&tx, &gone).unwrap();
    tx.commit().unwrap();
    let later = event_history::from_event(&source, crate::now() + 86400.0, 90).unwrap();
    let tx = db.transaction().unwrap();
    event_history::write(&tx, &later).unwrap();
    tx.commit().unwrap();
    assert_eq!(
        store.get("event-gone:retry").unwrap()["expires_at"],
        gone.expires_at
    );
    let mut damaged = store.get("event-gone-msg:bot:17:retry").unwrap();
    damaged["expires_at"] = json!(gone.expires_at + 1.0);
    store.put("event-gone-msg:bot:17:retry", &damaged).unwrap();
    let tx = db.transaction().unwrap();
    assert!(event_history::write(&tx, &later).is_err());
    assert!(event_history::remove_expired(&tx, "retry", gone.expires_at + 2.0).is_err());
    drop(tx);
    assert!(!store.get("event-gone:retry").unwrap().is_null());
    assert!(event_history::expires("event-gone-msg:invalid:retry", &damaged.to_string()).is_err());
}

#[test]
fn digest_matches_value_equality_including_recursive_float_zero() {
    let a = json!({"z":[-0.0,{"zero":-0.0}],"a":1});
    let b = json!({"a":1,"z":[0.0,{"zero":0.0}]});
    assert_eq!(a, b);
    assert_eq!(
        event_history::payload_digest(&a),
        event_history::payload_digest(&b)
    );
    for (a, b) in [
        (json!(1), json!(1.0)),
        (json!(0), json!(0.0)),
        (json!("0"), json!(0)),
        (json!([1, 2]), json!([2, 1])),
    ] {
        assert_ne!(a, b);
        assert_ne!(
            event_history::payload_digest(&a),
            event_history::payload_digest(&b)
        );
    }
    assert_eq!(event_history::payload_digest(&json!({})).len(), 64);
}

#[test]
fn runs_keep_latest_ties_uncertain_and_incomplete_and_remove_children_atomically() {
    let (_temp, store) = store();
    let mut db = store.connect().unwrap();
    for (id, job, started, finished, status) in [
        ("old", "a", 1.0, Some(2.0), "ok"),
        ("tie1", "a", 3.0, Some(4.0), "ok"),
        ("tie2", "a", 3.0, Some(4.0), "failed"),
        ("running", "a", 1.0, None, "running"),
        ("uncertain", "a", 1.0, Some(2.0), "uncertain"),
        ("incomplete", "a", 1.0, None, "ok"),
        ("sole", "b", 1.0, Some(2.0), "ok"),
    ] {
        db.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,NULL)",
            params![id, job, started, finished, status],
        )
        .unwrap();
    }
    db.execute("INSERT INTO run_events VALUES('old','event')", [])
        .unwrap();
    for id in ["old", "tie1", "absent"] {
        store.put(&format!("run-result:{id}"), &json!({})).unwrap();
    }
    let cleaned = batch(
        &mut db,
        Kind::Runs,
        &mut Cursor::default(),
        &Policy::default(),
        crate::now(),
        BATCH,
    )
    .unwrap();
    assert_eq!(cleaned.deleted["runs"], 1);
    assert_eq!(cleaned.deleted["run_events"], 1);
    assert!(store.get("run-result:old").unwrap().is_null());
    clean(&store, Kind::Orphans);
    assert!(store.get("run-result:absent").unwrap().is_null());
    assert_eq!(store.get("run-result:tie1").unwrap(), json!({}));
    assert_eq!(
        db.query_row("SELECT count(*) FROM runs", [], |r| r.get::<_, u64>(0))
            .unwrap(),
        6
    );
}

#[test]
fn update_window_and_expiring_auth_keep_watermarks_and_service_contexts() {
    let (_temp, store) = store();
    let mut db = store.connect().unwrap();
    for id in 1..=7 {
        db.execute(
            "INSERT INTO updates(profile,update_id,payload,status) VALUES('bot',?,'{}',?)",
            params![
                id,
                if id == 1 {
                    "uncertain"
                } else if id == 2 {
                    "unknown"
                } else {
                    "done"
                }
            ],
        )
        .unwrap();
    }
    store.put("offset:bot", &json!(8)).unwrap();
    let policy = Policy {
        updates_per_bot: 3,
        ..Policy::default()
    };
    assert_eq!(
        batch(
            &mut db,
            Kind::Updates,
            &mut Cursor::default(),
            &policy,
            crate::now(),
            BATCH,
        )
        .unwrap()
        .deleted["updates"],
        2
    );
    assert_eq!(store.get("offset:bot").unwrap(), 8);
    for (id, expires) in [
        ("service", 0.0),
        ("old", 1.0),
        ("recent", crate::now() - 10.0),
    ] {
        db.execute(
            "INSERT INTO contexts VALUES(?,'fixture','bot','[]',?,'fixture','fixture')",
            params![id, expires],
        )
        .unwrap();
    }
    assert_eq!(clean(&store, Kind::Contexts).deleted["contexts"], 1);
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM contexts WHERE expires_at=0",
            [],
            |r| r.get::<_, u64>(0)
        )
        .unwrap(),
        1
    );
    db.execute(
        "INSERT INTO web_tokens VALUES('used','101','bot','fixture',?,?)",
        params![crate::now() + 1000.0, crate::now()],
    )
    .unwrap();
    db.execute(
        "INSERT INTO web_tokens VALUES('expired','101','bot','fixture',1,NULL)",
        [],
    )
    .unwrap();
    db.execute(
        "INSERT INTO web_sessions VALUES('expired','101','bot','fixture',1)",
        [],
    )
    .unwrap();
    assert_eq!(clean(&store, Kind::WebTokens).deleted["web_tokens"], 2);
    assert_eq!(clean(&store, Kind::WebSessions).deleted["web_sessions"], 1);
}

#[test]
fn request_retention_requires_final_handoff_and_bounds_associated_deletes() {
    let temp = tempfile::tempdir().unwrap();
    let config = Config::new(temp.path().to_owned());
    let mut db = crate::queue::connect(&config).unwrap();
    for (id, state, execution, completion, input) in [
        ("old", "completed", "ok", "ok", None),
        ("uncertain", "completed", "uncertain", "ok", None),
        ("aggregate-uncertain", "completed", "ok", "ok", None),
        ("retry", "completed", "failed", "failed", Some("{}")),
        ("pending", "queued", "ok", "ok", None),
        ("large", "completed", "ok", "ok", None),
    ] {
        db.execute("INSERT INTO requests(id,job_id,module,topic,model,state,created_at,updated_at,execution_status,completion_status,completion_input) VALUES(?,?,'fixture','fixture',0,?,1,2,?,?,?)",params![id,id,state,execution,completion,input]).unwrap();
        db.execute("INSERT INTO triggers VALUES(?,?)", params![id, id])
            .unwrap();
    }
    db.execute("UPDATE requests SET detail='task reported failure or uncertainty' WHERE id='aggregate-uncertain'", []).unwrap();
    let tx = db.transaction().unwrap();
    for n in 0..5000 {
        tx.execute(
            "INSERT INTO triggers VALUES(?,'large')",
            [format!("large-{n}")],
        )
        .unwrap();
    }
    tx.commit().unwrap();
    let cleaned = batch(
        &mut db,
        Kind::Requests,
        &mut Cursor::default(),
        &Policy::default(),
        crate::now(),
        BATCH,
    )
    .unwrap();
    assert_eq!(cleaned.deleted["requests"], 1);
    assert_eq!(cleaned.deleted["triggers"], 1);
    assert!(cleaned.limited);
    assert_eq!(crate::queue::state(&config, "large").unwrap(), "completed");
    assert_eq!(
        crate::queue::state(&config, "aggregate-uncertain").unwrap(),
        "completed"
    );
}

#[test]
fn bounded_candidate_cursor_advances_past_protected_prefix_and_busy_rolls_back() {
    let (_temp, store) = store();
    let mut db = store.connect().unwrap();
    let tx = db.transaction().unwrap();
    for n in 0..501 {
        tx.execute(
            "INSERT INTO contexts VALUES(?,'fixture','bot','[]',0,'service:fixture','fixture')",
            [format!("live-{n}")],
        )
        .unwrap();
    }
    tx.execute(
        "INSERT INTO contexts VALUES('old','fixture','bot','[]',1,'fixture','fixture')",
        [],
    )
    .unwrap();
    tx.commit().unwrap();
    let mut cursor = Cursor::default();
    let first = batch(
        &mut db,
        Kind::Contexts,
        &mut cursor,
        &Policy::default(),
        crate::now(),
        BATCH,
    )
    .unwrap();
    assert_eq!(first.scanned, 500);
    assert!(first.deleted.is_empty());
    assert!(!first.done);
    let second = batch(
        &mut db,
        Kind::Contexts,
        &mut cursor,
        &Policy::default(),
        crate::now(),
        BATCH,
    )
    .unwrap();
    assert_eq!(second.deleted["contexts"], 1);
    assert!(second.done);
    let mut other = Connection::open(&store.path).unwrap();
    let tx = other
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .unwrap();
    db.busy_timeout(Duration::ZERO).unwrap();
    let error = batch(
        &mut db,
        Kind::Contexts,
        &mut cursor,
        &Policy::default(),
        crate::now(),
        BATCH,
    )
    .err()
    .unwrap();
    assert_eq!(error_code(&error), "database_busy");
    assert_eq!(cursor.row, 0);
    drop(tx);
}

#[tokio::test]
async fn round_deletion_budget_caps_partial_batches_and_keeps_progress() {
    let (temp, store) = store();
    let config = Config::new(temp.path().to_owned());
    let mut db = store.connect().unwrap();
    let tx = db.transaction().unwrap();
    for n in 0..20_503 {
        tx.execute(
            "INSERT INTO contexts VALUES(?,'fixture','bot','[]',?,'fixture','fixture')",
            params![format!("fixture-{n}"), if n % 501 == 0 { 0 } else { 1 }],
        )
        .unwrap();
    }
    tx.commit().unwrap();
    let (_stop, mut receiver) = watch::channel(false);
    let metrics = Metrics::default();
    let mut cursors = BTreeMap::new();
    sweep(
        &config,
        Some(&store),
        &Policy::default(),
        &[Kind::Contexts],
        &mut cursors,
        &metrics,
        &mut receiver,
    )
    .await;
    assert_eq!(metrics.snapshot()["deleted"]["contexts"], 20_000);
    assert!(cursors[&Kind::Contexts].row > 20_000);
    sweep(
        &config,
        Some(&store),
        &Policy::default(),
        &[Kind::Contexts],
        &mut cursors,
        &metrics,
        &mut receiver,
    )
    .await;
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM contexts WHERE expires_at>0",
            [],
            |r| r.get::<_, u64>(0)
        )
        .unwrap(),
        0
    );
    assert_eq!(
        db.query_row("SELECT count(*) FROM contexts", [], |r| r.get::<_, u64>(0))
            .unwrap(),
        41
    );
}

#[tokio::test]
async fn freeze_and_stop_prevent_new_batches() {
    let (temp, store) = store();
    let config = Config::new(temp.path().to_owned());
    std::fs::create_dir_all(config.home.join("run")).unwrap();
    std::fs::write(config.home.join("run/freeze"), b"").unwrap();
    let (tx, mut rx) = watch::channel(false);
    let metrics = Metrics::default();
    let mut cursors = BTreeMap::new();
    sweep(
        &config,
        Some(&store),
        &Policy::default(),
        &[Kind::Events],
        &mut cursors,
        &metrics,
        &mut rx,
    )
    .await;
    assert_eq!(metrics.snapshot()["batches"], 0);
    assert_eq!(metrics.snapshot()["status"], "interrupted");
    std::fs::remove_file(config.home.join("run/freeze")).unwrap();
    tx.send(true).unwrap();
    sweep(
        &config,
        Some(&store),
        &Policy::default(),
        &[Kind::Events],
        &mut cursors,
        &metrics,
        &mut rx,
    )
    .await;
    assert_eq!(metrics.snapshot()["batches"], 0);
}

#[tokio::test]
async fn committed_completions_protect_terminal_rows_until_reconciled() {
    let (_temp, store) = store();
    let payload = json!({"text":"fixture"});
    event(
        &store,
        "committed",
        "delivered",
        "text",
        payload.clone(),
        receipt(),
    );
    let raw = json!({"id":"committed","profile":"bot","payload":payload,"attempts":0});
    crate::completion::stage(
        &store,
        crate::completion::Completion::delivery(&raw, "delivered", Some(receipt()), "", 0.0)
            .unwrap(),
    )
    .await
    .unwrap();
    let mut db = store.connect().unwrap();
    let mut cursor = Cursor::default();
    let error = batch(
        &mut db,
        Kind::Events,
        &mut cursor,
        &Policy::default(),
        crate::now(),
        BATCH,
    )
    .err()
    .unwrap();
    assert_eq!(error_code(&error), "completion_pending");
    assert_eq!(cursor.row, 0);
    assert!(store.has_event("committed").unwrap());
    crate::completion::reconcile(&store).unwrap();
    assert_eq!(crate::completion::pending_count(&store.path).unwrap(), 0);
    assert_eq!(clean(&store, Kind::Events).deleted["events"], 1);

    db.execute("INSERT INTO updates(profile,update_id,payload,status) VALUES('bot',1,'{}','done'),('bot',2,'{}','done')", []).unwrap();
    crate::completion::stage(
        &store,
        crate::completion::Completion::command("bot", 1, "{}", "fixture", Value::Null, "done", &[]),
    )
    .await
    .unwrap();
    let policy = Policy {
        updates_per_bot: 1,
        ..Policy::default()
    };
    assert!(
        batch(
            &mut db,
            Kind::Updates,
            &mut Cursor::default(),
            &policy,
            crate::now(),
            BATCH,
        )
        .err()
        .unwrap()
        .is::<CompletionPending>()
    );
    crate::completion::reconcile(&store).unwrap();
    assert_eq!(
        batch(
            &mut db,
            Kind::Updates,
            &mut Cursor::default(),
            &policy,
            crate::now(),
            BATCH,
        )
        .unwrap()
        .deleted["updates"],
        1
    );
}

#[tokio::test(start_paused = true)]
async fn maintenance_waits_ten_minutes_then_skips_frozen_hourly_rounds() {
    use std::sync::Arc;
    let (temp, store) = store();
    let config = Config::new(temp.path().to_owned());
    std::fs::create_dir_all(config.home.join("config")).unwrap();
    std::fs::write(config.home.join("config/bots.json"), br#"{"bots":{}}"#).unwrap();
    store
        .connect()
        .unwrap()
        .execute(
            "INSERT INTO contexts VALUES('old','fixture','bot','[]',1,'fixture','fixture')",
            [],
        )
        .unwrap();
    crate::queue::connect(&config).unwrap().execute("INSERT INTO requests(id,job_id,module,topic,model,state,created_at,updated_at) VALUES('fixture','fixture','fixture','fixture',0,'completed',1,2)", []).unwrap();
    let lock_path = config
        .home
        .join("run")
        .join(format!("request-{}.lock", crate::digest("fixture")));
    drop(crate::runtime::open_task_lock(&lock_path).unwrap());
    let app = crate::api::App {
        config: config.clone(),
        store: store.clone(),
        snapshot: Arc::new(tokio::sync::RwLock::new(crate::api::Snapshot {
            settings: crate::config::Settings::default(),
            catalog: crate::catalog::Catalog::default(),
            credentials: BTreeMap::new(),
        })),
        reload_guard: Arc::default(),
    };
    let (stop, receiver) = watch::channel(false);
    let gateway_task = tokio::spawn(gateway(app, receiver.clone()));
    let executor_task = tokio::spawn(executor(config.clone(), receiver));
    tokio::task::yield_now().await;
    tokio::time::advance(Duration::from_secs(599)).await;
    tokio::task::yield_now().await;
    assert_eq!(store.runtime.retention.snapshot()["status"], "not_run");
    assert!(executor_storage(&config)["retention"].is_null());
    assert!(lock_path.exists());
    tokio::time::advance(Duration::from_secs(1)).await;
    // Allow real blocking filesystem/SQLite work to finish without advancing
    // the virtual clock through another hourly deadline.
    tokio::time::resume();
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            if store.runtime.retention.snapshot()["status"] == "completed"
                && executor_storage(&config)["locks"]["directory_entries"].is_number()
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(store.runtime.retention.snapshot()["deleted"]["contexts"], 1);
    assert!(!lock_path.exists());
    store
        .connect()
        .unwrap()
        .execute(
            "INSERT INTO contexts VALUES('frozen','fixture','bot','[]',1,'fixture','fixture')",
            [],
        )
        .unwrap();
    std::fs::write(config.home.join("run/freeze"), b"").unwrap();
    let gateway_before = store.runtime.retention.snapshot();
    let executor_before = executor_storage(&config)["retention"].clone();
    tokio::time::pause();
    tokio::time::advance(Duration::from_secs(3600)).await;
    tokio::task::yield_now().await;
    assert_eq!(store.runtime.retention.snapshot(), gateway_before);
    assert_eq!(executor_storage(&config)["retention"], executor_before);
    assert_eq!(
        store
            .connect()
            .unwrap()
            .query_row(
                "SELECT count(*) FROM contexts WHERE hash='frozen'",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        1
    );
    stop.send(true).unwrap();
    gateway_task.await.unwrap();
    executor_task.await.unwrap();
}

#[test]
fn storage_unknown_is_explicit_and_summary_is_stable_between_samples() {
    let temp = tempfile::tempdir().unwrap();
    let config = Config::new(temp.path().to_owned());
    assert!(storage(&config.db(), &config.home.join("run"))["database"]["bytes"].is_null());
    crate::queue::summary_flush(&config).unwrap();
    let path = config.home.join("run/task-summary.json");
    let previous = std::fs::read(&path).unwrap();
    let mtime = std::fs::metadata(&path).unwrap().modified().unwrap();
    crate::queue::summary_flush(&config).unwrap();
    assert_eq!(std::fs::read(&path).unwrap(), previous);
    assert_eq!(std::fs::metadata(&path).unwrap().modified().unwrap(), mtime);
    assert_eq!(
        std::fs::metadata(config.home.join("supervisor/task-state.sqlite3"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    let mut previous = crate::queue::public(&config);
    previous["metrics"]["storage"]["retention"] =
        json!({"status":"completed","deleted":{"requests":7}});
    previous["metrics"]["storage"]["locks"] =
        json!({"directory_entries":19,"directory_sampled_at":123.0});
    crate::config::atomic_json(&path, &previous, 0o644).unwrap();
    crate::queue::summary_flush(&config).unwrap();
    assert_eq!(
        crate::queue::public(&config)["metrics"]["storage"],
        previous["metrics"]["storage"]
    );
    crate::queue::summary_refresh(&config).unwrap();
    assert_eq!(
        crate::queue::public(&config)["metrics"]["storage"]["locks"],
        previous["metrics"]["storage"]["locks"]
    );
}
