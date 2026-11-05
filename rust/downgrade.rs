//! Read-only compatibility inspection. Never initialize or reconcile live state.
use crate::config::Config;
use anyhow::{Result, ensure};
use rusqlite::{Connection, OpenFlags};
use serde::Serialize;
use std::{
    fs::{self, File, OpenOptions},
    os::{
        fd::AsRawFd,
        unix::fs::{MetadataExt, OpenOptionsExt},
    },
    path::Path,
    time::Duration,
};

#[derive(Debug, Serialize)]
pub struct Report {
    pub ok: bool,
    pub target_version: String,
    pub completions_pending: Option<u64>,
    pub reason: Option<&'static str>,
}

/// Highest gateway state version (`PRAGMA user_version`) a rollback target can
/// run on. Every supported release ignores `user_version` and `runs.result`,
/// and names the `runs` columns it reads, so v0.5.30 state (1) stays readable.
/// v0.5.18 was withdrawn; v0.5.10 and older lack retained-history support.
fn readable_state(target: &str) -> Option<i64> {
    let minor = target.strip_prefix("0.5.")?;
    if minor.is_empty() || minor.starts_with('0') || !minor.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let patch: u32 = minor.parse().ok()?;
    ((11..=30).contains(&patch) && patch != 18).then_some(1)
}

pub fn check(config: &Config, target: &str) -> Report {
    let mut report = Report {
        ok: false,
        target_version: target.into(),
        completions_pending: None,
        reason: None,
    };
    let Some(readable) = readable_state(target) else {
        report.reason = Some("unsupported_target");
        return report;
    };
    match inspect(config, readable, &mut report.completions_pending) {
        Ok(reason) => {
            report.ok = reason.is_none();
            report.reason = reason;
        }
        Err(error) => {
            crate::errors::log("downgrade-check", &error);
            report.reason = Some("state_unavailable");
        }
    }
    report
}

fn inspect(
    config: &Config,
    readable: i64,
    pending: &mut Option<u64>,
) -> Result<Option<&'static str>> {
    let path = config.contained("gateway/state.sqlite3")?;
    let original = fs::symlink_metadata(config.db())?;
    ensure!(original.is_file(), "unsafe downgrade state");
    ensure!(
        unsafe { libc::geteuid() } == 0 || original.uid() == unsafe { libc::geteuid() },
        "downgrade state owner mismatch"
    );
    // Older releases may have left records in their completion mailbox.
    let count = crate::completion::pending_count(&path)?;
    *pending = Some(count);
    if count != 0 {
        return Ok(Some("pending_completions"));
    }
    let (_lock, db) = read_only_state(&path)?;
    db.busy_timeout(Duration::from_secs(1))?;
    let version: i64 = db.query_row("PRAGMA user_version", [], |row| row.get(0))?;
    db.query_row("SELECT count(*) FROM events WHERE 0", [], |row| {
        row.get::<_, u64>(0)
    })?;
    Ok((version > readable).then_some("state_requires_newer_gateway"))
}

/// SQLite mode=ro can still create WAL/SHM sidecars. Hold a POSIX database lock
/// while choosing between an existing WAL index and a fully quiescent file.
/// The lock descriptor needs write permission for F_WRLCK, but is never written.
pub(crate) fn read_only_state(path: &Path) -> Result<(File, Connection)> {
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let identity = lock.metadata()?;
    let current = fs::symlink_metadata(path)?;
    ensure!(
        identity.dev() == current.dev() && identity.ino() == current.ino(),
        "downgrade state changed"
    );
    database_lock(&lock, libc::F_RDLCK)?;
    let mut wal_path = path.as_os_str().to_owned();
    wal_path.push("-wal");
    let mut shm_path = path.as_os_str().to_owned();
    shm_path.push("-shm");
    let sidecar = |path: &std::ffi::OsStr| -> Result<Option<fs::Metadata>> {
        match fs::symlink_metadata(path) {
            Ok(metadata) => {
                ensure!(
                    metadata.is_file()
                        && metadata.nlink() == 1
                        && metadata.uid() == identity.uid()
                        && metadata.mode() & 0o777 == 0o600,
                    "unsafe database sidecar"
                );
                Ok(Some(metadata))
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error.into()),
        }
    };
    let wal = sidecar(&wal_path)?;
    let shm = sidecar(&shm_path)?;
    let mut uri = reqwest::Url::from_file_path(path)
        .map_err(|_| anyhow::anyhow!("invalid downgrade state path"))?;
    uri.query_pairs_mut().append_pair("mode", "ro");
    if wal.is_some() && shm.is_some() {
        // The shared database lock prevents last-connection WAL deletion. The
        // SQLite reader uses existing sidecars and cannot create the SHM file.
        uri.query_pairs_mut().append_pair("readonly_shm", "1");
    } else {
        // Never label a live database immutable or ignore uncheckpointed WAL.
        database_lock(&lock, libc::F_WRLCK)?;
        ensure!(
            sidecar(&wal_path)?.is_none_or(|metadata| metadata.len() == 0),
            "database requires recovery"
        );
        let mut journal = path.as_os_str().to_owned();
        journal.push("-journal");
        ensure!(
            sidecar(&journal)?.is_none_or(|metadata| metadata.len() == 0),
            "database requires recovery"
        );
        uri.query_pairs_mut().append_pair("immutable", "1");
    }
    let db = Connection::open_with_flags(
        uri.as_str(),
        OpenFlags::SQLITE_OPEN_READ_ONLY
            | OpenFlags::SQLITE_OPEN_URI
            | OpenFlags::SQLITE_OPEN_NOFOLLOW,
    )?;
    Ok((lock, db))
}

fn database_lock(file: &File, kind: impl Into<libc::c_int>) -> Result<()> {
    // The whole file includes SQLite's pending/reserved/shared lock bytes. A
    // nonblocking failure is an unavailable check, never permission to proceed.
    let mut lock: libc::flock = unsafe { std::mem::zeroed() };
    lock.l_type = kind.into() as _;
    lock.l_whence = libc::SEEK_SET as _;
    if unsafe { libc::fcntl(file.as_raw_fd(), libc::F_SETLK, &lock) } == -1 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(())
}
