use pylon::{config::Config, downgrade};
use rusqlite::Connection;
use serde_json::json;
use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf};

const COUNTER_KEY: &str = "uncertain:counter:v2";

fn counter() -> String {
    json!({"version": 2, "pending": 0, "last_day": ""}).to_string()
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

    let report = downgrade::check(&config, "0.5.5");

    assert!(report.ok);
    assert_eq!(report.target_version, "0.5.5");
    assert_eq!(report.completions_pending, Some(0));
    assert_eq!(report.reason, None);
}

#[test]
fn one_buffered_file_blocks_downgrade() {
    let (_temporary, config, _database_path) = home_with_state(&[], true);

    let report = downgrade::check(&config, "0.5.5");

    assert!(!report.ok);
    assert_eq!(report.target_version, "0.5.5");
    assert_eq!(report.completions_pending, Some(1));
    assert_eq!(report.reason, Some("pending_completions"));
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
