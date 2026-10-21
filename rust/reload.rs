//! File metadata is a precheck, never a replacement for validating a changed input.
use crate::{
    api::Snapshot,
    catalog::Catalog,
    config::{Config, Settings},
};
use anyhow::{Context, Result, ensure};
use std::{
    collections::BTreeMap,
    fs,
    io::Read,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, PartialEq, Eq)]
struct Stamp {
    regular: bool,
    dev: u64,
    ino: u64,
    mode: u32,
    uid: u32,
    gid: u32,
    links: u64,
    size: u64,
    modified: (i64, i64),
    changed: (i64, i64),
}
impl Stamp {
    fn read(path: &Path) -> Result<Self> {
        let m = fs::symlink_metadata(path)?;
        Ok(Self {
            regular: m.is_file(),
            dev: m.dev(),
            ino: m.ino(),
            mode: m.mode(),
            uid: m.uid(),
            gid: m.gid(),
            links: m.nlink(),
            size: m.len(),
            modified: (m.mtime(), m.mtime_nsec()),
            changed: (m.ctime(), m.ctime_nsec()),
        })
    }
    fn directory(path: &Path) -> Result<Self> {
        let mut stamp = Self::read(path)?;
        // Data/cache children are intentionally absent from the content hash.
        // Enumeration below, not directory timestamps, discovers source changes.
        stamp.size = 0;
        stamp.modified = (0, 0);
        stamp.changed = (0, 0);
        stamp.links = 0;
        Ok(stamp)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Inputs {
    bots: Vec<u8>,
    registry: Option<Vec<u8>>,
    declarations: Option<serde_json::Value>,
    paths: BTreeMap<PathBuf, Stamp>,
    roots: BTreeMap<String, PathBuf>,
    workspaces: Vec<PathBuf>,
    // Full loads also compare directory change times to detect an add/remove
    // race that restores the same file list. Ordinary probes ignore these times.
    directory_versions: BTreeMap<PathBuf, Stamp>,
}
impl Inputs {
    pub fn unchanged(&self, config: &Config, settings: &Settings) -> Result<bool> {
        // Changed settings can remove a credential or workspace that no longer
        // exists. Validate the new candidate instead of probing obsolete paths.
        if read_input(&config.contained("config/bots.json")?)? != self.bots {
            return Ok(false);
        }
        let candidate = Self::capture_previous(config, settings, Some(self))?;
        Ok(self.bots == candidate.bots
            && self.registry == candidate.registry
            && self.paths == candidate.paths
            && self.roots == candidate.roots
            && self.workspaces == candidate.workspaces)
    }
    pub fn capture(config: &Config, settings: &Settings) -> Result<Self> {
        Self::capture_previous(config, settings, None)
    }
    fn capture_previous(
        config: &Config,
        settings: &Settings,
        previous: Option<&Self>,
    ) -> Result<Self> {
        let bots_path = config.contained("config/bots.json")?;
        let bots = read_input(&bots_path)?;
        let base = config.contained("modules")?.canonicalize()?;
        let registry_path = base.join("registry.json");
        let registry_target = config.contained("modules/registry.json")?;
        let registry = match fs::symlink_metadata(&registry_path) {
            Ok(_) => Some(read_input(&registry_target)?),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
            Err(e) => return Err(e.into()),
        };
        let declarations = if let Some(previous) = previous.filter(|p| p.registry == registry) {
            previous.declarations.clone()
        } else {
            registry
                .as_deref()
                .map(serde_json::from_slice)
                .transpose()?
        };
        // Reuse parsing only. Resolve every declared path again so a replaced
        // symlink or directory cannot bypass containment checks.
        let roots = crate::catalog::module_roots(&base, declarations.clone())?;
        let mut paths = BTreeMap::new();
        let mut directory_versions = BTreeMap::new();
        directory_versions.insert(base.clone(), Stamp::read(&base)?);
        paths.insert(bots_path.clone(), Stamp::read(&bots_path)?);
        paths.insert(base.clone(), Stamp::directory(&base)?);
        if registry.is_some() {
            paths.insert(registry_path.clone(), Stamp::read(&registry_path)?);
            paths.insert(registry_target.clone(), Stamp::read(&registry_target)?);
        }
        for root in roots.values() {
            ensure!(root.starts_with(&base), "module escapes registry");
            paths.insert(root.clone(), Stamp::directory(root)?);
            directory_versions.insert(root.clone(), Stamp::read(root)?);
            let sources = crate::modules::source_inventory(root)?;
            for relative in sources.directories {
                let directory = root.join(relative);
                paths.insert(directory.clone(), Stamp::directory(&directory)?);
                directory_versions.insert(directory.clone(), Stamp::read(&directory)?);
            }
            for relative in sources.files {
                let path = root.join(relative);
                let stamp = Stamp::read(&path)?;
                ensure!(stamp.regular, "module source changed type");
                paths.insert(path, stamp);
            }
        }
        for bot in settings.bots.values().filter(|b| b.enabled) {
            let path = config.home.join("gateway/secrets").join(&bot.credential);
            config.contained(&format!("gateway/secrets/{}", bot.credential))?;
            let stamp = Stamp::read(&path).context("credential unavailable")?;
            ensure!(
                stamp.regular && stamp.mode & 0o077 == 0,
                "unsafe credential permissions"
            );
            paths.insert(path, stamp);
        }
        let mut workspaces = Vec::new();
        for workspace in settings.workspaces.values() {
            workspaces.push(config.contained(&workspace.root)?);
            if !workspace.env.is_empty() {
                workspaces.push(config.contained(&workspace.env)?);
            }
        }
        Ok(Self {
            bots,
            registry,
            declarations,
            paths,
            roots,
            workspaces,
            directory_versions,
        })
    }
}

fn read_input(path: &Path) -> Result<Vec<u8>> {
    let mut file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    ensure!(
        file.metadata()?.is_file(),
        "configuration input is not a regular file"
    );
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    Ok(bytes)
}

pub(crate) fn load(config: &Config) -> Result<(Snapshot, Inputs)> {
    for _ in 0..2 {
        let bytes = read_input(&config.contained("config/bots.json")?)?;
        let settings = config.settings_from_bytes(&bytes)?;
        let before = Inputs::capture(config, &settings)?;
        if before.bots != bytes {
            continue;
        }
        let candidate = (|| -> Result<_> {
            Ok((
                config.credential_fingerprints(&settings)?,
                Catalog::load_with_settings(config, &settings)?,
            ))
        })();
        // A disappearing/replaced input can make the candidate read fail too.
        // Retry that race, but report stable validation failures unchanged.
        if let Ok(after) = Inputs::capture(config, &settings)
            && before == after
        {
            let (credentials, catalog) = candidate?;
            return Ok((
                Snapshot {
                    settings,
                    catalog,
                    credentials,
                },
                after,
            ));
        }
    }
    anyhow::bail!("configuration changed while loading")
}

#[cfg(test)]
pub(crate) mod reads {
    use super::*;
    use std::sync::{Mutex, OnceLock};
    static READS: OnceLock<Mutex<BTreeMap<PathBuf, u64>>> = OnceLock::new();
    type Changes = BTreeMap<PathBuf, std::collections::VecDeque<Vec<u8>>>;
    static CHANGES: OnceLock<Mutex<Changes>> = OnceLock::new();
    pub fn record(path: &Path) {
        let normalized = path.canonicalize().unwrap();
        let path = normalized.as_path();
        *READS
            .get_or_init(Default::default)
            .lock()
            .unwrap()
            .entry(path.to_owned())
            .or_default() += 1;
        if let Some(bytes) = CHANGES
            .get_or_init(Default::default)
            .lock()
            .unwrap()
            .get_mut(path)
            .and_then(|changes| changes.pop_front())
        {
            fs::write(path.join("handler.sh"), bytes).unwrap();
        }
    }
    pub fn count(path: &Path) -> u64 {
        let normalized = path.canonicalize().unwrap();
        let path = normalized.as_path();
        READS
            .get_or_init(Default::default)
            .lock()
            .unwrap()
            .get(path)
            .copied()
            .unwrap_or(0)
    }
    pub fn change_during_hash(root: &Path, contents: &[&[u8]]) {
        CHANGES
            .get_or_init(Default::default)
            .lock()
            .unwrap()
            .insert(
                root.canonicalize().unwrap(),
                contents.iter().map(|bytes| bytes.to_vec()).collect(),
            );
    }
}

#[cfg(test)]
#[path = "reload_tests.rs"]
mod tests;
