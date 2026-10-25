use super::*;
use std::os::unix::fs::{PermissionsExt, symlink};

fn fixture() -> (tempfile::TempDir, Config) {
    let temp = tempfile::tempdir().unwrap();
    let config = Config::new(temp.path().to_owned());
    crate::queue::connect(&config).unwrap();
    (temp, config)
}
fn path(config: &Config, id: &str) -> std::path::PathBuf {
    config
        .home
        .join("run")
        .join(format!("request-{}.lock", crate::digest(id)))
}
fn request(config: &Config, id: &str, state: &str, at: f64) {
    crate::queue::connect(config).unwrap().execute("INSERT INTO requests(id,job_id,module,topic,model,state,created_at,updated_at) VALUES(?,?,'fixture','fixture',0,?,1,?)",rusqlite::params![id,id,state,at]).unwrap();
}

#[test]
fn only_completed_old_or_proven_old_orphan_unlocked_files_are_removed() {
    let (_temp, config) = fixture();
    let now = crate::now();
    for (id, state, at, expected) in [
        ("done", "completed", 1.0, true),
        ("new", "completed", now, false),
        ("running", "running", 1.0, false),
        ("uncertain", "uncertain", 1.0, false),
    ] {
        request(&config, id, state, at);
        let file = runtime::open_task_lock(&path(&config, id)).unwrap();
        drop(file);
        let known = (id.into(), state.into(), at);
        assert_eq!(
            remove(&config, &path(&config, id), Some(&known), now).unwrap(),
            expected
        );
    }
    let orphan = path(&config, "orphan");
    drop(runtime::open_task_lock(&orphan).unwrap());
    assert!(!remove(&config, &orphan, None, now).unwrap());
    assert!(remove(&config, &orphan, None, now + 86401.0).unwrap());
    request(&config, "held", "completed", 1.0);
    let held = path(&config, "held");
    let file = runtime::acquire_task_lock(&held).unwrap().unwrap();
    assert!(
        !remove(
            &config,
            &held,
            Some(&("held".into(), "completed".into(), 1.0)),
            now
        )
        .unwrap()
    );
    drop(file);
    assert!(
        remove(
            &config,
            &held,
            Some(&("held".into(), "completed".into(), 1.0)),
            now
        )
        .unwrap()
    );
}

#[test]
fn rejects_links_permissions_nonempty_and_rechecks_request_state() {
    let (_temp, config) = fixture();
    let now = crate::now() + 86401.0;
    let real = path(&config, "real");
    drop(runtime::open_task_lock(&real).unwrap());
    let link = path(&config, "symlink");
    symlink(&real, &link).unwrap();
    assert!(remove(&config, &link, None, now).is_err());
    let hard = path(&config, "hard");
    fs::hard_link(&real, &hard).unwrap();
    assert!(!remove(&config, &real, None, now).unwrap());
    fs::remove_file(&hard).unwrap();
    fs::set_permissions(&real, fs::Permissions::from_mode(0o666)).unwrap();
    assert!(!remove(&config, &real, None, now).unwrap());
    fs::set_permissions(&real, fs::Permissions::from_mode(0o600)).unwrap();
    fs::write(&real, b"fixture").unwrap();
    assert!(!remove(&config, &real, None, now).unwrap());
    request(&config, "changed", "running", 1.0);
    let changed = path(&config, "changed");
    drop(runtime::open_task_lock(&changed).unwrap());
    assert!(
        !remove(
            &config,
            &changed,
            Some(&("changed".into(), "completed".into(), 1.0)),
            now
        )
        .unwrap()
    );
}

#[test]
fn descriptor_opened_before_unlink_cannot_own_replacement_path() {
    let (_temp, config) = fixture();
    let path = path(&config, "race");
    let stale = runtime::open_task_lock(&path).unwrap();
    assert!(remove(&config, &path, None, crate::now() + 86401.0).unwrap());
    let current = runtime::acquire_task_lock(&path).unwrap().unwrap();
    assert!(runtime::try_lock(&stale).unwrap());
    assert!(!runtime::task_lock_current(&path, &stale).unwrap());
    assert!(runtime::task_lock_current(&path, &current).unwrap());
    assert!(runtime::acquire_task_lock(&path).unwrap().is_none());
}

#[tokio::test]
async fn missing_request_resume_is_noop_and_does_not_recreate_state() {
    let (_temp, config) = fixture();
    assert_eq!(runtime::resume(&config, "removed").await.unwrap(), 0);
    assert!(
        crate::queue::optional_state(&config, "removed")
            .unwrap()
            .is_none()
    );
}

#[test]
fn scan_is_bounded_continues_and_excludes_other_files_and_freeze() {
    let (_temp, config) = fixture();
    for n in 0..1200 {
        drop(runtime::open_task_lock(&path(&config, &format!("request-{n}"))).unwrap());
    }
    fs::write(config.home.join("run/job.lock"), b"").unwrap();
    let (_, rx) = watch::channel(false);
    let mut scan = Scan::default();
    batch(&config, &mut scan, &Requests::new(), &rx).unwrap();
    assert_eq!(scan.entries_seen, 1000);
    assert!(!scan.finished);
    let checked = scan.checked;
    batch(&config, &mut scan, &Requests::new(), &rx).unwrap();
    assert!(scan.finished);
    assert!(scan.checked > checked);
    assert_eq!(scan.directory_entries, Some(1201));
    assert_eq!(scan.checked, 1200);
    assert_eq!(scan.deleted, 0);
    fs::write(config.home.join("run/freeze"), b"").unwrap();
    let mut frozen = Scan::default();
    batch(&config, &mut frozen, &Requests::new(), &rx).unwrap();
    assert_eq!(frozen.checked, 0);
    assert!(candidate(&format!("request-{}.lock", "a".repeat(64))).is_some());
    for name in [
        "software-update-review.lock",
        "job.lock",
        "daemon.lock",
        "quota-personal-process-0.lock",
        "dispatch-deadbeef.lock",
        "request-XYZ.lock",
        "request-000000000000000000000000000000000000000000000000000000000000000g.lock",
    ] {
        assert!(candidate(name).is_none(), "{name}");
    }
}

#[test]
fn old_job_process_and_quota_locks_are_never_reclaimed() {
    let (_temp, config) = fixture();
    let now = crate::now() + 86401.0;
    let run = config.home.join("run");
    for name in [
        "software-update-review.lock",
        "93e1c037c069.lock",
        "kdocs-quotes.lock",
    ] {
        drop(runtime::open_task_lock(&run.join(name)).unwrap());
        assert!(
            !remove(&config, &run.join(name), None, now).unwrap(),
            "{name} must remain for openers without inode checks"
        );
        assert!(run.join(name).exists());
    }
    for name in ["daemon.lock", "quota-trading-model-0.lock"] {
        assert!(candidate(name).is_none(), "{name} must stay unreclaimable");
        drop(runtime::open_task_lock(&run.join(name)).unwrap());
        assert!(!remove(&config, &run.join(name), None, now).unwrap());
        assert!(run.join(name).exists(), "{name} must remain");
    }
}

#[test]
fn completed_with_fresh_updated_at_reclaims_when_lock_file_is_old() {
    let (_temp, config) = fixture();
    let now = crate::now();
    let future = now + 86401.0;
    // Simulate reconcile bumping updated_at to "now" while the lock inode is aged.
    request(&config, "reconciled", "completed", future);
    let file = path(&config, "reconciled");
    drop(runtime::open_task_lock(&file).unwrap());
    let known = ("reconciled".into(), "completed".into(), future);
    assert!(
        remove(&config, &file, Some(&known), future).unwrap(),
        "aged lock for completed request must reclaim despite fresh updated_at"
    );
    assert!(!file.exists());
}

#[test]
fn nonempty_request_lock_is_soft_skipped_without_scan_error() {
    let (_temp, config) = fixture();
    fs::write(path(&config, "nonempty"), b"holder\n").unwrap();
    fs::set_permissions(path(&config, "nonempty"), fs::Permissions::from_mode(0o660)).unwrap();
    let (_, rx) = watch::channel(false);
    let mut scan = Scan::default();
    batch(&config, &mut scan, &Requests::new(), &rx).unwrap();
    assert!(scan.finished);
    assert_eq!(scan.checked, 1);
    assert_eq!(scan.deleted, 0);
    assert_eq!(scan.skipped, 1);
    assert!(scan.scan_error.is_none(), "{:?}", scan.scan_error);
    assert!(path(&config, "nonempty").exists());
}
