use crate::config::Config;
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::PathBuf,
};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Manifest {
    pub api_version: u32,
    #[serde(default = "data_version")]
    pub data_version: u32,
    pub id: String,
    pub handler: Vec<String>,
    #[serde(default)]
    pub requires: Vec<String>,
    #[serde(default)]
    pub commands: Vec<Command>,
    #[serde(default)]
    pub callbacks: Vec<String>,
    #[serde(default)]
    pub hooks: Vec<String>,
    #[serde(default)]
    pub jobs: BTreeMap<String, Job>,
    #[serde(default)]
    pub services: BTreeMap<String, Service>,
    #[serde(default)]
    pub documents: Vec<Value>,
    #[serde(default)]
    pub capabilities: Vec<String>,
    #[serde(default)]
    pub notifications: BTreeMap<String, String>,
    #[serde(default)]
    pub deduplication: BTreeMap<String, Value>,
    #[serde(default)]
    pub workspace: String,
    #[serde(default)]
    pub check: Vec<Vec<String>>,
    #[serde(default)]
    pub lifecycle: Vec<String>,
    #[serde(default)]
    pub cli: BTreeMap<String, Vec<String>>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Command {
    pub name: String,
    pub description: String,
    #[serde(default)]
    pub action: String,
    #[serde(default)]
    pub argument: String,
    #[serde(default)]
    pub detail: String,
    #[serde(default)]
    pub example: String,
    #[serde(default)]
    pub topic: String,
    #[serde(default)]
    pub menu: bool,
    #[serde(default)]
    pub order: i64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Job {
    pub name: String,
    pub entry: Vec<String>,
    pub workspace: String,
    pub topic: String,
    pub schedule: String,
    #[serde(default)]
    pub lock: String,
    #[serde(default)]
    pub timeout: Option<u64>,
    #[serde(default)]
    pub deliver: bool,
    #[serde(default)]
    pub monotonic: bool,
    #[serde(default)]
    pub jitter: bool,
    #[serde(default)]
    pub model: bool,
    #[serde(default)]
    pub completion_hook: String,
    #[serde(default = "yes")]
    pub failure_notify: bool,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
}
fn yes() -> bool {
    true
}
fn data_version() -> u32 {
    1
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Service {
    #[serde(default)]
    pub passive: bool,
    pub entry: Vec<String>,
    pub workspace: String,
    #[serde(default)]
    pub topic: String,
    #[serde(default)]
    pub legacy_unit: String,
}
#[derive(Clone, Debug)]
pub struct Module {
    pub root: PathBuf,
    pub manifest: Manifest,
    pub hash: String,
}
#[derive(Clone, Default)]
pub struct Catalog {
    pub modules: BTreeMap<String, Module>,
    pub hash: String,
    /// Modules excluded by validation, with a program-owned reason. One bad
    /// module never rejects the rest of the catalog.
    pub rejected: BTreeMap<String, String>,
    /// Topics of loaded modules served only through the fallback route.
    pub fallback_topics: BTreeSet<String>,
    pub fallback_bot: Option<String>,
}

/// Public reason for a rejected module: never a serde/IO detail or a path.
fn rejection_reason(error: &anyhow::Error) -> String {
    if error.chain().any(|c| c.is::<serde_json::Error>()) {
        return "invalid module manifest".into();
    }
    if error.chain().any(|c| c.is::<std::io::Error>()) {
        return "module files unreadable".into();
    }
    let text = error.root_cause().to_string();
    if text.len() > 120 || text.contains('/') {
        "invalid module".into()
    } else {
        text
    }
}

#[derive(Default)]
struct Taken {
    commands: BTreeSet<String>,
    callbacks: Vec<String>,
    jobs: BTreeSet<String>,
}

pub fn valid_entry(entry: &[String]) -> bool {
    !entry.is_empty()
        && matches!(entry[0].as_str(), "python" | "node" | "bash" | "native")
        && entry.len() >= 2
        && entry.iter().all(|s| !s.contains('\0'))
        && (entry[0] != "native"
            || (!entry[1].starts_with('/') && !entry[1].split('/').any(|s| s == "..")))
}
pub(crate) fn module_roots(
    base: &std::path::Path,
    supplied: Option<Value>,
) -> Result<BTreeMap<String, PathBuf>> {
    let mut roots = BTreeMap::new();
    let index = base.join("registry.json");
    if supplied.is_some() || index.exists() {
        let data: Value = if let Some(value) = supplied {
            value
        } else {
            serde_json::from_slice(&fs::read(index)?)?
        };
        ensure!(data["api_version"] == 2, "unsupported registry version");
        for (id, relative) in data["modules"].as_object().context("invalid registry")? {
            let relative = relative.as_str().context("invalid module path")?;
            ensure!(
                std::path::Path::new(relative)
                    .components()
                    .all(|c| matches!(c, std::path::Component::Normal(_))),
                "module escapes registry"
            );
            let joined = base.join(relative);
            // A missing module directory rejects only that module (its
            // manifest read fails later); an escaping path rejects the registry.
            let path = joined.canonicalize().unwrap_or(joined);
            ensure!(path.starts_with(base), "module escapes registry");
            roots.insert(id.clone(), path);
        }
    } else {
        for item in fs::read_dir(base)? {
            let p = item?.path();
            if p.join("module.json").is_file() {
                roots.insert(
                    p.file_name().unwrap().to_string_lossy().into_owned(),
                    p.canonicalize()?,
                );
            }
        }
    }
    Ok(roots)
}

impl Catalog {
    pub fn load(config: &Config) -> Result<Self> {
        Self::load_with_settings(config, &config.settings()?)
    }
    pub(crate) fn load_with_settings(
        config: &Config,
        settings: &crate::config::Settings,
    ) -> Result<Self> {
        let mut catalog = Self::load_from(config.home.join("modules"))?;
        catalog.apply_settings(settings);
        Ok(catalog)
    }
    fn module_settings(manifest: &Manifest, settings: &crate::config::Settings) -> Result<()> {
        let routed = |topic: &str| settings.route_base(topic).is_some();
        ensure!(
            settings.workspaces.contains_key(&manifest.workspace),
            "module workspace is not configured"
        );
        for job in manifest.jobs.values() {
            ensure!(
                settings.workspaces.contains_key(&job.workspace),
                "task workspace is not configured"
            );
            ensure!(routed(&job.topic), "task topic is not configured");
        }
        for service in manifest.services.values() {
            ensure!(
                settings.workspaces.contains_key(&service.workspace),
                "service workspace is not configured"
            );
            ensure!(routed(&service.topic), "service topic is not configured");
        }
        for topic in manifest
            .commands
            .iter()
            .map(|c| &c.topic)
            .chain(manifest.notifications.values())
        {
            ensure!(routed(topic), "notification topic is not configured");
        }
        Ok(())
    }
    /// Every topic a manifest routes (jobs, services, commands, notifications).
    pub fn topics(manifest: &Manifest) -> impl Iterator<Item = &String> {
        manifest
            .jobs
            .values()
            .map(|j| &j.topic)
            .chain(manifest.services.values().map(|s| &s.topic))
            .chain(manifest.commands.iter().map(|c| &c.topic))
            .chain(manifest.notifications.values())
    }
    /// Reject only the modules whose workspaces or topics the settings cannot
    /// serve (plus modules depending on them) and record fallback topics.
    pub fn apply_settings(&mut self, settings: &crate::config::Settings) {
        let invalid = self
            .modules
            .iter()
            .filter_map(|(id, m)| {
                Self::module_settings(&m.manifest, settings)
                    .err()
                    .map(|e| (id.clone(), rejection_reason(&e)))
            })
            .collect::<Vec<_>>();
        for (id, reason) in invalid {
            self.modules.remove(&id);
            self.rejected.insert(id, reason);
        }
        self.reject_dependents();
        self.fallback_bot = settings.fallback_bot().map(str::to_owned);
        self.fallback_topics = self
            .modules
            .values()
            .flat_map(|m| Self::topics(&m.manifest))
            .filter(|topic| settings.routes_by_fallback(topic))
            .cloned()
            .collect();
    }
    /// Strict form for install, rollback and checks: fail on the first module
    /// rejected now that `baseline` (the current catalog) did not reject.
    pub fn ensure_accepted(&self, baseline: Option<&Catalog>) -> Result<()> {
        for (id, reason) in &self.rejected {
            if baseline.is_none_or(|b| !b.rejected.contains_key(id)) {
                anyhow::bail!("{reason}");
            }
        }
        Ok(())
    }
    pub fn validate_settings(&self, settings: &crate::config::Settings) -> Result<()> {
        for module in self.modules.values() {
            Self::module_settings(&module.manifest, settings)?;
        }
        Ok(())
    }
    /// Log module rejections and fallback-routed topics when they change.
    pub fn warn_changes(previous: Option<&Catalog>, next: &Catalog) {
        for (id, reason) in &next.rejected {
            if previous.is_none_or(|p| p.rejected.get(id) != Some(reason)) {
                crate::warn(
                    "module-rejected",
                    serde_json::json!({"module":id,"reason":reason}),
                );
            }
        }
        if previous.is_none_or(|p| {
            p.fallback_topics != next.fallback_topics || p.fallback_bot != next.fallback_bot
        }) && !next.fallback_topics.is_empty()
        {
            crate::warn(
                "route-fallback",
                serde_json::json!({"bot":next.fallback_bot,"topics":next.fallback_topics}),
            );
        }
    }
    pub fn load_from(base: PathBuf) -> Result<Self> {
        Self::load_index(base, None)
    }
    fn load_module(
        base: &std::path::Path,
        id: &str,
        root: &std::path::Path,
        taken: &mut Taken,
    ) -> Result<(Manifest, String)> {
        let bytes = fs::read(root.join("module.json"))?;
        let manifest: Manifest = serde_json::from_slice(&bytes)?;
        ensure!(
            manifest.id == id && crate::identifier(id) && manifest.api_version == 2,
            "invalid module protocol or identity"
        );
        ensure!(
            root.starts_with(base)
                && valid_entry(&manifest.handler)
                && (manifest.lifecycle.is_empty() || valid_entry(&manifest.lifecycle)),
            "invalid module entrypoint"
        );
        // Claims are committed only when the whole module is valid.
        let mut commands = BTreeSet::new();
        for command in &manifest.commands {
            ensure!(
                crate::identifier(&command.name)
                    && command.name.len() <= 32
                    && !command.name.contains('-')
                    && !taken.commands.contains(&command.name)
                    && commands.insert(command.name.clone()),
                "duplicate or invalid command"
            );
            ensure!(
                !command.description.contains('\n') && command.description.chars().count() <= 256,
                "invalid command description"
            );
        }
        let mut callbacks: Vec<String> = Vec::new();
        for prefix in &manifest.callbacks {
            ensure!(
                prefix.ends_with('|')
                    && crate::identifier(prefix.trim_end_matches('|'))
                    && !taken
                        .callbacks
                        .iter()
                        .chain(&callbacks)
                        .any(|p| p.starts_with(prefix) || prefix.starts_with(p)),
                "duplicate callback"
            );
            callbacks.push(prefix.clone());
        }
        let mut jobs = BTreeSet::new();
        for (id, job) in &manifest.jobs {
            ensure!(
                crate::identifier(id)
                    && !taken.jobs.contains(id)
                    && jobs.insert(id.clone())
                    && valid_entry(&job.entry),
                "duplicate or invalid job"
            );
            ensure!(
                job.lock.is_empty() || crate::identifier(&job.lock),
                "invalid lock"
            );
            ensure!(
                crate::identifier(&job.workspace)
                    && !job.topic.is_empty()
                    && job.timeout != Some(0),
                "invalid job workspace or timeout"
            );
            crate::modules::calendar(&job.schedule)?;
        }
        for (id, service) in &manifest.services {
            ensure!(
                crate::identifier(id)
                    && valid_entry(&service.entry)
                    && crate::identifier(&service.workspace),
                "invalid service"
            );
        }
        let hash = crate::modules::content_hash(root)?;
        taken.commands.extend(commands);
        taken.callbacks.extend(callbacks);
        taken.jobs.extend(jobs);
        Ok((manifest, hash))
    }
    pub fn load_index(base: PathBuf, supplied: Option<Value>) -> Result<Self> {
        let base = base.canonicalize()?;
        let roots = module_roots(&base, supplied)?;
        let mut catalog = Self::default();
        let mut taken = Taken {
            commands: BTreeSet::from(["help".to_string(), "start".to_string()]),
            ..Default::default()
        };
        for (id, root) in roots {
            match Self::load_module(&base, &id, &root, &mut taken) {
                Ok((manifest, hash)) => {
                    catalog.modules.insert(
                        id,
                        Module {
                            root,
                            manifest,
                            hash,
                        },
                    );
                }
                Err(error) => {
                    catalog.rejected.insert(id, rejection_reason(&error));
                }
            }
        }
        catalog.reject_dependents();
        Ok(catalog)
    }
    /// Rejected modules stay out of the hash: they are not served.
    fn rehash(&mut self) {
        let hashes = self
            .modules
            .iter()
            .map(|(id, m)| format!("{id}:{}:{}", m.root.display(), m.hash))
            .collect::<Vec<_>>();
        self.hash = crate::digest(hashes.join("\n"));
    }
    /// Reject modules whose dependencies are missing, rejected or cyclic.
    fn reject_dependents(&mut self) {
        loop {
            let bad = self.modules.keys().find_map(|id| {
                let mut seen = BTreeSet::new();
                let module = &self.modules[id];
                if module
                    .manifest
                    .requires
                    .iter()
                    .any(|r| !self.modules.contains_key(r))
                {
                    Some((id.clone(), "missing module dependency"))
                } else if self.dependencies(id, &mut seen).is_err() {
                    Some((id.clone(), "cyclic module dependency"))
                } else {
                    None
                }
            });
            let Some((id, reason)) = bad else { break };
            self.modules.remove(&id);
            self.rejected.insert(id, reason.into());
        }
        self.rehash();
    }
    fn dependencies(&self, id: &str, seen: &mut BTreeSet<String>) -> Result<()> {
        ensure!(seen.insert(id.to_string()), "cyclic module dependency");
        let Some(module) = self.modules.get(id) else {
            return Ok(());
        };
        for item in &module.manifest.requires {
            self.dependencies(item, seen)?;
        }
        seen.remove(id);
        Ok(())
    }
    pub fn job(&self, id: &str) -> Result<(&Module, &Job)> {
        self.modules
            .values()
            .find_map(|m| m.manifest.jobs.get(id).map(|j| (m, j)))
            .context("unknown job")
    }
    pub fn command(&self, id: &str) -> Option<(&Module, &Command)> {
        self.modules.values().find_map(|m| {
            m.manifest
                .commands
                .iter()
                .find(|c| c.name == id)
                .map(|c| (m, c))
        })
    }
}
#[cfg(test)]
#[path = "catalog_tests.rs"]
mod tests;
