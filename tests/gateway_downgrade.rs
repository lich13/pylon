use pylon::{config::Config, downgrade};
use rusqlite::Connection;
use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf};

fn state_snapshot(path: &PathBuf) -> (Vec<u8>, Option<Vec<u8>>, Option<Vec<u8>>) {
    let sidecar = |suffix: &str| fs::read(format!("{}{}", path.display(), suffix)).ok();
    (fs::read(path).unwrap(), sidecar("-wal"), sidecar("-shm"))
}

fn home_with_state(user_version: i64, buffered: bool) -> (tempfile::TempDir, Config, PathBuf) {
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
    database
        .execute_batch(&format!("PRAGMA user_version={user_version}"))
        .unwrap();
    database.close().unwrap();
    fs::set_permissions(&database_path, fs::Permissions::from_mode(0o600)).unwrap();

    let completion_dir = gateway.join("completions");
    fs::create_dir(&completion_dir).unwrap();
    fs::set_permissions(&completion_dir, fs::Permissions::from_mode(0o700)).unwrap();
    if buffered {
        let record = completion_dir.join("fixture.json");
        fs::write(&record, br#"{"fixture":"completion"}"#).unwrap();
        fs::set_permissions(record, fs::Permissions::from_mode(0o600)).unwrap();
    }
    (temporary, Config::new(home), database_path)
}

#[test]
fn current_state_is_readable_by_every_supported_target_without_writes() {
    for version in [0, 1] {
        let (_temporary, config, database_path) = home_with_state(version, false);
        let before = state_snapshot(&database_path);
        for target in [
            "0.5.11", "0.5.12", "0.5.17", "0.5.19", "0.5.25", "0.5.28", "0.5.29",
        ] {
            let report = downgrade::check(&config, target);
            assert!(report.ok, "{target}: {report:?}");
            assert_eq!(report.target_version, target);
            assert_eq!(report.completions_pending, Some(0));
            assert_eq!(report.reason, None);
        }
        assert_eq!(state_snapshot(&database_path), before);
    }
}

#[test]
fn newer_state_version_requires_a_newer_gateway() {
    let (_temporary, config, database_path) = home_with_state(2, false);
    let before = state_snapshot(&database_path);
    let report = downgrade::check(&config, "0.5.29");
    assert!(!report.ok);
    assert_eq!(report.reason, Some("state_requires_newer_gateway"));
    assert_eq!(state_snapshot(&database_path), before);
}

#[test]
fn one_legacy_buffered_file_blocks_downgrade() {
    let (_temporary, config, _database_path) = home_with_state(1, true);
    let report = downgrade::check(&config, "0.5.29");
    assert!(!report.ok);
    assert_eq!(report.completions_pending, Some(1));
    assert_eq!(report.reason, Some("pending_completions"));
}

#[test]
fn withdrawn_old_and_malformed_targets_are_unsupported() {
    let (_temporary, config, _database_path) = home_with_state(1, false);
    for target in [
        "0.5.3", "0.5.10", "0.5.18", "0.5.30", "0.5.011", "v0.5.29", "0.5.29.0", "0.6.0", "",
    ] {
        let report = downgrade::check(&config, target);
        assert!(!report.ok, "{target}");
        assert_eq!(report.completions_pending, None);
        assert_eq!(report.reason, Some("unsupported_target"));
    }
}
