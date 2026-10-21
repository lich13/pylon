use super::*;
use crate::db::{Claim, Store};

fn store() -> (tempfile::TempDir, Store) {
    let temp = tempfile::tempdir().unwrap();
    let store = Store::new(&temp.path().join("state.sqlite3")).unwrap();
    (temp, store)
}

#[test]
fn date_keys_cover_utc8_midnight_month_year_and_leap_day() {
    for (utc, expected) in [
        ("2026-10-21T15:59:59Z", "2026-10-21"),
        ("2026-10-21T16:00:00Z", "2026-10-22"),
        ("2026-01-31T16:00:00Z", "2026-02-01"),
        ("2025-12-31T16:00:00Z", "2026-01-01"),
        ("2024-02-28T16:00:00Z", "2024-02-29"),
        ("2024-02-29T15:59:59Z", "2024-02-29"),
        ("2024-02-29T16:00:00Z", "2024-03-01"),
        ("2000-02-28T16:00:00Z", "2000-02-29"),
        ("2100-02-28T16:00:00Z", "2100-03-01"),
    ] {
        let time = chrono::DateTime::parse_from_rfc3339(utc).unwrap();
        assert_eq!(crate::db::utc8_day(time.into()), expected);
    }
}

#[tokio::test(start_paused = true)]
async fn notification_generation_survives_the_gap_before_wait_and_is_scoped() {
    let runtime = Runtime::default();
    let (wake, mut telegram) = runtime.delivery_listener("one", false);
    let (_bark_wake, bark) = runtime.delivery_listener("one", true);
    let (_other_wake, other) = runtime.delivery_listener("two", false);
    let isolated = Runtime::default();
    let (_isolated_wake, isolated_rx) = isolated.delivery_listener("one", false);
    telegram.borrow_and_update();
    runtime.delivery_ready("one", false);
    // This notification happened before changed() was polled.
    tokio::time::timeout(Duration::from_millis(1), telegram.changed())
        .await
        .unwrap()
        .unwrap();
    assert!(!bark.has_changed().unwrap());
    assert!(!other.has_changed().unwrap());
    assert!(!isolated_rx.has_changed().unwrap());
    drop(wake);
    assert!(telegram.changed().await.is_err());
    let (_replacement_wake, replacement) = runtime.delivery_listener("one", false);
    assert!(!replacement.has_changed().unwrap());
}

#[test]
fn all_committed_event_sources_wake_their_delivery_lane() {
    let (_temp, store) = store();
    let (_wake, mut telegram) = store.runtime.delivery_listener("fixture", false);
    let (_bark_wake, mut bark) = store.runtime.delivery_listener("fixture", true);
    let event = json!({"id":"event","kind":"text","payload":{"text":"fixture"}});
    store.clone().submit("fixture", &event, "fixture").unwrap();
    assert!(telegram.has_changed().unwrap());
    telegram.borrow_and_update();
    assert!(!bark.has_changed().unwrap());
    store.submit("fixture", &event, "fixture").unwrap();
    assert!(
        !telegram.has_changed().unwrap(),
        "idempotent submission must stay quiet"
    );
    let mut invalid = event.clone();
    invalid["payload"] = json!({"text":"different"});
    assert!(store.submit("fixture", &invalid, "fixture").is_err());
    assert!(!telegram.has_changed().unwrap());
    store
        .submit(
            "fixture",
            &json!({"id":"bark","kind":"bark","payload":{}}),
            "fixture",
        )
        .unwrap();
    assert!(bark.has_changed().unwrap());
    bark.borrow_and_update();
    store
        .finish("bark", "pending", None, "fixture", 30.0)
        .unwrap();
    assert!(bark.has_changed().unwrap());
    store
        .complete_update(
            "fixture",
            42,
            "fixture",
            &json!({"text":"reply"}),
            "done",
            &[],
        )
        .unwrap();
    assert!(telegram.has_changed().unwrap());
    telegram.borrow_and_update();
    store
        .configuration_rejected("fixture", Some("fixture"))
        .unwrap();
    assert!(telegram.has_changed().unwrap());
    telegram.borrow_and_update();
    store.connect().unwrap().execute("INSERT INTO updates(profile,update_id,payload,status) VALUES('fixture',43,'{}','running')", []).unwrap();
    store.recover().unwrap();
    assert!(telegram.has_changed().unwrap());
    telegram.borrow_and_update();
    store
        .finish("event", "uncertain", None, "fixture", 0.0)
        .unwrap();
    assert!(!telegram.has_changed().unwrap());
    store.uncertain_digest_day("fixture", "2026-10-21").unwrap();
    assert!(telegram.has_changed().unwrap());
    telegram.borrow_and_update();
    store.uncertain_digest_day("fixture", "2026-10-21").unwrap();
    assert!(!telegram.has_changed().unwrap());
}

#[test]
fn empty_claim_has_a_lane_specific_deadline_and_never_claims_early() {
    let (_temp, store) = store();
    let due = crate::now() + 30.0;
    for (id, kind, next) in [
        ("later", "text", due + 5.0),
        ("first", "text", due),
        ("bark", "bark", due - 5.0),
    ] {
        store
            .submit(
                "fixture",
                &json!({"id":id,"kind":kind,"payload":{},"next_attempt":next}),
                "fixture",
            )
            .unwrap();
    }
    assert!(
        matches!(store.claim_ready("fixture", Some(false)).unwrap(), Claim::Empty(Some(at)) if at == due)
    );
    assert!(
        matches!(store.claim_ready("fixture", Some(true)).unwrap(), Claim::Empty(Some(at)) if at == due - 5.0)
    );
    assert!(matches!(
        store.claim_ready("other", Some(false)).unwrap(),
        Claim::Empty(None)
    ));
    assert_eq!(store.event("first").unwrap()["attempts"], 0);
    assert_eq!(store.runtime.metrics.snapshot()["delivery_empty_claims"], 3);
    store
        .finish("first", "pending", None, "fixture", 0.0)
        .unwrap();
    assert!(
        matches!(store.claim_ready("fixture", Some(false)).unwrap(), Claim::Event(event) if event["id"] == "first")
    );
}
