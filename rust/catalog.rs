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
    pub web: BTreeMap<String, Web>,
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
    pub preview: Vec<String>,
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
    pub shared: bool,
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
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Web {
    pub service: String,
    pub upstream: String,
    #[serde(default)]
    pub title: String,
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
            let path = base.join(relative).canonicalize()?;
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
        let catalog = Self::load_from(config.home.join("modules"))?;
        catalog.validate_settings(settings)?;
        Ok(catalog)
    }
    pub fn validate_settings(&self, settings: &crate::config::Settings) -> Result<()> {
        for module in self.modules.values() {
            let manifest = &module.manifest;
            ensure!(
                settings.workspaces.contains_key(&manifest.workspace),
                "module workspace is not configured"
            );
            for job in manifest.jobs.values() {
                ensure!(
                    settings.workspaces.contains_key(&job.workspace),
                    "task workspace is not configured"
                );
                ensure!(
                    settings.routes.contains_key(&job.topic),
                    "task topic is not configured"
                );
            }
            for service in manifest.services.values() {
                ensure!(
                    settings.workspaces.contains_key(&service.workspace),
                    "service workspace is not configured"
                );
                ensure!(
                    settings.routes.contains_key(&service.topic),
                    "service topic is not configured"
                );
            }
            for topic in manifest
                .commands
                .iter()
                .map(|c| &c.topic)
                .chain(manifest.notifications.values())
            {
                ensure!(
                    settings.routes.contains_key(topic),
                    "notification topic is not configured"
                );
            }
        }
        Ok(())
    }
    pub fn load_from(base: PathBuf) -> Result<Self> {
        Self::load_index(base, None)
    }
    pub fn load_index(base: PathBuf, supplied: Option<Value>) -> Result<Self> {
        let base = base.canonicalize()?;
        let roots = module_roots(&base, supplied)?;
        let mut catalog = Self::default();
        let mut commands = BTreeSet::from(["help".to_string(), "start".to_string()]);
        let mut jobs = BTreeSet::new();
        let mut callbacks: Vec<String> = Vec::new();
        let mut web_paths = BTreeSet::new();
        let mut hashes = Vec::new();
        for (id, root) in roots {
            let bytes = fs::read(root.join("module.json"))?;
            let manifest: Manifest = serde_json::from_slice(&bytes)?;
            ensure!(
                manifest.id == id && crate::identifier(&id) && manifest.api_version == 2,
                "invalid module protocol or identity"
            );
            ensure!(
                root.starts_with(&base)
                    && valid_entry(&manifest.handler)
                    && (manifest.lifecycle.is_empty() || valid_entry(&manifest.lifecycle)),
                "invalid module entrypoint"
            );
            for command in &manifest.commands {
                ensure!(
                    crate::identifier(&command.name)
                        && command.name.len() <= 32
                        && !command.name.contains('-')
                        && commands.insert(command.name.clone()),
                    "duplicate or invalid command"
                );
                ensure!(
                    !command.description.contains('\n')
                        && command.description.chars().count() <= 256,
                    "invalid command description"
                );
            }
            for prefix in &manifest.callbacks {
                ensure!(
                    prefix.ends_with('|')
                        && crate::identifier(prefix.trim_end_matches('|'))
                        && !callbacks
                            .iter()
                            .any(|p| p.starts_with(prefix) || prefix.starts_with(p)),
                    "duplicate callback"
                );
                callbacks.push(prefix.clone());
            }
            for (id, job) in &manifest.jobs {
                ensure!(
                    crate::identifier(id) && jobs.insert(id.clone()) && valid_entry(&job.entry),
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
            for (path, web) in &manifest.web {
                ensure!(
                    crate::identifier(path)
                        && web_paths.insert(path.clone())
                        && manifest.services.contains_key(&web.service),
                    "invalid web registration"
                );
                let url = reqwest::Url::parse(&web.upstream)?;
                ensure!(
                    url.scheme() == "http"
                        && url.host_str() == Some("127.0.0.1")
                        && url.port().is_some()
                        && url.path() == "/"
                        && url.query().is_none()
                        && url.fragment().is_none()
                        && url.username().is_empty()
                        && url.password().is_none(),
                    "web service must use loopback"
                );
            }
            let hash = crate::modules::content_hash(&root)?;
            hashes.push(format!("{id}:{}:{hash}", root.display()));
            catalog.modules.insert(
                id,
                Module {
                    root,
                    manifest,
                    hash,
                },
            );
        }
        for module in catalog.modules.values() {
            for required in &module.manifest.requires {
                ensure!(
                    catalog.modules.contains_key(required),
                    "missing module dependency"
                );
            }
            let mut visiting = BTreeSet::new();
            catalog.dependencies(&module.manifest.id, &mut visiting)?;
        }
        catalog.hash = crate::digest(hashes.join("\n"));
        Ok(catalog)
    }
    fn dependencies(&self, id: &str, seen: &mut BTreeSet<String>) -> Result<()> {
        ensure!(seen.insert(id.to_string()), "cyclic module dependency");
        for item in &self.modules[id].manifest.requires {
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
