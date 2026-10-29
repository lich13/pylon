use pylon::{config::Config, downgrade};
use rusqlite::Connection;
use serde_json::json;
use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf};

const COUNTER_KEY: &str = "uncertain:counter:v2";

fn counter() -> String {
    json!({"version": 2, "pending": 0, "last_day": ""}).to_string()
}

fn gone(id: &str, expires_at: f64) -> String {
    json!({
        "version": 1,
        "id": id,
        "module": "fixture-module",
        "profile": "fixture-bot",
        "kind": "text",
        "cancelled": false,
        "status": "delivered",
        "attempts": 1,
        "created_at": 1.0,
        "updated_at": 2.0,
        "next_attempt": 2.0,
        "expires_at": expires_at,
        "payload_sha256": "a".repeat(64),
        "receipt": {"message_id": 101, "chat_id": 1001},
        "error": null,
        "callbacks": ["b".repeat(64)]
    })
    .to_string()
}

fn gone_message(event_id: &str, expires_at: f64) -> String {
    json!({"version": 1, "event_id": event_id, "expires_at": expires_at}).to_string()
}

fn state_snapshot(path: &PathBuf) -> (Vec<u8>, Option<Vec<u8>>, Option<Vec<u8>>) {
    let sidecar = |suffix: &str| fs::read(format!("{}{}", path.display(), suffix)).ok();
    (fs::read(path).unwrap(), sidecar("-wal"), sidecar("-shm"))
}

fn home_with_state(
    entries: &[(&str, String)],
    buffered: bool,
) -> (tempfile::TempDir, Config, PathBuf) {
    let temporary = tempfile::tempdir().unwrap();
    let home = temporary.path().join("home");
    let gateway = home.join("gateway");
    fs::create_dir_all(&gateway).unwrap();
    fs::set_permissions(&home, fs::Permissions::from_mode(0o700)).unwrap();
    fs::set_permissions(&gateway, fs::Permissions::from_mode(0o700)).unwrap();

    let database_path = gateway.join("state.sqlite3");
    let database = Connection::open(&database_path).unwrap();
    database
        .execute_batch(include_str!("../rust/schema.sql"))
        .unwrap();
    for (key, value) in entries {
        database
            .execute(
                "INSERT INTO kv(key,value) VALUES(?,?)",
                (*key, value.as_str()),
            )
            .unwrap();
    }
    database.close().unwrap();
    fs::set_permissions(&database_path, fs::Permissions::from_mode(0o600)).unwrap();

    if buffered {
        let completion_dir = gateway.join("completions");
        fs::create_dir(&completion_dir).unwrap();
        fs::set_permissions(&completion_dir, fs::Permissions::from_mode(0o700)).unwrap();
        let record = completion_dir.join("fixture.json");
        fs::write(record, br#"{"fixture":"completion"}"#).unwrap();
        fs::set_permissions(
            completion_dir.join("fixture.json"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
    } else {
        let completion_dir = gateway.join("completions");
        fs::create_dir(&completion_dir).unwrap();
        fs::set_permissions(&completion_dir, fs::Permissions::from_mode(0o700)).unwrap();
    }

    let config = Config::new(home);
    (temporary, config, database_path)
}

#[test]
fn empty_buffer_is_safe_for_current_target() {
    let (_temporary, config, _database_path) = home_with_state(&[(COUNTER_KEY, counter())], false);

    let report = downgrade::check(&config, "0.5.6");

    assert!(report.ok);
    assert_eq!(report.target_version, "0.5.6");
    assert_eq!(report.completions_pending, Some(0));
    assert_eq!(report.reason, None);
}

#[test]
fn target_057_is_supported_with_the_existing_counter_state() {
    let (_temporary, config, _database_path) = home_with_state(&[(COUNTER_KEY, counter())], false);

    let report = downgrade::check(&config, "0.5.7");

    assert!(report.ok);
    assert_eq!(report.target_version, "0.5.7");
    assert_eq!(report.completions_pending, Some(0));
    assert_eq!(report.reason, None);
}

#[test]
fn one_buffered_file_blocks_downgrade() {
    let (_temporary, config, _database_path) = home_with_state(&[], true);

    let report = downgrade::check(&config, "0.5.6");

    assert!(!report.ok);
    assert_eq!(report.target_version, "0.5.6");
    assert_eq!(report.completions_pending, Some(1));
    assert_eq!(report.reason, Some("pending_completions"));
    for target in [
        "0.5.15", "0.5.16", "0.5.17", "0.5.19", "0.5.20", "0.5.21", "0.5.22",
    ] {
        assert_eq!(
            downgrade::check(&config, target).reason,
            Some("pending_completions")
        );
    }
}

#[test]
fn active_v2_counter_is_compatible_with_054_but_needs_preparation_for_053() {
    let (_temporary, config, _database_path) = home_with_state(&[(COUNTER_KEY, counter())], false);

    let current = downgrade::check(&config, "0.5.4");
    assert!(current.ok);
    assert_eq!(current.completions_pending, Some(0));
    assert_eq!(current.reason, None);

    let legacy = downgrade::check(&config, "0.5.3");
    assert!(!legacy.ok);
    assert_eq!(legacy.completions_pending, Some(0));
    assert_eq!(legacy.reason, Some("legacy_counter_preparation_required"));
}

#[test]
fn active_retained_history_blocks_every_supported_target_without_writing_state() {
    let event_id = "retained-event-01";
    let cases = [
        (
            format!("event-gone:{event_id}"),
            gone(event_id, 4_000_000_000.0),
        ),
        (
            format!("event-gone-msg:fixture-bot:101:{event_id}"),
            gone_message(event_id, 4_000_000_000.0),
        ),
    ];

    for (key, value) in cases {
        let entries = [(key.as_str(), value)];
        let (_temporary, config, database_path) = home_with_state(&entries, false);
        let before = state_snapshot(&database_path);

        for target in [
            "0.5.3", "0.5.4", "0.5.5", "0.5.6", "0.5.7", "0.5.8", "0.5.9",
        ] {
            let report = downgrade::check(&config, target);
            assert!(
                !report.ok,
                "target {target} unexpectedly accepted retained state"
            );
            assert_eq!(report.target_version, target);
            assert_eq!(report.completions_pending, Some(0));
            assert_eq!(report.reason, Some("retained_events_require_newer_gateway"));
        }

        assert_eq!(state_snapshot(&database_path), before);
        for target in [
            "0.5.15", "0.5.16", "0.5.17", "0.5.19", "0.5.20", "0.5.21", "0.5.22",
        ] {
            assert!(downgrade::check(&config, target).ok);
        }
        assert_eq!(state_snapshot(&database_path), before);
    }
}

#[test]
fn malformed_retained_history_is_unavailable_and_read_only() {
    let cases = [
        ("event-gone:retained-event-02", "{fixture-invalid-json"),
        (
            "event-gone-msg:fixture-bot:102:retained-event-02",
            r#"{"version":1,"event_id":"retained-event-02"}"#,
        ),
    ];

    for (key, value) in cases {
        let (_temporary, config, database_path) = home_with_state(&[(key, value.into())], false);
        let before = state_snapshot(&database_path);
        let report = downgrade::check(&config, "0.5.7");

        assert!(!report.ok);
        assert_eq!(report.completions_pending, Some(0));
        assert_eq!(report.reason, Some("state_unavailable"));
        assert_eq!(state_snapshot(&database_path), before);
        for target in [
            "0.5.15", "0.5.16", "0.5.17", "0.5.19", "0.5.20", "0.5.21", "0.5.22",
        ] {
            assert_eq!(
                downgrade::check(&config, target).reason,
                Some("state_unavailable")
            );
        }
        assert_eq!(state_snapshot(&database_path), before);
    }
}

#[test]
fn expired_retained_history_preserves_completion_and_counter_checks() {
    let event_id = "expired-event-01";
    let key = format!("event-gone:{event_id}");
    let entries = [
        (key.as_str(), gone(event_id, 3.0)),
        (COUNTER_KEY, counter()),
    ];

    let (_temporary, config, database_path) = home_with_state(&entries, false);
    let before = state_snapshot(&database_path);
    let current = downgrade::check(&config, "0.5.6");
    assert!(current.ok);
    assert_eq!(current.completions_pending, Some(0));
    assert_eq!(current.reason, None);
    let legacy = downgrade::check(&config, "0.5.3");
    assert!(!legacy.ok);
    assert_eq!(legacy.completions_pending, Some(0));
    assert_eq!(legacy.reason, Some("legacy_counter_preparation_required"));
    assert_eq!(state_snapshot(&database_path), before);

    let (_temporary, config, database_path) = home_with_state(
        &[
            (key.as_str(), gone(event_id, 3.0)),
            (COUNTER_KEY, counter()),
        ],
        true,
    );
    let before = state_snapshot(&database_path);
    let report = downgrade::check(&config, "0.5.7");
    assert!(!report.ok);
    assert_eq!(report.completions_pending, Some(1));
    assert_eq!(report.reason, Some("pending_completions"));
    assert_eq!(state_snapshot(&database_path), before);
}

#[test]
fn recent_targets_accept_valid_retained_history_but_reject_corruption_and_buffers() {
    for target in ["0.5.11", "0.5.12", "0.5.13"] {
        let valid = gone("fixture", pylon::now() + 86400.0);
        let (_temp, config, path) = home_with_state(
            &[(COUNTER_KEY, counter()), ("event-gone:fixture", valid)],
            false,
        );
        let before = state_snapshot(&path);
        assert!(downgrade::check(&config, target).ok);
        assert_eq!(state_snapshot(&path), before);
        let (_temp, config, _) = home_with_state(&[("event-gone:fixture", "{}".into())], false);
        assert!(!downgrade::check(&config, target).ok);
        let (_temp, config, _) = home_with_state(&[(COUNTER_KEY, counter())], true);
        assert_eq!(
            downgrade::check(&config, target).reason,
            Some("pending_completions")
        );
    }
}
