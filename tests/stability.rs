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
    assert!(
        !store
            .get("uncertain:watermark")
            .unwrap()
            .as_array()
            .unwrap()
            .contains(&json!("new-later"))
    );
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
