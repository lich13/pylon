//! Read-only compatibility inspection. Never initialize or reconcile live state.
use crate::config::Config;
use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, OpenFlags, OptionalExtension};
use serde::Serialize;
use serde_json::Value;
use std::{
    collections::BTreeSet,
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

pub fn check(config: &Config, target: &str) -> Report {
    let mut report = Report {
        ok: false,
        target_version: target.into(),
        completions_pending: None,
        reason: None,
    };
    if !matches!(
        target,
        "0.5.3" | "0.5.4" | "0.5.5" | "0.5.6" | "0.5.7" | "0.5.8" | "0.5.9" | "0.5.10"
    ) {
        report.reason = Some("unsupported_target");
        return report;
    }
    match inspect(config, target, &mut report.completions_pending) {
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
    target: &str,
    pending: &mut Option<u64>,
) -> Result<Option<&'static str>> {
    let path = config.contained("gateway/state.sqlite3")?;
    let original = fs::symlink_metadata(config.db())?;
    ensure!(original.is_file(), "unsafe downgrade state");
    ensure!(
        unsafe { libc::geteuid() } == 0 || original.uid() == unsafe { libc::geteuid() },
        "downgrade state owner mismatch"
    );
    let count = crate::completion::pending_count(&path)?;
    *pending = Some(count);
    if count != 0 {
        return Ok(Some("pending_completions"));
    }
    let (_lock, db) = read_only_state(&path)?;
    db.busy_timeout(Duration::from_secs(1))?;
    db.execute_batch("PRAGMA query_only=ON; BEGIN;")?;
    let mut history = db.prepare("SELECT key,value FROM kv WHERE (key>='event-gone:' AND key<'event-gone;') OR (key>='event-gone-msg:' AND key<'event-gone-msg;')")?;
    for row in history.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))? {
        let (key, raw) = row?;
        if crate::event_history::expires(&key, &raw)? > crate::now() {
            return Ok(Some("retained_events_require_newer_gateway"));
        }
    }
    let counter = state(&db, "uncertain:counter:v2")?;
    let retired = state(&db, "uncertain:counter:v2:retired")?;
    for counter in counter.iter().chain(retired.iter()) {
        ensure!(
            counter["version"].as_u64() == Some(2)
                && counter["pending"].as_u64().is_some()
                && counter["last_day"].is_string(),
            "invalid uncertain counter"
        );
    }
    db.query_row("SELECT count(*) FROM events WHERE 0", [], |row| {
        row.get::<_, u64>(0)
    })?;
    if target == "0.5.3" {
        let triggers = state(&db, "uncertain:triggers")?;
        ensure!(
            triggers.is_none() || triggers == Some(serde_json::json!(3)),
            "unknown uncertain triggers"
        );
        if counter.is_some() || (triggers.is_some() && retired.is_none()) {
            return Ok(Some("legacy_counter_preparation_required"));
        }
        let watermark =
            state(&db, "uncertain:watermark")?.context("legacy uncertain watermark missing")?;
        let seen: BTreeSet<String> = serde_json::from_value(watermark)?;
        let mut query = db.prepare(
            "SELECT id FROM events WHERE status='uncertain' AND job_id<>'uncertain-summary'",
        )?;
        for id in query.query_map([], |row| row.get::<_, String>(0))? {
            ensure!(seen.contains(&id?), "legacy uncertain watermark incomplete");
        }
    } else if counter.is_none()
        && let Some(watermark) = state(&db, "uncertain:watermark")?
    {
        // Current versions initialize an absent counter at startup. Inspect its
        // legacy input now without creating a seed or changing the old watermark.
        let _: BTreeSet<String> = serde_json::from_value(watermark)?;
    }
    // Catch a writer that added a completion during the read transaction. This
    // remains an advisory check; deployment must freeze, drain, stop and recheck.
    let count = crate::completion::pending_count(&path)?;
    *pending = Some(count);
    Ok((count != 0).then_some("pending_completions"))
}

/// SQLite mode=ro can still create WAL/SHM sidecars. Hold a POSIX database lock
/// while choosing between an existing WAL index and a fully quiescent file.
/// The lock descriptor needs write permission for F_WRLCK, but is never written.
fn read_only_state(path: &Path) -> Result<(File, Connection)> {
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

fn state(db: &Connection, key: &str) -> Result<Option<Value>> {
    let raw: Option<String> = db
        .query_row("SELECT value FROM kv WHERE key=?", [key], |row| row.get(0))
        .optional()?;
    raw.map(|raw| serde_json::from_str(&raw))
        .transpose()
        .map_err(Into::into)
}
