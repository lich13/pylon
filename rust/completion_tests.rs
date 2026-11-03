use super::*;
use std::os::unix::fs::{PermissionsExt, symlink};

fn new_store() -> (tempfile::TempDir, Store) {
    let temp = tempfile::tempdir().unwrap();
    let store = Store::new(&temp.path().join("state.sqlite3")).unwrap();
    (temp, store)
}

fn claim_delivery(store: &Store, id: &str) -> Value {
    store
        .submit(
            "fixture-module",
            &json!({"id":id,"kind":"text","payload":{"text":"fixture"}}),
            "fixture-bot",
        )
        .unwrap();
    store.claim("fixture-bot").unwrap().unwrap()
}

fn delivery_record(event: &Value, status: &str, receipt: Value) -> Completion {
    Completion::delivery(event, status, Some(receipt), "fixture result", 0.0).unwrap()
}

#[tokio::test]
async fn busy_database_retries_and_commits_once_released() {
    let (_temp, store) = new_store();
    let event = claim_delivery(&store, "busy-persist");
    let (locked, held) = std::sync::mpsc::channel();
    let path = store.path.clone();
    let release = std::thread::spawn(move || {
        let mut db = rusqlite::Connection::open(path).unwrap();
        let tx = db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .unwrap();
        locked.send(()).unwrap();
        std::thread::sleep(Duration::from_millis(300));
        tx.commit().unwrap();
    });
    held.recv().unwrap();
    let (_stop_sender, mut stop) = watch::channel(false);
    let persisted = persist_for(
        &store,
        delivery_record(&event, "delivered", json!([])),
        &mut stop,
        Duration::from_secs(20),
    )
    .await
    .unwrap();
    release.join().unwrap();
    assert!(persisted);
    assert_eq!(store.event("busy-persist").unwrap()["status"], "delivered");
}

#[tokio::test]
async fn pending_and_command_completion_commit_before_delivery_wakes() {
    let (_temp, store) = new_store();
    let event = claim_delivery(&store, "retry");
    let (_wake, mut changed) = store
        .runtime
        .delivery_listener("fixture-bot", crate::optimization::Lane::Telegram);
    assert!(!changed.has_changed().unwrap());
    apply(&store, &delivery_record(&event, "pending", json!([]))).unwrap();
    assert!(changed.has_changed().unwrap());
    assert_eq!(store.event("retry").unwrap()["status"], "pending");
    changed.borrow_and_update();
    store.connect().unwrap().execute("INSERT INTO updates(profile,update_id,payload,status) VALUES('fixture-bot',51,'{}','running')", []).unwrap();
    let record = Completion::command(
        "fixture-bot",
        51,
        "{}",
        "fixture-module",
        json!({"text":"reply"}),
        "done",
        &[],
    );
    apply(&store, &record).unwrap();
    assert!(changed.has_changed().unwrap());
    assert_eq!(
        store
            .connect()
            .unwrap()
            .query_row("SELECT status FROM updates WHERE update_id=51", [], |row| {
                row.get::<_, String>(0)
            })
            .unwrap(),
        "done"
    );
}

#[tokio::test]
async fn partial_delivery_receipt_recovers_to_uncertain_without_requeue() {
    let (_temp, store) = new_store();
    let event = claim_delivery(&store, "partial-delivery");
    let receipt = json!([{"message_id":31,"chat_id":"fixture-chat"}]);
    apply(&store, &delivery_record(&event, "sending", receipt.clone())).unwrap();
    let current = store.event("partial-delivery").unwrap();
    assert_eq!(current["status"], "sending");
    assert_eq!(current["receipt"], receipt);
    assert!(store.claim("fixture-bot").unwrap().is_none());

    // Worker retirement keeps the partial receipt and never queues the
    // uncertain remote operation for automatic replay.
    interrupt_bot(&store, "fixture-bot").unwrap();
    let current = store.event("partial-delivery").unwrap();
    assert_eq!(current["status"], "uncertain");
    assert_eq!(current["receipt"], receipt);
    assert_eq!(current["attempts"], 1);
    assert!(store.claim("fixture-bot").unwrap().is_none());
}

#[tokio::test]
async fn persist_deadline_leaves_claimed_row_for_uncertain_recovery() {
    let (_temp, store) = new_store();
    let event = claim_delivery(&store, "deadline-partial");
    let record = delivery_record(&event, "delivered", json!([{"message_id":32}]));
    let mut db = store.connect().unwrap();
    let tx = db
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .unwrap();
    let (_stop_sender, mut stop) = watch::channel(false);
    let started = std::time::Instant::now();
    let result = tokio::time::timeout(
        Duration::from_secs(1),
        persist_for(&store, record, &mut stop, Duration::from_millis(150)),
    )
    .await
    .expect("short persistence budget should not wait for SQLite's busy timeout")
    .unwrap();
    assert!(!result);
    assert!(started.elapsed() < Duration::from_secs(1));
    drop(tx);
    drop(db);
    assert!(!store.path.parent().unwrap().join("completions").exists());
    // The abandoned blocking attempt may still commit the true result after
    // the lock is released; otherwise recovery marks the claim uncertain.
    store.recover().unwrap();
    let current = store.event("deadline-partial").unwrap();
    assert!(
        matches!(current["status"].as_str(), Some("uncertain" | "delivered")),
        "{current}"
    );
    assert_eq!(current["attempts"], 1);
    assert!(store.claim("fixture-bot").unwrap().is_none());
}

#[tokio::test]
async fn stopped_persist_makes_one_bounded_final_attempt() {
    let (_temp, store) = new_store();
    let event = claim_delivery(&store, "stopped-persist");
    let receipt = json!([{"message_id":33,"chat_id":"fixture-chat"}]);
    let (_stop_sender, mut stop) = watch::channel(true);
    let mut db = store.connect().unwrap();
    let tx = db
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .unwrap();
    let started = std::time::Instant::now();
    let record = delivery_record(&event, "delivered", receipt.clone());
    let result = tokio::time::timeout(Duration::from_secs(1), persist(&store, record, &mut stop))
        .await
        .expect("stopped persistence should make only a bounded final attempt")
        .unwrap();
    assert!(!result);
    assert!(started.elapsed() < Duration::from_millis(750));
    drop(tx);
    drop(db);
    let record = delivery_record(&event, "delivered", receipt.clone());
    assert!(persist(&store, record, &mut stop).await.unwrap());
    let current = store.event("stopped-persist").unwrap();
    assert_eq!(current["status"], "delivered");
    assert_eq!(current["receipt"], receipt);
}

#[test]
fn complete_delivery_receipt_can_be_supplemented_repeatedly() {
    let (_temp, store) = new_store();
    let event = claim_delivery(&store, "complete-delivery");
    let partial = json!([{"message_id":41,"chat_id":"fixture-chat"}]);
    apply(&store, &delivery_record(&event, "sending", partial)).unwrap();
    let complete = json!([
        {"message_id":41,"chat_id":"fixture-chat"},
        {"message_id":42,"chat_id":"fixture-chat"}
    ]);
    apply(
        &store,
        &delivery_record(&event, "delivered", complete.clone()),
    )
    .unwrap();
    // A duplicate result must not duplicate the event or regress its receipt.
    apply(
        &store,
        &delivery_record(&event, "delivered", complete.clone()),
    )
    .unwrap();
    let current = store.event("complete-delivery").unwrap();
    assert_eq!(current["status"], "delivered");
    assert_eq!(current["receipt"], complete);
    assert_eq!(current["attempts"], 1);
    let count: u64 = store
        .connect()
        .unwrap()
        .query_row(
            "SELECT count(*) FROM events WHERE id='complete-delivery'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);
}

#[test]
fn stale_delivery_completion_rejects_changed_payload_or_profile() {
    for (id, change) in [
        ("changed-payload", "payload"),
        ("changed-profile", "profile"),
    ] {
        let (_temp, store) = new_store();
        let event = claim_delivery(&store, id);
        let record = delivery_record(
            &event,
            "delivered",
            json!([{"message_id":51,"chat_id":"fixture-chat"}]),
        );
        let db = store.connect().unwrap();
        match change {
            "payload" => {
                db.execute(
                    "UPDATE events SET payload=? WHERE id=?",
                    rusqlite::params![json!({"text":"changed fixture"}).to_string(), id],
                )
                .unwrap();
            }
            "profile" => {
                db.execute(
                    "UPDATE events SET profile='other-fixture-bot' WHERE id=?",
                    [id],
                )
                .unwrap();
            }
            _ => unreachable!(),
        }
        drop(db);
        let error = apply(&store, &record).unwrap_err();
        assert!(
            format!("{error:#}").contains("completion event changed"),
            "unexpected {change} mismatch error: {error:#}"
        );
        assert_eq!(store.event(id).unwrap()["status"], "sending");
    }
}

#[test]
fn stale_delivery_completion_is_skipped_after_a_newer_attempt_starts() {
    let (_temp, store) = new_store();
    let event = claim_delivery(&store, "newer-attempt");
    let completion = delivery_record(
        &event,
        "delivered",
        json!([{"message_id":61,"chat_id":"fixture-chat"}]),
    );
    store
        .connect()
        .unwrap()
        .execute(
            "UPDATE events SET attempts=2,status='sending' WHERE id='newer-attempt'",
            [],
        )
        .unwrap();
    apply(&store, &completion).unwrap();
    let current = store.event("newer-attempt").unwrap();
    assert_eq!(current["attempts"], 2);
    assert_eq!(current["status"], "sending");
    assert!(current["receipt"].is_null());
}

#[test]
fn legacy_mailbox_count_never_creates_or_trusts_unsafe_entries() {
    let (_temp, store) = new_store();
    let root = store.path.parent().unwrap().join("completions");
    assert_eq!(pending_count(&store.path).unwrap(), 0);
    assert!(!root.exists());
    std::fs::create_dir(&root).unwrap();
    std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).unwrap();
    assert_eq!(pending_count(&store.path).unwrap(), 0);
    std::fs::write(root.join("legacy.json"), b"not parsed").unwrap();
    std::fs::set_permissions(
        root.join("legacy.json"),
        std::fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    assert_eq!(pending_count(&store.path).unwrap(), 1);
    std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o755)).unwrap();
    assert!(pending_count(&store.path).is_err());

    let (_temp, store) = new_store();
    let root = store.path.parent().unwrap().join("completions");
    let target = store.path.parent().unwrap().join("completion-target");
    std::fs::create_dir(&target).unwrap();
    symlink(&target, &root).unwrap();
    assert!(pending_count(&store.path).is_err());
}

#[test]
fn command_completion_commits_reply_and_context_removal_idempotently() {
    let (_temp, store) = new_store();
    let bot = "fixture-bot";
    let update = 81;
    let payload = json!({"message":{"text":"/fixture"}}).to_string();
    let context = "fixture-context-token";
    let context_hash = crate::digest(context);
    let reply = json!({"text":"fixture reply"});
    let db = store.connect().unwrap();
    db.execute(
        "INSERT INTO updates(profile,update_id,payload,status,kind) VALUES(?,?,?,'running','message')",
        rusqlite::params![bot, update, payload],
    )
    .unwrap();
    db.execute(
        "INSERT INTO contexts(hash,module,bot,capabilities,expires_at,origin,workspace) VALUES(?,?,?,'[]',0,'fixture','fixture')",
        rusqlite::params![context_hash, "fixture-module", bot],
    )
    .unwrap();
    drop(db);

    let contexts = vec![context.to_owned()];
    let completion = Completion::command(
        bot,
        update,
        &payload,
        "fixture-module",
        reply.clone(),
        "done",
        &contexts,
    );
    apply(&store, &completion).unwrap();

    let db = store.connect().unwrap();
    let status: String = db
        .query_row(
            "SELECT status FROM updates WHERE profile=? AND update_id=?",
            rusqlite::params![bot, update],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(status, "done");
    let remaining_contexts: u64 = db
        .query_row(
            "SELECT count(*) FROM contexts WHERE hash=?",
            [&context_hash],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(remaining_contexts, 0);
    let reply_key = crate::digest(format!("{bot}:text:reply:{update}"));
    let reply_id = &reply_key[..32];
    let (reply_count, reply_payload): (u64, String) = db
        .query_row(
            "SELECT count(*),max(payload) FROM events WHERE id=?",
            [reply_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(reply_count, 1);
    assert_eq!(
        serde_json::from_str::<Value>(&reply_payload).unwrap(),
        reply
    );
    drop(db);

    let duplicate = Completion::command(
        bot,
        update,
        &payload,
        "fixture-module",
        reply.clone(),
        "done",
        &contexts,
    );
    apply(&store, &duplicate).unwrap();
    let duplicate_count: u64 = store
        .connect()
        .unwrap()
        .query_row(
            "SELECT count(*) FROM events WHERE id=?",
            [reply_id],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(duplicate_count, 1);
}
