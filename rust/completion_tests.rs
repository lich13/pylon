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
async fn partial_delivery_receipt_recovers_to_uncertain_without_requeue() {
    let (_temp, store) = new_store();
    let event = claim_delivery(&store, "partial-delivery");
    let receipt = json!([{"message_id":31,"chat_id":"fixture-chat"}]);
    let record = delivery_record(&event, "sending", receipt.clone());
    let (_stop_sender, mut stop) = watch::channel(false);
    let hold = Hold::delivery(&store, &event, &mut stop).await.unwrap();
    let path = stage(&store, record).await.unwrap();

    // A blocking writer can finish after its async waiter times out. Applying
    // this partial result must keep the only checkpoint for later recovery.
    apply(&store, &path, false).unwrap();
    assert!(path.exists());
    let current = store.event("partial-delivery").unwrap();
    assert_eq!(current["status"], "sending");
    assert_eq!(current["receipt"], receipt);

    // A live worker still owns this delivery, so restart reconciliation skips it.
    reconcile(&store).unwrap();
    assert_eq!(
        store.event("partial-delivery").unwrap()["status"],
        "sending"
    );
    assert!(store.claim("fixture-bot").unwrap().is_none());
    assert!(path.exists());

    // Once ownership ends, recovery preserves the partial receipt and never queues
    // the uncertain remote operation for automatic replay.
    drop(hold);
    reconcile(&store).unwrap();
    let current = store.event("partial-delivery").unwrap();
    assert_eq!(current["status"], "uncertain");
    assert_eq!(current["receipt"], receipt);
    assert_eq!(current["attempts"], 1);
    assert!(store.claim("fixture-bot").unwrap().is_none());
    assert!(!path.exists());
}

#[tokio::test]
async fn persist_deadline_keeps_partial_checkpoint_for_reconciliation() {
    let (_temp, store) = new_store();
    let event = claim_delivery(&store, "deadline-partial");
    let receipt = json!([{"message_id":32,"chat_id":"fixture-chat"}]);
    let record = delivery_record(&event, "sending", receipt.clone());
    let path = directory(&store).unwrap().join(record.name());

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
    assert!(
        path.exists(),
        "timed-out partial completion lost its checkpoint"
    );

    drop(tx);
    drop(db);
    reconcile(&store).unwrap();
    let current = store.event("deadline-partial").unwrap();
    assert_eq!(current["status"], "uncertain");
    assert_eq!(current["receipt"], receipt);
    assert_eq!(current["attempts"], 1);
    assert!(store.claim("fixture-bot").unwrap().is_none());
    assert!(!path.exists());
    reconcile(&store).unwrap();
    assert_eq!(store.event("deadline-partial").unwrap()["receipt"], receipt);
}

#[tokio::test]
async fn stopped_persist_makes_one_bounded_final_attempt() {
    let (_temp, store) = new_store();
    let event = claim_delivery(&store, "stopped-persist");
    let receipt = json!([{"message_id":33,"chat_id":"fixture-chat"}]);
    let record = delivery_record(&event, "delivered", receipt.clone());
    let path = directory(&store).unwrap().join(record.name());

    let mut db = store.connect().unwrap();
    let tx = db
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .unwrap();
    let (_stop_sender, mut stop) = watch::channel(true);
    let started = std::time::Instant::now();
    let result = tokio::time::timeout(Duration::from_secs(1), persist(&store, record, &mut stop))
        .await
        .expect("stopped persistence should make only a bounded final attempt")
        .unwrap();
    assert!(!result);
    assert!(started.elapsed() < Duration::from_millis(750));
    assert!(
        path.exists(),
        "failed final attempt must leave a checkpoint"
    );

    drop(tx);
    drop(db);
    reconcile(&store).unwrap();
    let current = store.event("stopped-persist").unwrap();
    assert_eq!(current["status"], "delivered");
    assert_eq!(current["receipt"], receipt);
    assert_eq!(current["attempts"], 1);
    assert!(!path.exists());
}

#[test]
fn complete_delivery_receipt_can_be_supplemented_repeatedly() {
    let (_temp, store) = new_store();
    let event = claim_delivery(&store, "complete-delivery");
    let partial = json!([{"message_id":41,"chat_id":"fixture-chat"}]);
    let partial_path = save(&store, &delivery_record(&event, "sending", partial.clone())).unwrap();
    apply(&store, &partial_path, false).unwrap();

    let complete = json!([
        {"message_id":41,"chat_id":"fixture-chat"},
        {"message_id":42,"chat_id":"fixture-chat"}
    ]);
    let completion = delivery_record(&event, "delivered", complete.clone());
    let path = save(&store, &completion).unwrap();
    apply(&store, &path, false).unwrap();
    assert!(!path.exists());

    // A later duplicate checkpoint has the same logical identity and must not
    // duplicate the event or regress its final receipt.
    let duplicate = delivery_record(&event, "delivered", complete.clone());
    let duplicate_path = save(&store, &duplicate).unwrap();
    apply(&store, &duplicate_path, false).unwrap();
    let current = store.event("complete-delivery").unwrap();
    assert_eq!(current["status"], "delivered");
    assert_eq!(current["receipt"], complete);
    assert_eq!(current["attempts"], 1);
    assert!(!duplicate_path.exists());
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
        let path = save(&store, &record).unwrap();
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

        let error = apply(&store, &path, false).unwrap_err();
        assert!(
            format!("{error:#}").contains("completion event changed"),
            "unexpected {change} mismatch error: {error:#}"
        );
        assert!(path.exists());
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
    let path = save(&store, &completion).unwrap();
    store
        .connect()
        .unwrap()
        .execute(
            "UPDATE events SET attempts=2,status='sending' WHERE id='newer-attempt'",
            [],
        )
        .unwrap();

    apply(&store, &path, false).unwrap();
    let current = store.event("newer-attempt").unwrap();
    assert_eq!(current["attempts"], 2);
    assert_eq!(current["status"], "sending");
    assert!(current["receipt"].is_null());
    assert!(!path.exists());
}

#[test]
fn completion_directory_rejects_symlink_and_unsafe_permissions() {
    let (_temp, store) = new_store();
    let root = store.path.parent().unwrap().join("completions");
    let target = store.path.parent().unwrap().join("completion-target");
    std::fs::create_dir(&target).unwrap();
    symlink(&target, &root).unwrap();
    let error = directory(&store).unwrap_err();
    assert!(format!("{error:#}").contains("unsafe completion directory"));

    let (_temp, store) = new_store();
    let root = store.path.parent().unwrap().join("completions");
    std::fs::create_dir(&root).unwrap();
    std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o755)).unwrap();
    let error = directory(&store).unwrap_err();
    assert!(format!("{error:#}").contains("unsafe completion directory"));
}

#[test]
fn completion_file_rejects_symlink_and_unsafe_permissions() {
    let (_temp, store) = new_store();
    let record = Completion::command(
        "fixture-bot",
        71,
        "fixture payload",
        "fixture-module",
        json!({"text":"fixture reply"}),
        "done",
        &[],
    );
    let root = directory(&store).unwrap();
    let path = root.join(record.name());
    let target = root.join("outside-fixture");
    std::fs::write(&target, b"fixture only").unwrap();
    symlink(&target, &path).unwrap();
    assert!(save(&store, &record).is_err());

    let (_temp, store) = new_store();
    let record = Completion::command(
        "fixture-bot",
        72,
        "fixture payload",
        "fixture-module",
        json!({"text":"fixture reply"}),
        "done",
        &[],
    );
    let root = directory(&store).unwrap();
    let path = root.join(record.name());
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .unwrap();
    file.write_all(&serde_json::to_vec(&record).unwrap())
        .unwrap();
    drop(file);
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
    let error = save(&store, &record).unwrap_err();
    assert!(format!("{error:#}").contains("unsafe completion file"));
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
    let path = save(&store, &completion).unwrap();
    apply(&store, &path, false).unwrap();

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
    assert!(!path.exists());

    let duplicate = Completion::command(
        bot,
        update,
        &payload,
        "fixture-module",
        reply.clone(),
        "done",
        &contexts,
    );
    let duplicate_path = save(&store, &duplicate).unwrap();
    apply(&store, &duplicate_path, false).unwrap();
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
    assert!(!duplicate_path.exists());
}
