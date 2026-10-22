//! Only request/dispatch lock files are reclaimable. Openers validate identity
//! after flock, so an opener waiting on an unlinked inode cannot enter a job.
use crate::{config::Config, runtime};
use anyhow::{Result, ensure};
use rusqlite::OptionalExtension;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::{self, ReadDir},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::Path,
};
use tokio::sync::watch;

type Requests = BTreeMap<String, (String, String, f64)>;
#[derive(Default)]
pub(crate) struct Scan {
    directory: Option<ReadDir>,
    identity: Option<(u64, u64)>,
    entries_seen: u64,
    entries_removed: u64,
    directory_started_at: Option<f64>,
    directory_entries: Option<u64>,
    directory_sampled_at: Option<f64>,
    checked: u64,
    deleted: u64,
    skipped: u64,
    scan_error: Option<&'static str>,
    finished: bool,
}
impl Scan {
    pub(crate) fn in_progress(&self) -> bool {
        self.directory.is_some()
    }
    fn snapshot(&self) -> Value {
        json!({"checked":self.checked,"deleted":self.deleted,"skipped":self.skipped,"directory_entries":self.directory_entries,"directory_sampled_at":self.directory_sampled_at,"directory_scan_started_at":self.directory_started_at,"scan_error":self.scan_error,"sampled_at":crate::now(),"scan_in_progress":self.directory.is_some()})
    }
}

fn requests(config: &Config) -> Result<Requests> {
    let db = crate::queue::connect(config)?;
    let mut q = db.prepare("SELECT id,state,updated_at FROM requests")?;
    let mut result = BTreeMap::new();
    for row in q.query_map([], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, f64>(2)?,
        ))
    })? {
        let (id, state, at) = row?;
        result.insert(crate::digest(&id), (id, state, at));
    }
    Ok(result)
}
fn candidate(name: &str) -> Option<&str> {
    let hash = name
        .strip_prefix("dispatch-")
        .or_else(|| name.strip_prefix("request-"))?
        .strip_suffix(".lock")?;
    (hash.len() == 64
        && hash
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c)))
    .then_some(hash)
}
fn safe(metadata: &fs::Metadata) -> bool {
    // Executor runs as root:pylon and creates root-owned locks. Allow the same
    // process to reclaim group-owned empty request/dispatch locks after a UID
    // migration without widening to arbitrary files (name + emptiness still bind).
    let euid = unsafe { libc::geteuid() };
    let egid = unsafe { libc::getegid() };
    let owner_ok = metadata.uid() == euid || (euid == 0 && metadata.gid() == egid);
    metadata.is_file()
        && owner_ok
        && metadata.nlink() == 1
        && metadata.len() == 0
        && metadata.mode() & 0o7007 == 0
        && metadata.mode() & 0o700 == 0o600
        && metadata.mode() & 0o010 == 0
}
fn old(metadata: &fs::Metadata, now: f64) -> bool {
    (metadata.mtime() as f64) < now - 86400.0 && (metadata.ctime() as f64) < now - 86400.0
}

pub(crate) fn remove(
    config: &Config,
    path: &Path,
    known: Option<&(String, String, f64)>,
    now: f64,
) -> Result<bool> {
    if let Some((_, state, at)) = known
        && (state != "completed" || !at.is_finite() || *at < 0.0 || *at >= now - 86400.0)
    {
        return Ok(false);
    }
    let file = match fs::OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
    {
        Ok(file) => file,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(e) => return Err(e.into()),
    };
    let metadata = file.metadata()?;
    ensure!(safe(&metadata), "unsafe request lock");
    if known.is_none() && !old(&metadata, now) {
        return Ok(false);
    }
    if !runtime::try_lock(&file)? {
        return Ok(false);
    }
    ensure!(
        runtime::task_lock_current(path, &file)?,
        "request lock identity changed"
    );
    let mut orphan = known.is_none();
    if let Some((id, _, _)) = known {
        let state = crate::queue::connect(config)?
            .query_row(
                "SELECT state,updated_at FROM requests WHERE id=?",
                [id],
                |r| Ok((r.get::<_, String>(0)?, r.get::<_, f64>(1)?)),
            )
            .optional()?;
        match state {
            Some((state, at))
                if state == "completed" && at.is_finite() && at >= 0.0 && at < now - 86400.0 => {}
            None if old(&metadata, now) => {
                orphan = true;
            }
            _ => return Ok(false),
        }
    }
    let current = file.metadata()?;
    ensure!(
        safe(&current) && runtime::task_lock_current(path, &file)?,
        "request lock identity changed"
    );
    if orphan && !old(&current, now) {
        return Ok(false);
    }
    fs::remove_file(path)?;
    Ok(true)
}

fn batch(
    config: &Config,
    scan: &mut Scan,
    requests: &Requests,
    stop: &watch::Receiver<bool>,
) -> Result<()> {
    let run = config.home.join("run");
    let metadata = fs::symlink_metadata(&run)?;
    ensure!(metadata.is_dir(), "unsafe run directory");
    let identity = (metadata.dev(), metadata.ino());
    if scan.directory.is_none() {
        scan.directory = Some(fs::read_dir(&run)?);
        scan.identity = Some(identity);
        scan.entries_seen = 0;
        scan.entries_removed = 0;
        scan.directory_started_at = Some(crate::now());
    }
    ensure!(scan.identity == Some(identity), "run directory changed");
    for _ in 0..1000 {
        if *stop.borrow() || run.join("freeze").exists() {
            break;
        }
        let entry = scan.directory.as_mut().expect("scan open").next();
        let Some(entry) = entry else {
            scan.directory = None;
            scan.directory_entries = Some(scan.entries_seen.saturating_sub(scan.entries_removed));
            scan.directory_sampled_at = Some(crate::now());
            scan.finished = true;
            break;
        };
        let entry = entry?;
        scan.entries_seen += 1;
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        let Some(hash) = candidate(name) else {
            continue;
        };
        scan.checked += 1;
        match remove(config, &entry.path(), requests.get(hash), crate::now()) {
            Ok(true) => {
                scan.deleted += 1;
                scan.entries_removed += 1;
            }
            Ok(false) => scan.skipped += 1,
            Err(error) if error.downcast_ref::<rusqlite::Error>().is_some() => return Err(error),
            Err(_) => {
                scan.skipped += 1;
                scan.scan_error = Some("lock_safety_check_failed");
            }
        }
    }
    Ok(())
}

pub(crate) async fn run(config: &Config, mut scan: Scan, stop: &mut watch::Receiver<bool>) -> Scan {
    if *stop.borrow() || config.home.join("run/freeze").exists() {
        return scan;
    }
    scan.checked = 0;
    scan.deleted = 0;
    scan.skipped = 0;
    scan.finished = false;
    scan.scan_error = None;
    let read = config.clone();
    let requests = match crate::blocking(move || requests(&read)).await {
        Ok(requests) => std::sync::Arc::new(requests),
        Err(_) => {
            scan.scan_error = Some("request_state_unavailable");
            crate::retention::publish_locks(config, scan.snapshot());
            return scan;
        }
    };
    // Bound entries as well as candidates: unrelated files cannot monopolize a round.
    for _ in 0..50 {
        if *stop.borrow() || config.home.join("run/freeze").exists() {
            break;
        }
        let config_copy = config.clone();
        let requests = requests.clone();
        let stopping = stop.clone();
        let result = crate::blocking(move || {
            let result = batch(&config_copy, &mut scan, &requests, &stopping);
            if let Err(error) = result {
                scan.directory = None;
                scan.scan_error = Some(if error.downcast_ref::<rusqlite::Error>().is_some() {
                    "request_state_unavailable"
                } else {
                    "directory_scan_unavailable"
                });
                scan.finished = true;
            }
            Ok(scan)
        })
        .await;
        scan = match result {
            Ok(scan) => scan,
            Err(_) => {
                let scan = Scan {
                    scan_error: Some("directory_scan_unavailable"),
                    ..Scan::default()
                };
                crate::retention::publish_locks(config, scan.snapshot());
                return scan;
            }
        };
        crate::retention::publish_locks(config, scan.snapshot());
        if scan.finished {
            break;
        }
        tokio::select! {_=stop.changed()=>break,_=tokio::time::sleep(std::time::Duration::from_millis(500))=>{}}
    }
    scan
}

#[cfg(test)]
#[path = "lock_reaper_tests.rs"]
mod tests;
