//! Per-request execution locks held in the supervisor database (v0.5.30).
//!
//! A lock row names its owner by boot id, PID and process start time, so it is
//! released by process exit without a lock file, an age heuristic or a reaper:
//! a row whose owner no longer exists is free. The dispatcher takes the lock,
//! spawns `resume --dispatched` and hands the row to the child, which keeps it
//! until the run ends. Job, quota, admission and summary locks stay `flock`
//! files: they have fixed names, and modules may open job locks themselves.
use crate::config::Config;
use anyhow::{Context, Result};
use rusqlite::{OptionalExtension, params};
use std::time::Duration;

pub(crate) const SCHEMA: &str = "CREATE TABLE IF NOT EXISTS locks(name TEXT PRIMARY KEY,owner TEXT NOT NULL,acquired_at REAL NOT NULL);";

fn boot_id() -> Result<String> {
    Ok(std::fs::read_to_string("/proc/sys/kernel/random/boot_id")
        .context("boot id unavailable")?
        .trim()
        .to_owned())
}

/// `<boot id>:<pid>:<start time>`; `None` once the process is gone.
pub(crate) fn identity(pid: u32) -> Result<Option<String>> {
    let stat = match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        Ok(stat) => stat,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    // Field 22 (start time); the command name in field 2 may contain spaces.
    let start = stat
        .rsplit_once(')')
        .and_then(|(_, rest)| rest.split_whitespace().nth(19))
        .context("invalid process status")?;
    Ok(Some(format!("{}:{pid}:{start}", boot_id()?)))
}

pub(crate) fn alive(owner: &str) -> Result<bool> {
    let Some(pid) = owner
        .split(':')
        .nth(1)
        .and_then(|pid| pid.parse::<u32>().ok())
    else {
        return Ok(false);
    };
    Ok(identity(pid)?.as_deref() == Some(owner))
}

fn name(run: &str) -> String {
    format!("request:{run}")
}

/// Whether a live process holds the request lock. Never changes state.
pub(crate) fn held(db: &rusqlite::Connection, run: &str) -> Result<bool> {
    // An older gateway's database has no lock table until this release opens it.
    let table: i64 = db.query_row(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='locks'",
        [],
        |r| r.get(0),
    )?;
    if table == 0 {
        return Ok(false);
    }
    let owner: Option<String> = db
        .query_row("SELECT owner FROM locks WHERE name=?", [name(run)], |r| {
            r.get(0)
        })
        .optional()?;
    owner.map_or(Ok(false), |owner| alive(&owner))
}

pub struct RequestLock {
    config: Config,
    name: String,
    owner: Option<String>,
}

impl RequestLock {
    /// Take the lock for this process, or `None` while another live process holds it.
    pub(crate) fn acquire(config: &Config, run: &str) -> Result<Option<Self>> {
        let me = identity(std::process::id())?.context("process identity unavailable")?;
        let name = name(run);
        let mut db = crate::queue::connect(config)?;
        // Skip the write transaction while a live owner holds it.
        if held(&db, run)? {
            return Ok(None);
        }
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let owner: Option<String> = tx
            .query_row("SELECT owner FROM locks WHERE name=?", [&name], |r| {
                r.get(0)
            })
            .optional()?;
        if let Some(owner) = owner
            && owner != me
            && alive(&owner)?
        {
            return Ok(None);
        }
        tx.execute(
            "INSERT INTO locks VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,acquired_at=excluded.acquired_at",
            params![name, me, crate::now()],
        )?;
        tx.commit()?;
        Ok(Some(Self {
            config: config.clone(),
            name,
            owner: Some(me),
        }))
    }

    /// Wait for a dispatcher to hand this process the lock.
    pub(crate) fn adopt(config: &Config, run: &str) -> Result<Option<Self>> {
        let me = identity(std::process::id())?.context("process identity unavailable")?;
        let name = name(run);
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        loop {
            let owner: Option<String> = crate::queue::connect(config)?
                .query_row("SELECT owner FROM locks WHERE name=?", [&name], |r| {
                    r.get(0)
                })
                .optional()?;
            match owner {
                Some(owner) if owner == me => {
                    return Ok(Some(Self {
                        config: config.clone(),
                        name,
                        owner: Some(me),
                    }));
                }
                Some(owner) if alive(&owner)? && std::time::Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(20));
                }
                _ => return Ok(None),
            }
        }
    }

    /// Hand the lock to a spawned child. A child that already exited frees it.
    pub(crate) fn transfer(mut self, pid: u32) -> Result<()> {
        let owner = self.owner.take().context("request lock released")?;
        let db = crate::queue::connect(&self.config)?;
        match identity(pid)? {
            Some(child) => db.execute(
                "UPDATE locks SET owner=?,acquired_at=? WHERE name=? AND owner=?",
                params![child, crate::now(), self.name, owner],
            )?,
            None => db.execute(
                "DELETE FROM locks WHERE name=? AND owner=?",
                params![self.name, owner],
            )?,
        };
        Ok(())
    }
}

impl Drop for RequestLock {
    fn drop(&mut self) {
        let Some(owner) = self.owner.take() else {
            return;
        };
        let result = crate::queue::connect(&self.config).and_then(|db| {
            db.execute(
                "DELETE FROM locks WHERE name=? AND owner=?",
                params![self.name, owner],
            )?;
            Ok(())
        });
        // A row left behind is freed by this process exiting.
        if let Err(error) = result {
            crate::errors::log("request-lock-release", &error);
        }
    }
}

/// Delete rows of exited owners and, once, the request/dispatch lock files of
/// v0.5.29 and earlier that no process holds. Bounded and idempotent.
pub(crate) fn sweep(config: &Config) -> Result<serde_json::Value> {
    let db = crate::queue::connect(config)?;
    let rows = db
        .prepare("SELECT name,owner FROM locks LIMIT 10000")?
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut released = 0_u64;
    for (name, owner) in rows {
        if !alive(&owner)? {
            released += db.execute(
                "DELETE FROM locks WHERE name=? AND owner=?",
                params![name, owner],
            )? as u64;
        }
    }
    let held = db.query_row("SELECT count(*) FROM locks", [], |r| r.get::<_, u64>(0))?;
    let legacy = remove_legacy_files(config)?;
    // Keep the run-directory size signal that diagnose has always reported.
    let entries = match std::fs::read_dir(config.home.join("run")) {
        Ok(entries) => entries.count() as u64,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => 0,
        Err(error) => return Err(error.into()),
    };
    let now = crate::now();
    Ok(serde_json::json!({
        "held": held,
        "released": released,
        "legacy_files_removed": legacy,
        "directory_entries": entries,
        "directory_sampled_at": now,
        "directory_scan_started_at": now,
    }))
}

fn legacy_name(name: &str) -> bool {
    name.strip_prefix("dispatch-")
        .or_else(|| name.strip_prefix("request-"))
        .and_then(|rest| rest.strip_suffix(".lock"))
        .is_some_and(|hash| {
            hash.len() == 64
                && hash
                    .bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        })
}

fn remove_legacy_files(config: &Config) -> Result<u64> {
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    let euid = unsafe { libc::geteuid() };
    let mut removed = 0;
    let entries = match std::fs::read_dir(config.home.join("run")) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(error) => return Err(error.into()),
    };
    for entry in entries {
        let entry = entry?;
        if !entry.file_name().to_str().is_some_and(legacy_name) {
            continue;
        }
        let path = entry.path();
        let file = match std::fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(&path)
        {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(_) => continue,
        };
        let metadata = file.metadata()?;
        // Only empty regular lock files of this user or, for root, any owner.
        if !metadata.is_file() || metadata.len() != 0 || (euid != 0 && metadata.uid() != euid) {
            continue;
        }
        // A previous release may still hold it during a rollback window.
        if !crate::runtime::try_lock(&file)? || !crate::runtime::task_lock_current(&path, &file)? {
            continue;
        }
        std::fs::remove_file(&path)?;
        removed += 1;
    }
    Ok(removed)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> (tempfile::TempDir, Config) {
        let temp = tempfile::tempdir().unwrap();
        let mut config = Config::new(temp.path().to_owned());
        config.offline = true;
        std::fs::create_dir_all(config.home.join("run")).unwrap();
        (temp, config)
    }

    #[test]
    fn request_locks_live_in_sqlite_and_free_on_owner_exit() {
        let (_temp, config) = config();
        let lock = RequestLock::acquire(&config, "run-a").unwrap().unwrap();
        let db = crate::queue::connect(&config).unwrap();
        assert!(held(&db, "run-a").unwrap());
        assert!(!held(&db, "run-b").unwrap());
        drop(lock);
        assert!(!held(&db, "run-a").unwrap());
        // A row owned by an exited process is free and reclaimed.
        let mut child = std::process::Command::new("true").spawn().unwrap();
        let pid = child.id();
        let dead = identity(pid).unwrap().unwrap();
        child.wait().unwrap();
        db.execute("INSERT INTO locks VALUES('request:run-c',?,1)", [&dead])
            .unwrap();
        assert!(!held(&db, "run-c").unwrap());
        let taken = RequestLock::acquire(&config, "run-c").unwrap();
        assert!(taken.is_some());
        drop(taken);
        db.execute("INSERT INTO locks VALUES('request:run-d',?,1)", [&dead])
            .unwrap();
        assert_eq!(sweep(&config).unwrap()["released"], 1);
        assert!(!config.home.join("run/request-x.lock").exists());
        assert_eq!(
            db.query_row("SELECT count(*) FROM locks", [], |r| r.get::<_, u64>(0))
                .unwrap(),
            0
        );
    }

    #[test]
    fn a_live_owner_blocks_and_a_transfer_hands_over() {
        let (_temp, config) = config();
        let mut child = std::process::Command::new("sleep")
            .arg("30")
            .spawn()
            .unwrap();
        let lock = RequestLock::acquire(&config, "run-a").unwrap().unwrap();
        lock.transfer(child.id()).unwrap();
        let db = crate::queue::connect(&config).unwrap();
        assert!(held(&db, "run-a").unwrap());
        // The dispatcher no longer owns it; a second taker is refused.
        assert!(RequestLock::acquire(&config, "run-a").unwrap().is_none());
        child.kill().unwrap();
        child.wait().unwrap();
        assert!(!held(&db, "run-a").unwrap());
        assert!(RequestLock::acquire(&config, "run-a").unwrap().is_some());
    }

    #[test]
    fn unheld_legacy_lock_files_are_removed_and_held_ones_kept() {
        let (_temp, config) = config();
        let hash = crate::digest("run-a");
        let free = config.home.join(format!("run/request-{hash}.lock"));
        let busy = config.home.join(format!("run/dispatch-{hash}.lock"));
        let job = config.home.join("run/job.lock");
        for path in [&free, &busy, &job] {
            std::fs::write(path, b"").unwrap();
        }
        let holder = crate::runtime::acquire_task_lock(&busy).unwrap().unwrap();
        assert_eq!(sweep(&config).unwrap()["legacy_files_removed"], 1);
        assert!(!free.exists() && busy.exists() && job.exists());
        drop(holder);
        assert_eq!(sweep(&config).unwrap()["legacy_files_removed"], 1);
        assert!(!busy.exists() && job.exists());
    }
}
