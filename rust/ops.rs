//! Operator changes to bots, routes and deliveries.
//!
//! Every write goes through [`run`]: root (or owner of the home) only, one at
//! a time (`run/ops.lock`), refused during a deployment freeze. `--dry-run`
//! writes nothing and prints a masked configuration diff plus the database
//! plan. A real change first copies the current `config/bots.json`, every
//! affected credential and the database plan into
//! `backups/gateway-ops-<UTC>-<op>/`, then writes credentials, writes the
//! configuration atomically, forces a gateway reload and waits until the
//! gateway reports the new settings digest, applies the database plan in one
//! transaction and appends an audit line to `logs/ops.jsonl`. On failure it
//! prints `pylon ops restore <backup>`. Output never contains tokens, owner
//! chat ids or message bodies.
use crate::{
    config::{Config, Settings},
    db::{DiscardStep, OperatorPlan, RetargetStep, Selection, Store},
};
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Map, Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::Write,
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
};

/// Program-owned operator messages that are safe to print verbatim.
pub const MESSAGES: &[&str] = &[
    "operator changes require root",
    "deployment freeze is active; operator changes are refused",
    "another operator change is in progress",
    "credential unavailable",
    "invalid credential format",
    "credential required on stdin",
    "credential prefix does not match the bot",
    "credential unchanged",
    "credential file already exists",
    "invalid bot identity",
    "bot already registered",
    "unknown bot",
    "unknown topic",
    "invalid topic",
    "invalid duration",
    "invalid time",
    "target bot is disabled",
    "target bot delivers to a different chat",
    "bot is still routed; pass --reroute-to",
    "reroute bot must differ from the bot",
    "bot has uncertain deliveries; resolve them first",
    "bot has pending deliveries; pass --pending retarget or --pending discard",
    "pending deliveries need --to",
    "--to is only used with --pending retarget",
    "bot already serves the topic",
    "override needs --for, --until or --revert-when-healthy",
    "use either --for or --until",
    "no route override for topic",
    "bot is not deferred",
    "pass --discard to suppress pending deliveries",
    "select bots or pass --all",
    "telegram rejected the credential",
    "telegram returned a different bot",
    "telegram returned a different username",
    "skipping getMe needs --reason",
    "gateway did not apply the change",
    "gateway rejected the configuration",
    "bot did not become healthy",
    "menu was not confirmed",
    "operator change failed; run the printed restore command",
    "invalid backup directory",
    "backup directory exists; retry",
    "source bot is still configured",
    "only per-bot state can be pruned",
    "only pending deliveries can be discarded",
    "route override bot must exist and be enabled",
    "route override needs a base route",
    "route override bot has a different owner",
    "invalid route override",
    "invalid delivery policy",
    "paused bot must exist and be enabled",
    "unknown route bot",
    "notification topic is not configured",
    "service topic is not configured",
    "task topic is not configured",
    "task workspace is not configured",
    "module workspace is not configured",
    "service workspace is not configured",
    "invalid module manifest",
    "module files unreadable",
    "invalid module",
    "invalid module protocol or identity",
    "invalid module entrypoint",
    "invalid command description",
    "duplicate callback",
    "duplicate or invalid job",
    "invalid lock",
    "invalid job workspace or timeout",
    "invalid service",
    "missing module dependency",
    "cyclic module dependency",
    "module stop preparation timed out after 10 minutes; tasks still active",
    "workspace registration requires root",
];

pub fn is_root() -> bool {
    unsafe { libc::geteuid() == 0 }
}
pub fn actor() -> String {
    format!("uid:{}", unsafe { libc::geteuid() })
}

/// Current state an operation plans against.
pub struct Ctx<'a> {
    pub config: &'a Config,
    pub raw: Value,
    pub settings: Settings,
    pub store: Option<Store>,
}
impl Ctx<'_> {
    fn bot(&self, id: &str) -> Result<&crate::config::Bot> {
        self.settings.bots.get(id).context("unknown bot")
    }
    fn count(&self, bot: &str, status: &str) -> Result<usize> {
        Ok(match &self.store {
            Some(store) => store.deliveries_for_bot(bot, status)?.len(),
            None => 0,
        })
    }
}

/// What an operation changes. `raw` is the complete new `bots.json` value.
#[derive(Default)]
pub struct Change {
    pub raw: Option<Value>,
    /// Credential file name to new token, or `None` to move it into the backup.
    pub secrets: BTreeMap<String, Option<String>>,
    pub db: OperatorPlan,
    /// Metadata shown in the preview and the result (no secrets).
    pub notes: Map<String, Value>,
    pub wait_healthy: Vec<String>,
    pub wait_menu: Vec<String>,
}
impl Change {
    fn note(&mut self, key: &str, value: Value) {
        self.notes.insert(key.into(), value);
    }
}

fn lock(config: &Config) -> Result<fs::File> {
    let path = config.home.join("run/ops.lock");
    fs::create_dir_all(path.parent().context("run directory missing")?)?;
    let file = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&path)?;
    use std::os::fd::AsRawFd;
    ensure!(
        unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0,
        "another operator change is in progress"
    );
    Ok(file)
}

/// Production homes are root-owned; tests run in a home the caller owns.
fn may_operate(config: &Config) -> Result<()> {
    let owner = fs::metadata(&config.home)?.uid();
    let uid = unsafe { libc::geteuid() };
    ensure!(
        uid == 0 || config.offline || owner == uid && owner != 0,
        "operator changes require root"
    );
    Ok(())
}

pub async fn run(
    config: &Config,
    op: &str,
    args: Value,
    dry_run: bool,
    plan: impl FnOnce(&Ctx) -> Result<Change>,
) -> Result<Value> {
    may_operate(config)?;
    ensure!(
        !config.home.join("run/freeze").exists(),
        "deployment freeze is active; operator changes are refused"
    );
    let _lock = lock(config)?;
    let path = config.home.join("config/bots.json");
    let old_bytes = fs::read(&path)?;
    let raw: Value = serde_json::from_slice(&old_bytes)?;
    let settings = config.settings_from_bytes(&old_bytes)?;
    let store = if config.db().is_file() {
        Some(Store::new(&config.db())?)
    } else {
        None
    };
    let ctx = Ctx {
        config,
        raw: raw.clone(),
        settings: settings.clone(),
        store,
    };
    let mut change = plan(&ctx)?;
    let new_raw = change.raw.clone().unwrap_or_else(|| raw.clone());
    let config_changed = new_raw != raw;
    let new_bytes = if config_changed {
        serde_json::to_vec_pretty(&new_raw)?
    } else {
        old_bytes.clone()
    };
    let new_settings = config.settings_from_bytes(&new_bytes)?;
    check_credentials(config, &new_settings, &change.secrets)?;
    if config.home.join("modules").is_dir()
        && let Ok(current) = crate::catalog::Catalog::load_with_settings(config, &settings)
    {
        // A change may not reject a module the current configuration accepts.
        let next = crate::catalog::Catalog::load_with_settings(config, &new_settings)?;
        next.ensure_accepted(Some(&current))?;
    }
    let deliveries = plan_deliveries(&ctx, &change.db)?;
    if !change.db.is_empty()
        && let Some(store) = &ctx.store
    {
        let (plan, live, owners) = db_inputs(&ctx, &change.db, &new_settings);
        store.check_plan(&plan, &live, &owners, &actor())?;
    }
    let changes = config_changed || !change.secrets.is_empty() || !change.db.is_empty();
    let mut preview = json!({
        "op": op,
        "args": mask(&args),
        "dry_run": dry_run,
        "changed": changes,
        "config_diff": masked_diff(&raw, &new_raw),
        "credentials": {
            "write": change.secrets.values().filter(|v| v.is_some()).count(),
            "move_to_backup": change.secrets.values().filter(|v| v.is_none()).count(),
        },
        "database": plan_summary(&change.db, &deliveries),
    });
    for (key, value) in std::mem::take(&mut change.notes) {
        preview[key] = value;
    }
    if dry_run || !changes {
        preview["ok"] = json!(true);
        preview["backup"] = Value::Null;
        return Ok(preview);
    }
    let backup = backup(config, op, &old_bytes, &change, &deliveries)?;
    preview["backup"] = json!(backup.display().to_string());
    let started = crate::now();
    let outcome = apply(
        config,
        &ctx,
        &change,
        config_changed.then_some(&new_bytes),
        &new_settings,
        started,
    )
    .await;
    let ok = outcome.is_ok();
    match &outcome {
        Ok(result) => {
            for (key, value) in result.as_object().into_iter().flatten() {
                preview[key] = value.clone();
            }
        }
        Err(error) => {
            preview["error"] = json!(crate::errors::cli_message(error));
            preview["restore"] = json!(format!("pylon ops restore {}", backup.display()));
        }
    }
    preview["ok"] = json!(ok);
    audit(config, op, &preview);
    if let Err(error) = outcome {
        println!("{}", serde_json::to_string(&preview)?);
        return Err(error.context(OpsFailed));
    }
    Ok(preview)
}

/// The database plan with target owners filled in, the bots that stay live,
/// and every owner known before or after the change.
fn db_inputs(
    ctx: &Ctx<'_>,
    plan: &OperatorPlan,
    new_settings: &Settings,
) -> (OperatorPlan, Vec<String>, BTreeMap<String, String>) {
    let live = new_settings
        .bots
        .iter()
        .filter(|(_, b)| b.enabled)
        .map(|(id, _)| id.clone())
        .collect::<Vec<_>>();
    let mut owners = ctx
        .settings
        .bots
        .iter()
        .map(|(id, b)| (id.clone(), b.owner.clone()))
        .collect::<BTreeMap<_, _>>();
    owners.extend(
        new_settings
            .bots
            .iter()
            .map(|(id, b)| (id.clone(), b.owner.clone())),
    );
    let mut plan = plan.clone();
    for step in &mut plan.retarget {
        step.to_owner = new_settings
            .bots
            .get(&step.to)
            .map(|b| b.owner.clone())
            .unwrap_or_default();
    }
    (plan, live, owners)
}

#[derive(Debug)]
struct OpsFailed;
impl std::fmt::Display for OpsFailed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("operator change failed; run the printed restore command")
    }
}
impl std::error::Error for OpsFailed {}

async fn apply(
    config: &Config,
    ctx: &Ctx<'_>,
    change: &Change,
    new_bytes: Option<&Vec<u8>>,
    new_settings: &Settings,
    started: f64,
) -> Result<Value> {
    let mut result = json!({});
    for (credential, token) in &change.secrets {
        match token {
            Some(token) => write_secret(config, credential, token)?,
            None => {
                let path = secret_path(config, credential)?;
                if path.exists() {
                    fs::remove_file(&path)?;
                    fs::File::open(path.parent().context("credential parent missing")?)?
                        .sync_all()?;
                }
            }
        }
    }
    if let Some(bytes) = new_bytes {
        write_config(config, bytes)?;
    }
    if new_bytes.is_some() || !change.secrets.is_empty() {
        result["reload"] = json!(reload(config, new_settings).await?);
    }
    if !change.db.is_empty() {
        let store = ctx.store.as_ref().context("gateway state missing")?;
        let (plan, live, owners) = db_inputs(ctx, &change.db, new_settings);
        result["database"] = store.apply_plan(&plan, &live, &owners, &actor())?;
    }
    if !change.wait_healthy.is_empty() {
        let bots = change.wait_healthy.clone();
        let healthy = wait_diagnostics(config, 60, |d| {
            bots.iter().all(|id| {
                bot_row(d, id).is_some_and(|b| {
                    b["connection"]["status"] == "ok"
                        && b["connection"]["at"]
                            .as_f64()
                            .is_some_and(|at| at >= started)
                })
            })
        })
        .await?;
        ensure!(healthy, "bot did not become healthy");
        result["healthy"] = json!(bots);
    }
    if !change.wait_menu.is_empty() {
        let bots = change.wait_menu.clone();
        let done = wait_diagnostics(config, 75, |d| {
            bots.iter().all(|id| {
                bot_row(d, id).is_some_and(|b| {
                    b["menu"]["status"] == "ok"
                        && b["menu"]["at"].as_f64().is_some_and(|at| at >= started)
                })
            })
        })
        .await?;
        ensure!(done, "menu was not confirmed");
        result["menu_confirmed"] = json!(bots);
    }
    Ok(result)
}

fn bot_row<'a>(diagnostics: &'a Value, id: &str) -> Option<&'a Value> {
    diagnostics["bots"]
        .as_array()?
        .iter()
        .find(|b| b["id"] == id)
}

async fn diagnostics(config: &Config) -> Result<Value> {
    crate::api::rpc(config, "diagnostics", json!({}), "").await
}

async fn wait_diagnostics(
    config: &Config,
    seconds: u64,
    done: impl Fn(&Value) -> bool,
) -> Result<bool> {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(seconds);
    loop {
        if let Ok(value) = diagnostics(config).await
            && done(&value)
        {
            return Ok(true);
        }
        if std::time::Instant::now() >= deadline {
            return Ok(false);
        }
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
}

/// Forces a reload and waits until the gateway serves exactly `settings`.
pub async fn reload(config: &Config, settings: &Settings) -> Result<&'static str> {
    if !config.socket().exists() {
        return Ok("gateway_unavailable");
    }
    let privileged = is_root() || config.offline;
    match crate::api::rpc(config, "admin.reload", json!({}), "").await {
        Ok(_) => {}
        Err(error) if error.to_string() == "gateway unavailable" => {
            return Ok("gateway_unavailable");
        }
        Err(_) if !privileged => return Ok("automatic"),
        Err(error) => return Err(error.context("gateway rejected the configuration")),
    }
    let digest = crate::config::settings_digest(settings);
    let applied = wait_diagnostics(config, 10, |d| d["settings_digest"] == digest).await?;
    ensure!(applied, "gateway did not apply the change");
    let d = diagnostics(config).await?;
    ensure!(
        d["configuration_rejected"].is_null(),
        "gateway rejected the configuration"
    );
    Ok("applied")
}

fn check_credentials(
    config: &Config,
    settings: &Settings,
    secrets: &BTreeMap<String, Option<String>>,
) -> Result<()> {
    let mut seen = BTreeSet::new();
    for bot in settings.bots.values().filter(|b| b.enabled) {
        let token = match secrets.get(&bot.credential) {
            Some(Some(token)) => token.clone(),
            Some(None) => bail!("credential unavailable"),
            None => config.token(bot)?,
        };
        ensure!(
            crate::config::valid_token(&token),
            "invalid credential format"
        );
        ensure!(
            seen.insert(crate::digest(&token)),
            "duplicate bot credential"
        );
    }
    Ok(())
}

/// Format and uniqueness checks for a new token before any network call.
pub fn preflight_token(config: &Config, token: &str, replacing: Option<&str>) -> Result<()> {
    ensure!(
        crate::config::valid_token(token),
        "invalid credential format"
    );
    let settings = config.settings()?;
    for (id, bot) in settings.bots.iter().filter(|(_, b)| b.enabled) {
        if Some(id.as_str()) == replacing {
            continue;
        }
        if let Ok(existing) = config.token(bot) {
            ensure!(existing != token, "duplicate bot credential");
        }
    }
    Ok(())
}

/// `getMe` with a token that is not installed yet.
pub async fn verify_token(config: &Config, token: &str, username: &str) -> Result<Value> {
    let family = config.settings()?.telegram_ip_family;
    let me = crate::telegram::Telegram::with_token(config, &family, token.to_owned())?
        .call("getMe", &json!({}), true)
        .await
        .map_err(|f| {
            anyhow::anyhow!("telegram rejected the credential").context(json!({
                "error_class": f.class, "http_status": f.http_status,
                "telegram_error_code": f.telegram_error_code}))
        })
        .map_err(|_| anyhow::anyhow!("telegram rejected the credential"))?;
    let prefix = token.split_once(':').map(|(a, _)| a).unwrap_or("");
    if !config.offline {
        ensure!(
            me["id"].as_u64().map(|v| v.to_string()).as_deref() == Some(prefix),
            "telegram returned a different bot"
        );
    }
    ensure!(
        username.is_empty() || me["username"] == username,
        "telegram returned a different username"
    );
    Ok(json!({"verified":true,"username":me["username"]}))
}

fn secret_path(config: &Config, credential: &str) -> Result<PathBuf> {
    ensure!(crate::identifier(credential), "invalid bot identity");
    Ok(config.home.join("gateway/secrets").join(credential))
}

fn write_secret(config: &Config, credential: &str, token: &str) -> Result<()> {
    let path = secret_path(config, credential)?;
    let parent = path.parent().context("credential parent missing")?;
    fs::create_dir_all(parent)?;
    let tmp = parent.join(format!(".{credential}.{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| -> Result<()> {
        let mut file = fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&tmp)?;
        file.write_all(token.as_bytes())?;
        file.sync_all()?;
        if is_root() {
            let meta = fs::metadata(parent)?;
            std::os::unix::fs::fchown(&file, Some(meta.uid()), Some(meta.gid()))?;
        }
        fs::rename(&tmp, &path)?;
        fs::File::open(parent)?.sync_all()?;
        Ok(())
    })();
    if tmp.exists() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

/// Replaces `config/bots.json` atomically, keeping its mode and ownership.
fn write_config(config: &Config, bytes: &[u8]) -> Result<()> {
    let path = config.home.join("config/bots.json");
    let meta = fs::metadata(&path)?;
    let parent = path.parent().context("missing parent")?;
    let tmp = parent.join(format!(".bots.json.{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| -> Result<()> {
        let mut file = fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(meta.mode() & 0o7777)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&tmp)?;
        fs::set_permissions(&tmp, fs::Permissions::from_mode(meta.mode() & 0o7777))?;
        file.write_all(bytes)?;
        file.sync_all()?;
        if is_root() {
            std::os::unix::fs::fchown(&file, Some(meta.uid()), Some(meta.gid()))?;
        }
        fs::rename(&tmp, &path)?;
        fs::File::open(parent)?.sync_all()?;
        Ok(())
    })();
    if tmp.exists() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

fn private_file(path: &Path, bytes: &[u8]) -> Result<()> {
    let mut file = fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    Ok(())
}

pub fn backup_name(op: &str, at: f64) -> String {
    let stamp = chrono::DateTime::from_timestamp(at as i64, 0)
        .map(|t| t.format("%Y%m%dT%H%M%SZ").to_string())
        .unwrap_or_else(|| "19700101T000000Z".into());
    format!("gateway-ops-{stamp}-{op}")
}

fn backup(
    config: &Config,
    op: &str,
    old_bytes: &[u8],
    change: &Change,
    deliveries: &[(String, String)],
) -> Result<PathBuf> {
    let root = config.home.join("backups");
    if !root.exists() {
        fs::create_dir_all(&root)?;
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700))?;
    }
    let dir = root.join(backup_name(op, crate::now()));
    match fs::create_dir(&dir) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
            bail!("backup directory exists; retry")
        }
        Err(e) => return Err(e.into()),
    }
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700))?;
    private_file(&dir.join("bots.json"), old_bytes)?;
    let secrets = dir.join("secrets");
    for credential in change.secrets.keys() {
        let source = secret_path(config, credential)?;
        if let Ok(bytes) = fs::read(&source) {
            if !secrets.exists() {
                fs::create_dir(&secrets)?;
                fs::set_permissions(&secrets, fs::Permissions::from_mode(0o700))?;
            }
            private_file(&secrets.join(credential), &bytes)?;
        }
    }
    let plan = json!({
        "op": op,
        "at": crate::now(),
        "version": env!("CARGO_PKG_VERSION"),
        "plan": change.db,
        "deliveries": deliveries.iter().map(|(id, bot)| json!({"id":id,"profile":bot})).collect::<Vec<_>>(),
    });
    private_file(
        &dir.join("db-plan.json"),
        &serde_json::to_vec_pretty(&plan)?,
    )?;
    fs::File::open(&dir)?.sync_all()?;
    Ok(dir)
}

/// Deliveries the plan touches now, as (id, current bot).
fn plan_deliveries(ctx: &Ctx, plan: &OperatorPlan) -> Result<Vec<(String, String)>> {
    let Some(store) = &ctx.store else {
        return Ok(vec![]);
    };
    let mut rows = Vec::new();
    let selections = plan
        .retarget
        .iter()
        .map(|s| &s.select)
        .chain(plan.discard.iter().map(|s| &s.select));
    for selection in selections {
        for id in &selection.ids {
            let event = store.event(id)?;
            rows.push((
                id.clone(),
                event["profile"].as_str().unwrap_or("").to_owned(),
            ));
        }
        if let Some(bot) = &selection.from_bot {
            for id in store.deliveries_for_bot(bot, "pending")? {
                rows.push((id, bot.clone()));
            }
        }
    }
    rows.sort();
    rows.dedup();
    Ok(rows)
}

fn plan_summary(plan: &OperatorPlan, deliveries: &[(String, String)]) -> Value {
    let count = |s: &Selection| {
        deliveries
            .iter()
            .filter(|(id, bot)| s.ids.contains(id) || s.from_bot.as_ref() == Some(bot))
            .count()
    };
    json!({
        "retarget": plan.retarget.iter().map(|s| json!({"from_bot":s.select.from_bot,"ids":s.select.ids,"to":s.to,"count":count(&s.select)})).collect::<Vec<_>>(),
        "discard": plan.discard.iter().map(|s| json!({"from_bot":s.select.from_bot,"ids":s.select.ids,"reason":s.reason,"count":count(&s.select)})).collect::<Vec<_>>(),
        "delete_kv": plan.delete_kv,
        "delete_updates_for": plan.delete_updates_for,
    })
}

fn audit(config: &Config, op: &str, result: &Value) {
    let line = json!({"event":"operator-change","op":op,"at":crate::now(),"actor":actor(),
        "ok":result["ok"],"args":result["args"],"backup":result["backup"],
        "database":result["database"],"reload":result["reload"],"error":result["error"]});
    let path = config.home.join("logs/ops.jsonl");
    let written = (|| -> Result<()> {
        fs::create_dir_all(path.parent().context("logs directory missing")?)?;
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&path)?;
        writeln!(file, "{line}")?;
        file.sync_all()?;
        Ok(())
    })();
    if written.is_err() {
        eprintln!("{}", json!({"event":"operator-audit-failed","op":op}));
    }
    if is_root() && !config.offline {
        let _ = std::process::Command::new("logger")
            .args(["-t", "pylon-ops"])
            .arg(line.to_string())
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
    }
}

const MASKED_KEYS: &[&str] = &["owner", "credential", "chat", "chat_id", "token"];

/// Replaces owner chat ids, credential names and tokens with `***`.
pub fn mask(value: &Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(k, v)| {
                    let masked = if MASKED_KEYS.contains(&k.as_str()) && !v.is_null() {
                        json!("***")
                    } else {
                        mask(v)
                    };
                    (k.clone(), masked)
                })
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.iter().map(mask).collect()),
        other => other.clone(),
    }
}

/// Line diff of the masked configurations with three lines of context.
pub fn masked_diff(old: &Value, new: &Value) -> Vec<String> {
    let render = |v: &Value| {
        serde_json::to_string_pretty(&mask(v))
            .unwrap_or_default()
            .lines()
            .map(str::to_owned)
            .collect::<Vec<_>>()
    };
    let (a, b) = (render(old), render(new));
    if a == b {
        return vec![];
    }
    // Longest common subsequence; configurations are small.
    let (n, m) = (a.len(), b.len());
    let mut lcs = vec![vec![0u32; m + 1]; n + 1];
    for i in (0..n).rev() {
        for j in (0..m).rev() {
            lcs[i][j] = if a[i] == b[j] {
                lcs[i + 1][j + 1] + 1
            } else {
                lcs[i + 1][j].max(lcs[i][j + 1])
            };
        }
    }
    let mut ops = Vec::new();
    let (mut i, mut j) = (0, 0);
    while i < n || j < m {
        if i < n && j < m && a[i] == b[j] {
            ops.push((' ', a[i].clone()));
            i += 1;
            j += 1;
        } else if j < m && (i == n || lcs[i][j + 1] >= lcs[i + 1][j]) {
            ops.push(('+', b[j].clone()));
            j += 1;
        } else {
            ops.push(('-', a[i].clone()));
            i += 1;
        }
    }
    let changed = ops
        .iter()
        .enumerate()
        .filter(|(_, (k, _))| *k != ' ')
        .map(|(i, _)| i)
        .collect::<Vec<_>>();
    let keep = |i: usize| changed.iter().any(|c| c.abs_diff(i) <= 3);
    let mut out = Vec::new();
    let mut skipped = false;
    for (index, (kind, line)) in ops.iter().enumerate() {
        if keep(index) {
            if skipped && !out.is_empty() {
                out.push("@@".to_owned());
            }
            skipped = false;
            out.push(format!("{kind} {line}"));
        } else {
            skipped = true;
        }
    }
    out
}

/// `90s`, `30m`, `2h`, `1d` or plain seconds, between one minute and seven days.
pub fn parse_duration(text: &str) -> Result<u64> {
    let text = text.trim();
    let (number, unit) = match text.char_indices().last() {
        Some((i, c)) if c.is_ascii_alphabetic() => (&text[..i], c),
        _ => (text, 's'),
    };
    let factor = match unit {
        's' => 1,
        'm' => 60,
        'h' => 3600,
        'd' => 86400,
        _ => bail!("invalid duration"),
    };
    let seconds = number
        .parse::<u64>()
        .ok()
        .and_then(|n| n.checked_mul(factor))
        .context("invalid duration")?;
    ensure!(
        (crate::config::MIN_DURATION..=crate::config::MAX_DURATION).contains(&seconds),
        "invalid duration"
    );
    Ok(seconds)
}

/// RFC 3339 time between one minute and seven days from now.
pub fn parse_until(text: &str, now: f64) -> Result<f64> {
    let at = chrono::DateTime::parse_from_rfc3339(text.trim())
        .ok()
        .context("invalid time")?
        .timestamp() as f64;
    ensure!(
        at >= now + crate::config::MIN_DURATION as f64
            && at <= now + crate::config::MAX_DURATION as f64,
        "invalid time"
    );
    Ok(at)
}

fn object<'a>(value: &'a mut Value, key: &str) -> &'a mut Map<String, Value> {
    if !value[key].is_object() {
        value[key] = json!({});
    }
    value[key].as_object_mut().expect("object")
}
fn drop_empty(value: &mut Value, key: &str) {
    if value[key].as_object().is_some_and(Map::is_empty)
        && let Some(map) = value.as_object_mut()
    {
        map.remove(key);
    }
}
fn paused_mut(raw: &mut Value) -> &mut Map<String, Value> {
    object(object_value(raw, "delivery"), "paused")
}
fn object_value<'a>(value: &'a mut Value, key: &str) -> &'a mut Value {
    if !value[key].is_object() {
        value[key] = json!({});
    }
    &mut value[key]
}
fn tidy_delivery(raw: &mut Value) {
    if raw["delivery"].is_object() {
        drop_empty(&mut raw["delivery"], "paused");
    }
    drop_empty(raw, "delivery");
    drop_empty(raw, "route_overrides");
}

/// Moves topics off a bot that stops serving. Overrides that use the bot,
/// revert to it or sit on a rerouted topic are removed; so is its pause.
fn detach_bot(
    ctx: &Ctx,
    raw: &mut Value,
    id: &str,
    reroute: Option<&str>,
    change: &mut Change,
) -> Result<()> {
    let routed = ctx
        .settings
        .routes
        .iter()
        .filter(|(_, bot)| *bot == id)
        .map(|(topic, _)| topic.clone())
        .collect::<Vec<_>>();
    if !routed.is_empty() {
        let Some(target) = reroute else {
            println!("{}", json!({"ok":false,"routed_topics":routed}));
            bail!("bot is still routed; pass --reroute-to")
        };
        ensure!(target != id, "reroute bot must differ from the bot");
        ensure!(ctx.bot(target)?.enabled, "target bot is disabled");
        let routes = object(raw, "routes");
        for topic in &routed {
            routes.insert(topic.clone(), json!(target));
        }
        change.note("rerouted_topics", json!(routed));
    }
    let removed = ctx
        .settings
        .route_overrides
        .iter()
        .filter(|(topic, o)| {
            o.bot == id
                || o.revert_when_healthy.as_ref().is_some_and(|r| r.bot == id)
                || routed.contains(topic)
        })
        .map(|(topic, _)| topic.clone())
        .collect::<Vec<_>>();
    if !removed.is_empty() {
        let overrides = object(raw, "route_overrides");
        for topic in &removed {
            overrides.remove(topic);
        }
        change.note("removed_overrides", json!(removed));
    }
    paused_mut(raw).remove(id);
    tidy_delivery(raw);
    Ok(())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, clap::ValueEnum)]
pub enum Pending {
    Keep,
    Retarget,
    Discard,
}

fn pending_steps(
    ctx: &Ctx,
    id: &str,
    pending: Pending,
    to: Option<&str>,
    reason: &str,
    change: &mut Change,
) -> Result<()> {
    ensure!(
        to.is_none() || pending == Pending::Retarget,
        "--to is only used with --pending retarget"
    );
    let count = ctx.count(id, "pending")?;
    change.note("pending", json!(count));
    match pending {
        Pending::Keep => {}
        Pending::Retarget => {
            let to = to.context("pending deliveries need --to")?;
            let target = ctx.bot(to)?;
            ensure!(target.enabled && to != id, "target bot is disabled");
            ensure!(
                target.owner == ctx.bot(id)?.owner,
                "target bot delivers to a different chat"
            );
            change.db.retarget.push(RetargetStep {
                select: Selection {
                    ids: vec![],
                    from_bot: Some(id.into()),
                },
                to: to.into(),
                to_owner: String::new(),
            });
        }
        Pending::Discard => change.db.discard.push(DiscardStep {
            select: Selection {
                ids: vec![],
                from_bot: Some(id.into()),
            },
            reason: reason.into(),
        }),
    }
    Ok(())
}

pub fn bots_add(ctx: &Ctx, id: &str, owner: &str, username: &str, token: &str) -> Result<Change> {
    ensure!(
        crate::identifier(id)
            && !owner.is_empty()
            && owner.len() <= 32
            && owner.bytes().all(|b| b.is_ascii_digit()),
        "invalid bot identity"
    );
    ensure!(
        !ctx.settings.bots.contains_key(id),
        "bot already registered"
    );
    let credential = format!("bot-{id}");
    ensure!(
        !secret_path(ctx.config, &credential)?.exists(),
        "credential file already exists"
    );
    let mut raw = ctx.raw.clone();
    object(&mut raw, "bots").insert(
        id.into(),
        json!({"require_start":true,"owner":owner,"credential":credential,
            "username":username,"enabled":true,"menus":[]}),
    );
    let mut change = Change {
        raw: Some(raw),
        ..Default::default()
    };
    change.secrets.insert(credential, Some(token.into()));
    Ok(change)
}

pub fn bots_rotate(ctx: &Ctx, id: &str, token: &str) -> Result<Change> {
    let bot = ctx.bot(id)?;
    let current = ctx.config.token(bot)?;
    let prefix = |t: &str| t.split_once(':').map(|(a, _)| a.to_owned());
    ensure!(
        prefix(&current) == prefix(token),
        "credential prefix does not match the bot"
    );
    ensure!(current != token, "credential unchanged");
    let mut change = Change::default();
    change
        .secrets
        .insert(bot.credential.clone(), Some(token.into()));
    Ok(change)
}

pub fn bots_enable(ctx: &Ctx, id: &str) -> Result<Change> {
    ctx.bot(id)?;
    let mut raw = ctx.raw.clone();
    raw["bots"][id]["enabled"] = json!(true);
    Ok(Change {
        raw: Some(raw),
        ..Default::default()
    })
}

pub fn bots_disable(
    ctx: &Ctx,
    id: &str,
    pending: Pending,
    to: Option<&str>,
    reroute: Option<&str>,
) -> Result<Change> {
    ctx.bot(id)?;
    let mut change = Change::default();
    let mut raw = ctx.raw.clone();
    raw["bots"][id]["enabled"] = json!(false);
    detach_bot(ctx, &mut raw, id, reroute, &mut change)?;
    pending_steps(ctx, id, pending, to, "bot disabled", &mut change)?;
    change.raw = Some(raw);
    Ok(change)
}

pub fn bots_remove(
    ctx: &Ctx,
    id: &str,
    pending: Option<Pending>,
    to: Option<&str>,
    reroute: Option<&str>,
    purge_state: bool,
) -> Result<Change> {
    let bot = ctx.bot(id)?.clone();
    ensure!(
        ctx.count(id, "uncertain")? == 0,
        "bot has uncertain deliveries; resolve them first"
    );
    let pending_count = ctx.count(id, "pending")?;
    let pending = pending.unwrap_or(Pending::Keep);
    ensure!(
        pending_count == 0 || pending != Pending::Keep,
        "bot has pending deliveries; pass --pending retarget or --pending discard"
    );
    let mut change = Change::default();
    let mut raw = ctx.raw.clone();
    detach_bot(ctx, &mut raw, id, reroute, &mut change)?;
    object(&mut raw, "bots").remove(id);
    if pending_count > 0 {
        pending_steps(ctx, id, pending, to, "bot removed", &mut change)?;
    } else {
        ensure!(
            to.is_none() || pending == Pending::Retarget,
            "--to is only used with --pending retarget"
        );
    }
    let shared = ctx
        .settings
        .bots
        .iter()
        .any(|(other, b)| other != id && b.credential == bot.credential);
    if !shared && secret_path(ctx.config, &bot.credential)?.exists() {
        change.secrets.insert(bot.credential.clone(), None);
    }
    if purge_state && let Some(store) = &ctx.store {
        let configured = ctx
            .settings
            .bots
            .keys()
            .filter(|b| *b != id)
            .cloned()
            .collect::<BTreeSet<_>>();
        let stale = store.stale_bot_state(&configured, Some(&[id.to_owned()]))?;
        change.db.delete_kv = stale.kv;
        change.db.delete_updates_for = stale.updates.keys().cloned().collect();
        change.note("updates_kept", json!(stale.updates_kept));
    }
    change.raw = Some(raw);
    Ok(change)
}

pub fn bots_prune_state(ctx: &Ctx) -> Result<Change> {
    let mut change = Change::default();
    let Some(store) = &ctx.store else {
        return Ok(change);
    };
    let configured = ctx.settings.bots.keys().cloned().collect::<BTreeSet<_>>();
    let stale = store.stale_bot_state(&configured, None)?;
    change.note(
        "stale_bots",
        json!(
            stale
                .kv
                .iter()
                .filter_map(|k| crate::db::bot_state_key(k))
                .chain(stale.updates.keys().map(String::as_str))
                .collect::<BTreeSet<_>>()
        ),
    );
    change.note("updates", json!(stale.updates));
    change.note("updates_kept", json!(stale.updates_kept));
    change.db.delete_kv = stale.kv;
    change.db.delete_updates_for = stale.updates.keys().cloned().collect();
    Ok(change)
}

pub fn bots_menu_resync(ctx: &Ctx, ids: &[String], all: bool) -> Result<Change> {
    let selected = if all {
        ctx.settings
            .bots
            .iter()
            .filter(|(_, b)| b.enabled)
            .map(|(id, _)| id.clone())
            .collect::<Vec<_>>()
    } else {
        ensure!(!ids.is_empty(), "select bots or pass --all");
        for id in ids {
            ensure!(ctx.bot(id)?.enabled, "target bot is disabled");
        }
        ids.to_vec()
    };
    let mut change = Change::default();
    change.db.delete_kv = selected.iter().map(|id| format!("menu:{id}")).collect();
    change.wait_menu = selected;
    Ok(change)
}

pub fn routes_set(ctx: &Ctx, topic: &str, bot: &str) -> Result<Change> {
    // `*` names the fallback bot for topics without their own route.
    ensure!(
        topic == crate::config::FALLBACK_TOPIC || crate::identifier(topic),
        "invalid topic"
    );
    ensure!(ctx.bot(bot)?.enabled, "target bot is disabled");
    let mut raw = ctx.raw.clone();
    object(&mut raw, "routes").insert(topic.into(), json!(bot));
    Ok(Change {
        raw: Some(raw),
        ..Default::default()
    })
}

pub fn routes_unset(ctx: &Ctx, topic: &str) -> Result<Change> {
    ensure!(ctx.settings.routes.contains_key(topic), "unknown topic");
    let mut raw = ctx.raw.clone();
    object(&mut raw, "routes").remove(topic);
    Ok(Change {
        raw: Some(raw),
        ..Default::default()
    })
}

pub struct OverrideArgs<'a> {
    pub to: &'a str,
    pub duration: Option<&'a str>,
    pub until: Option<&'a str>,
    pub revert_when_healthy: Option<&'a str>,
    pub reason: &'a str,
}

pub fn routes_override(ctx: &Ctx, topic: &str, args: &OverrideArgs, now: f64) -> Result<Change> {
    let base = ctx.settings.routes.get(topic).context("unknown topic")?;
    ensure!(base != args.to, "bot already serves the topic");
    ensure!(ctx.bot(args.to)?.enabled, "target bot is disabled");
    ensure!(
        args.duration.is_some() || args.until.is_some() || args.revert_when_healthy.is_some(),
        "override needs --for, --until or --revert-when-healthy"
    );
    ensure!(
        args.duration.is_none() || args.until.is_none(),
        "use either --for or --until"
    );
    let mut entry = json!({"bot": args.to});
    if let Some(duration) = args.duration {
        entry["until"] = json!((now + parse_duration(duration)? as f64).floor());
    }
    if let Some(until) = args.until {
        entry["until"] = json!(parse_until(until, now)?);
    }
    if let Some(healthy) = args.revert_when_healthy {
        entry["revert_when_healthy"] =
            json!({"bot": base, "healthy_seconds": parse_duration(healthy)?});
    }
    if !args.reason.is_empty() {
        entry["reason"] = json!(args.reason);
    }
    let mut raw = ctx.raw.clone();
    object(&mut raw, "route_overrides").insert(topic.into(), entry);
    Ok(Change {
        raw: Some(raw),
        ..Default::default()
    })
}

pub fn routes_clear(ctx: &Ctx, topic: Option<&str>, expired: bool, now: f64) -> Result<Change> {
    let topics = match topic {
        Some(topic) => {
            ensure!(
                ctx.settings.route_overrides.contains_key(topic),
                "no route override for topic"
            );
            vec![topic.to_owned()]
        }
        None => {
            ensure!(expired, "no route override for topic");
            ctx.settings
                .route_overrides
                .iter()
                .filter(|(_, o)| Settings::override_expired(o, now))
                .map(|(t, _)| t.clone())
                .collect()
        }
    };
    let mut raw = ctx.raw.clone();
    let overrides = object(&mut raw, "route_overrides");
    for topic in &topics {
        overrides.remove(topic);
    }
    tidy_delivery(&mut raw);
    let mut change = Change {
        raw: Some(raw),
        ..Default::default()
    };
    change.note("cleared", json!(topics));
    Ok(change)
}

/// Topic table with base, override and the bot that serves it now.
pub fn routes_list(config: &Config) -> Result<Value> {
    let settings = config.settings()?;
    let store = if config.db().is_file() {
        Some(Store::new(&config.db())?)
    } else {
        None
    };
    let now = crate::now();
    let topics = settings
        .routes
        .keys()
        .chain(settings.route_overrides.keys())
        .collect::<BTreeSet<_>>();
    let rows = topics
        .into_iter()
        .map(|topic| {
            let effective = if topic.as_str() == crate::config::FALLBACK_TOPIC {
                settings.fallback_bot().map(str::to_owned)
            } else {
                match &store {
                    Some(store) => settings.effective_route(store, topic),
                    None => settings.effective_route_with(topic, now, |_| Value::Null),
                }
            };
            let o = settings.route_overrides.get(topic).map(|o| {
                json!({"bot":o.bot,"until":o.until,"revert_when_healthy":o.revert_when_healthy,
                    "reason":o.reason,"expired":Settings::override_expired(o, now),
                    "active":effective.as_deref() == Some(o.bot.as_str())
                        && settings.routes.get(topic) != Some(&o.bot)})
            });
            json!({"topic":topic,"base":settings.routes.get(topic),"override":o,"effective":effective})
        })
        .collect::<Vec<_>>();
    Ok(json!({"routes":rows,"fallback":settings.fallback_bot()}))
}

/// Bots with state; owners and credentials are never shown.
pub fn bots_list(config: &Config) -> Result<Value> {
    let settings = config.settings()?;
    let store = if config.db().is_file() {
        Some(Store::new(&config.db())?)
    } else {
        None
    };
    let mut rows = Vec::new();
    for (id, bot) in &settings.bots {
        let mut row = json!({"id":id,"enabled":bot.enabled,"username":bot.username,
            "credential_present":config.token(bot).is_ok(),
            "routes":settings.routes.iter().filter(|(_, b)| *b == id).map(|(t, _)| t).collect::<Vec<_>>(),
            "overrides":settings.route_overrides.iter().filter(|(_, o)| o.bot == *id).map(|(t, _)| t).collect::<Vec<_>>(),
            "paused_until":settings.delivery.paused.get(id).map(|p| p.until)});
        if let Some(store) = &store {
            row["connection"] = crate::connection::public(&store.get(&format!("connection:{id}"))?);
            row["menu"] = store.get(&format!("menu-state:{id}"))?;
            row["pending"] = json!(store.deliveries_for_bot(id, "pending")?.len());
            row["uncertain"] = json!(store.deliveries_for_bot(id, "uncertain")?.len());
        }
        rows.push(row);
    }
    Ok(json!({"bots":rows}))
}

pub fn deliveries_retarget(
    ctx: &Ctx,
    ids: &[String],
    from_bot: Option<&str>,
    to: &str,
) -> Result<Change> {
    let target = ctx.bot(to)?;
    ensure!(target.enabled, "target bot is disabled");
    if let Some(source) = from_bot {
        ensure!(
            !ctx.settings.bots.get(source).is_some_and(|b| b.enabled),
            "source bot is still configured"
        );
    }
    ensure!(
        !ids.is_empty() || from_bot.is_some(),
        "no deliveries selected"
    );
    let mut change = Change::default();
    change.db.retarget.push(RetargetStep {
        select: Selection {
            ids: ids.to_vec(),
            from_bot: from_bot.map(str::to_owned),
        },
        to: to.into(),
        to_owner: String::new(),
    });
    Ok(change)
}

pub fn deliveries_purge_bot(ctx: &Ctx, bot: &str, discard: bool) -> Result<Change> {
    ensure!(discard, "pass --discard to suppress pending deliveries");
    let _ = ctx;
    let mut change = Change::default();
    change.db.discard.push(DiscardStep {
        select: Selection {
            ids: vec![],
            from_bot: Some(bot.into()),
        },
        reason: "operator purged".into(),
    });
    Ok(change)
}

pub fn deliveries_defer(ctx: &Ctx, bot: &str, duration: &str, now: f64) -> Result<Change> {
    ensure!(ctx.bot(bot)?.enabled, "target bot is disabled");
    let until = (now + parse_duration(duration)? as f64).floor();
    let mut raw = ctx.raw.clone();
    paused_mut(&mut raw).insert(bot.into(), json!({"until": until}));
    Ok(Change {
        raw: Some(raw),
        ..Default::default()
    })
}

pub fn deliveries_undefer(ctx: &Ctx, bot: &str) -> Result<Change> {
    ensure!(
        ctx.settings.delivery.paused.contains_key(bot),
        "bot is not deferred"
    );
    let mut raw = ctx.raw.clone();
    paused_mut(&mut raw).remove(bot);
    tidy_delivery(&mut raw);
    Ok(Change {
        raw: Some(raw),
        ..Default::default()
    })
}

/// Restores configuration and credentials from an operator backup. Database
/// changes are not reverted; `db-plan.json` lists what was touched.
pub fn restore(ctx: &Ctx, dir: &Path) -> Result<Change> {
    let root = ctx.config.home.join("backups").canonicalize()?;
    let dir = dir.canonicalize().context("invalid backup directory")?;
    ensure!(
        dir.parent() == Some(root.as_path())
            && dir
                .file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with("gateway-ops-")),
        "invalid backup directory"
    );
    let bytes = fs::read(dir.join("bots.json")).context("invalid backup directory")?;
    let raw: Value = serde_json::from_slice(&bytes)?;
    let mut change = Change {
        raw: Some(raw),
        ..Default::default()
    };
    if let Ok(entries) = fs::read_dir(dir.join("secrets")) {
        for entry in entries {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            ensure!(crate::identifier(&name), "invalid backup directory");
            let token = fs::read_to_string(entry.path())?.trim().to_owned();
            change.secrets.insert(name, Some(token));
        }
    }
    change.note(
        "database_not_reverted",
        json!(dir.join("db-plan.json").is_file()),
    );
    Ok(change)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_refusal_in_this_module_is_printable() {
        // Internal faults that are deliberately reported as "request rejected".
        const INTERNAL: &[&str] = &[
            "credential parent missing",
            "run directory missing",
            "logs directory missing",
            "missing parent",
            "object",
        ];
        let source = include_str!("ops.rs");
        let source = &source[..source.find("#[cfg(test)]").unwrap()];
        let literal = |text: &str| -> Option<String> {
            let start = text.find('"')? + 1;
            let end = start + text[start..].find('"')?;
            Some(text[start..end].to_owned())
        };
        let mut messages = Vec::new();
        for (index, _) in source.match_indices("ensure!(") {
            let body = &source[index..];
            let body = &body[..body.find(");").unwrap()];
            let last = body.rfind(",\n").or_else(|| body.rfind(", ")).unwrap();
            messages.extend(literal(&body[last..]));
        }
        for marker in ["bail!(", ".context("] {
            for (index, _) in source.match_indices(marker) {
                let rest = source[index + marker.len()..].trim_start();
                if rest.starts_with('"') {
                    messages.extend(literal(rest));
                }
            }
        }
        assert!(messages.len() > 40, "{}", messages.len());
        let missing = messages
            .iter()
            .filter(|m| {
                !INTERNAL.contains(&m.as_str())
                    && crate::errors::cli_message(&anyhow::anyhow!("{m}")) != m.as_str()
            })
            .collect::<Vec<_>>();
        assert!(missing.is_empty(), "{missing:?}");
    }

    #[test]
    fn durations_are_bounded() {
        assert_eq!(parse_duration("90s").unwrap(), 90);
        assert_eq!(parse_duration("30m").unwrap(), 1800);
        assert_eq!(parse_duration("2h").unwrap(), 7200);
        assert_eq!(parse_duration("7d").unwrap(), 7 * 86400);
        assert_eq!(parse_duration("600").unwrap(), 600);
        for bad in ["59s", "8d", "", "m", "1w", "-5m", "1.5h"] {
            assert!(parse_duration(bad).is_err(), "{bad}");
        }
        let now = 1_800_000_000.0;
        assert!(parse_until("2027-01-16T08:00:00Z", now).is_ok());
        assert!(parse_until("2026-01-01T00:00:00Z", now).is_err());
        assert!(parse_until("2027-01-30T00:00:00Z", now).is_err());
        assert!(parse_until("tomorrow", now).is_err());
    }

    #[test]
    fn diff_masks_owners_and_credentials() {
        let old = json!({"bots":{"a":{"owner":"777001","credential":"bot-a","enabled":true}},"routes":{"t":"a"}});
        let mut new = old.clone();
        new["bots"]["a"]["enabled"] = json!(false);
        new["bots"]["b"] = json!({"owner":"777002","credential":"bot-b","enabled":true});
        let diff = masked_diff(&old, &new).join("\n");
        assert!(diff.contains("+       \"enabled\": false"), "{diff}");
        assert!(diff.contains("+     \"b\": {"));
        assert!(!diff.contains("777001") && !diff.contains("777002"));
        assert!(!diff.contains("bot-b") && diff.contains("***"));
        assert!(masked_diff(&old, &old).is_empty());
    }

    #[test]
    fn backup_names_match_the_release_prune_parser() {
        let name = backup_name("bots-remove", 1_793_000_000.0);
        assert!(name.starts_with("gateway-ops-2026"), "{name}");
        assert!(name.ends_with("Z-bots-remove"), "{name}");
        assert_eq!(name.len(), "gateway-ops-".len() + 16 + "-bots-remove".len());
    }
}
