use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Bot {
    #[serde(default)]
    pub require_start: bool,
    pub owner: String,
    pub credential: String,
    #[serde(default)]
    pub username: String,
    #[serde(default = "yes")]
    pub enabled: bool,
    #[serde(default)]
    pub menus: Vec<String>,
}
fn yes() -> bool {
    true
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Settings {
    pub bots: BTreeMap<String, Bot>,
    #[serde(default)]
    pub routes: BTreeMap<String, String>,
    #[serde(default)]
    pub workspaces: BTreeMap<String, Workspace>,
    #[serde(default)]
    pub public_origin: String,
    #[serde(default)]
    pub legacy_workspaces: BTreeMap<String, String>,
    #[serde(default)]
    pub groups: BTreeMap<String, ResourceGroup>,
    #[serde(default)]
    pub runtime_users: BTreeMap<String, RuntimeUser>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RuntimeUser {
    pub uid: u32,
    pub gid: u32,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ResourceGroup {
    #[serde(default = "ordinary_limit")]
    pub processes: u32,
    #[serde(default = "model_limit")]
    pub models: u32,
}
fn ordinary_limit() -> u32 {
    4
}
fn model_limit() -> u32 {
    2
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Workspace {
    pub root: String,
    #[serde(default)]
    pub env: String,
}
#[derive(Clone)]
pub struct Config {
    pub home: PathBuf,
    pub offline: bool,
}
impl Config {
    pub fn new(home: PathBuf) -> Self {
        Self {
            home,
            offline: false,
        }
    }
    pub fn settings(&self) -> Result<Settings> {
        let s: Settings = serde_json::from_slice(&fs::read(self.home.join("config/bots.json"))?)?;
        for (id, bot) in &s.bots {
            ensure!(
                crate::identifier(id)
                    && !bot.owner.is_empty()
                    && bot.owner.bytes().all(|c| c.is_ascii_digit()),
                "invalid bot identity"
            );
            ensure!(
                crate::identifier(&bot.credential),
                "invalid credential reference"
            );
        }
        for target in s.routes.values() {
            ensure!(s.bots.contains_key(target), "unknown route bot");
        }
        let mut runtime_ids = BTreeSet::new();
        for (id, user) in &s.runtime_users {
            ensure!(
                crate::identifier(id)
                    && user.uid > 0
                    && user.gid > 0
                    && runtime_ids.insert(user.uid),
                "runtime users must have distinct unprivileged identities"
            )
        }
        for (name, group) in &s.groups {
            ensure!(
                crate::identifier(name)
                    && group.processes > 0
                    && group.models > 0
                    && group.processes <= 32
                    && group.models <= 16,
                "invalid resource quota"
            )
        }
        for (key, workspace) in &s.workspaces {
            ensure!(crate::identifier(key), "invalid workspace");
            self.contained(&workspace.root)?;
            if !workspace.env.is_empty() {
                self.contained(&workspace.env)?;
            }
        }
        if !s.public_origin.is_empty() {
            let u = reqwest::Url::parse(&s.public_origin)?;
            ensure!(
                u.scheme() == "https"
                    && u.username().is_empty()
                    && u.password().is_none()
                    && u.query().is_none()
                    && u.fragment().is_none()
                    && u.path() == "/",
                "invalid public origin"
            );
        }
        Ok(s)
    }
    pub fn validate_credentials(&self, s: &Settings) -> Result<()> {
        let mut seen = BTreeSet::new();
        for bot in s.bots.values().filter(|b| b.enabled) {
            ensure!(
                seen.insert(crate::digest(self.token(bot)?)),
                "duplicate bot credential"
            );
        }
        Ok(())
    }
    pub fn contained(&self, relative: &str) -> Result<PathBuf> {
        let p = Path::new(relative);
        ensure!(
            !p.is_absolute()
                && !relative.is_empty()
                && p.components()
                    .all(|c| matches!(c, std::path::Component::Normal(_))),
            "unsafe relative path"
        );
        let result = self.home.join(p);
        if result.exists() {
            ensure!(
                result
                    .canonicalize()?
                    .starts_with(self.home.canonicalize()?),
                "path escapes home"
            );
        }
        Ok(result)
    }
    pub fn token(&self, bot: &Bot) -> Result<String> {
        let p = self.home.join("gateway/secrets").join(&bot.credential);
        let metadata = fs::symlink_metadata(&p).context("credential unavailable")?;
        ensure!(
            metadata.is_file() && metadata.permissions().mode() & 0o077 == 0,
            "unsafe credential permissions"
        );
        let token = fs::read_to_string(p)?.trim().to_owned();
        ensure!(
            token
                .split_once(':')
                .is_some_and(|(a, b)| a.bytes().all(|c| c.is_ascii_digit())
                    && !a.is_empty()
                    && b.len() >= 20
                    && b.bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))),
            "invalid credential format"
        );
        Ok(token)
    }
    pub fn db(&self) -> PathBuf {
        self.home.join("gateway/state.sqlite3")
    }
    pub fn socket(&self) -> PathBuf {
        self.home.join("run/gateway.sock")
    }
    pub fn python(&self) -> PathBuf {
        self.home.join("venv/bin/python")
    }
    pub fn resolve_bot(&self, topic: &str, fallback: &str) -> Result<String> {
        let s = self.settings()?;
        let target = s.routes.get(topic).map(String::as_str).unwrap_or(fallback);
        ensure!(s.bots.contains_key(target), "unknown bot");
        Ok(target.to_string())
    }
}
pub fn read_env(path: &Path) -> Result<BTreeMap<String, String>> {
    if !path.exists() {
        return Ok(BTreeMap::new());
    }
    let mut values = BTreeMap::new();
    for raw in fs::read_to_string(path)?.lines() {
        let raw = raw.trim().strip_prefix("export ").unwrap_or(raw.trim());
        if raw.starts_with('#') {
            continue;
        }
        if let Some((k, v)) = raw.split_once('=')
            && k.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_')
        {
            values.insert(k.to_owned(), v.trim().trim_matches(['\'', '"']).to_owned());
        }
    }
    Ok(values)
}
pub fn atomic_json(path: &Path, value: &impl Serialize, mode: u32) -> Result<()> {
    let parent = path.parent().context("missing parent")?;
    fs::create_dir_all(parent)?;
    let tmp = parent.join(format!(".{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| -> Result<()> {
        use std::io::Write;
        let mut f = fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&tmp)?;
        fs::set_permissions(&tmp, fs::Permissions::from_mode(mode))?;
        f.write_all(&serde_json::to_vec_pretty(value)?)?;
        f.sync_all()?;
        fs::rename(&tmp, path)?;
        fs::File::open(parent)?.sync_all()?;
        Ok(())
    })();
    if tmp.exists() {
        fs::remove_file(tmp)?;
    }
    result
}
pub fn credential_input() -> Result<String> {
    use std::io::Read;
    let mut input = String::new();
    std::io::stdin().take(4096).read_to_string(&mut input)?;
    if input.trim().is_empty() {
        bail!("credential required on stdin")
    }
    Ok(input.trim().to_owned())
}
