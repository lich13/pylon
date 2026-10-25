//! Read-only drain evidence, including requests outside the public summary window.
use crate::{config::Config, downgrade, runtime};
use anyhow::{Result, ensure};
use serde::Serialize;
use std::os::unix::fs::MetadataExt;

#[derive(Debug, Serialize)]
pub struct DrainStatus {
    pub ready: bool,
    pub frozen: bool,
    pub tasks: u64,
    pub starting: u64,
    pub commands: u64,
    pub deliveries: u64,
    pub completions: u64,
}

pub fn drain_status(config: &Config) -> Result<DrainStatus> {
    ensure!(
        config.offline || unsafe { libc::geteuid() } == 0,
        "drain status requires root"
    );
    let gateway = config.contained("gateway/state.sqlite3")?;
    let tasks = config.contained("supervisor/task-state.sqlite3")?;
    for path in [&gateway, &tasks] {
        let metadata = std::fs::symlink_metadata(path)?;
        ensure!(
            metadata.is_file() && metadata.nlink() == 1,
            "unsafe drain state"
        );
        ensure!(
            unsafe { libc::geteuid() } == 0 || metadata.uid() == unsafe { libc::geteuid() },
            "drain state owner mismatch"
        );
    }
    let (_gateway_lock, db) = downgrade::read_only_state(&gateway)?;
    let (_task_lock, queue) = downgrade::read_only_state(&tasks)?;
    // Keep counts and queued identities in the same read snapshot. Otherwise a
    // preparing request could move to running between those two queries.
    db.execute_batch("PRAGMA query_only=ON; BEGIN;")?;
    queue.execute_batch("PRAGMA query_only=ON; BEGIN;")?;
    let mut status = DrainStatus {
        ready: false,
        frozen: std::fs::symlink_metadata(config.home.join("run/freeze"))
            .is_ok_and(|m| m.is_file()),
        tasks: queue.query_row(
            "SELECT count(*) FROM requests WHERE state IN ('running','completing','reporting')",
            [],
            |r| r.get(0),
        )?,
        starting: 0,
        commands: db.query_row(
            "SELECT count(*) FROM updates WHERE status='running'",
            [],
            |r| r.get(0),
        )?,
        deliveries: db.query_row(
            "SELECT count(*) FROM events WHERE status='sending'",
            [],
            |r| r.get(0),
        )?,
        completions: crate::completion::pending_count(&gateway)?,
    };
    let mut query = queue.prepare("SELECT id FROM requests WHERE state='queued'")?;
    for id in query.query_map([], |r| r.get::<_, String>(0))? {
        let hash = crate::digest(id?);
        for prefix in ["request", "dispatch"] {
            if runtime::existing_task_lock_held(
                &config
                    .home
                    .join("run")
                    .join(format!("{prefix}-{hash}.lock")),
            )? {
                status.starting += 1;
                break;
            }
        }
    }
    status.completions = status
        .completions
        .max(crate::completion::pending_count(&gateway)?);
    status.ready = status.frozen
        && status.tasks
            + status.starting
            + status.commands
            + status.deliveries
            + status.completions
            == 0;
    Ok(status)
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;
    use std::{fs, os::unix::fs::PermissionsExt};

    #[test]
    fn drain_uses_full_counts_and_includes_preparing_requests_without_writes() {
        let temp = tempfile::tempdir().unwrap();
        let mut config = Config::new(temp.path().to_owned());
        config.offline = true;
        for dir in ["run", "gateway", "supervisor"] {
            fs::create_dir(config.home.join(dir)).unwrap();
        }
        fs::write(config.home.join("run/freeze"), "").unwrap();
        let gateway = config.db();
        let tasks = config.home.join("supervisor/task-state.sqlite3");
        Connection::open(&gateway)
            .unwrap()
            .execute_batch("CREATE TABLE events(status TEXT); CREATE TABLE updates(status TEXT);")
            .unwrap();
        let db = Connection::open(&tasks).unwrap();
        db.execute_batch("CREATE TABLE requests(id TEXT,state TEXT);")
            .unwrap();
        for i in 0..250 {
            db.execute(
                "INSERT INTO requests VALUES(?,'completed')",
                [i.to_string()],
            )
            .unwrap();
        }
        db.execute("INSERT INTO requests VALUES('old-active','running')", [])
            .unwrap();
        for path in [&gateway, &tasks] {
            fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
        }
        let before = fs::read(&tasks).unwrap();
        let status = drain_status(&config).unwrap();
        assert_eq!(status.tasks, 1);
        assert!(!status.ready);
        assert_eq!(before, fs::read(&tasks).unwrap());
        db.execute(
            "UPDATE requests SET state='queued' WHERE id='old-active'",
            [],
        )
        .unwrap();
        let lock = runtime::acquire_task_lock(
            &config
                .home
                .join("run")
                .join(format!("request-{}.lock", crate::digest("old-active"))),
        )
        .unwrap()
        .unwrap();
        let status = drain_status(&config).unwrap();
        assert_eq!(status.starting, 1);
        assert!(!status.ready);
        drop(lock);
        assert!(drain_status(&config).unwrap().ready);
        fs::remove_file(config.home.join("run/freeze")).unwrap();
        assert!(!drain_status(&config).unwrap().ready);
    }
}
