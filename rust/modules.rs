use anyhow::{Context, Result, ensure};
use std::{collections::BTreeSet, path::Path};

fn source_name(path: &Path) -> bool {
    let private = [
        "__pycache__",
        "node_modules",
        "target",
        "build",
        "dist",
        "cache",
        "data",
        "secrets",
        "profiles",
        "private",
        "logs",
        "cron_state",
        "artifacts",
        "cookies.json",
        "google_token.json",
        "token.json",
        "auth.json",
        "credentials.json",
    ];
    !path.components().any(|c| {
        let n = c.as_os_str().to_string_lossy();
        n.starts_with('.') || private.contains(&n.as_ref())
    }) && !path.extension().is_some_and(|s| {
        [
            "pyc",
            "pyo",
            "db",
            "sqlite",
            "sqlite3",
            "session",
            "pem",
            "key",
            "p12",
            "pfx",
            "log",
            "tar",
            "gz",
            "zip",
            "whl",
            "db-wal",
            "db-shm",
            "db-journal",
            "sqlite-wal",
            "sqlite-shm",
            "sqlite-journal",
            "sqlite3-wal",
            "sqlite3-shm",
            "sqlite3-journal",
        ]
        .contains(&s.to_string_lossy().as_ref())
    })
}
pub(crate) struct Sources {
    pub files: Vec<std::path::PathBuf>,
    pub directories: Vec<std::path::PathBuf>,
}

pub(crate) fn source_inventory(root: &Path) -> Result<Sources> {
    fn visit(root: &Path, dir: &Path, out: &mut Sources) -> Result<()> {
        for entry in std::fs::read_dir(dir)? {
            let entry = entry?;
            let path = entry.path();
            let relative = path.strip_prefix(root)?;
            if !source_name(relative) {
                continue;
            }
            let meta = entry.file_type()?;
            ensure!(
                !meta.is_symlink(),
                "module packages cannot contain symbolic links"
            );
            if meta.is_dir() {
                out.directories.push(relative.to_path_buf());
                visit(root, &path, out)?;
            } else if meta.is_file() {
                out.files.push(relative.to_path_buf());
            }
        }
        Ok(())
    }
    let mut sources = Sources {
        files: vec![],
        directories: vec![],
    };
    visit(root, root, &mut sources)?;
    sources.files.sort();
    sources.directories.sort();
    Ok(sources)
}

pub(crate) fn source_files(root: &Path) -> Result<Vec<std::path::PathBuf>> {
    Ok(source_inventory(root)?.files)
}
pub fn content_hash(root: &Path) -> Result<String> {
    #[cfg(test)]
    crate::reload::reads::record(root);
    use sha2::{Digest, Sha256};
    let mut hash = Sha256::new();
    for path in source_files(root)? {
        hash.update(path.to_string_lossy().as_bytes());
        hash.update([0]);
        use std::os::unix::fs::PermissionsExt;
        hash.update([
            if std::fs::metadata(root.join(&path))?.permissions().mode() & 0o111 != 0 {
                1
            } else {
                0
            },
        ]);
        hash.update(std::fs::read(root.join(path))?);
        hash.update([0]);
    }
    Ok(hex::encode(hash.finalize()))
}
pub fn pack(root: &Path, output: &Path) -> Result<serde_json::Value> {
    let manifest: crate::catalog::Manifest =
        serde_json::from_slice(&std::fs::read(root.join("module.json"))?)?;
    ensure!(
        manifest.api_version == 2 && crate::identifier(&manifest.id),
        "invalid manifest"
    );
    let files = source_files(root)?;
    let mut archive = tar::Builder::new(flate2::write::GzEncoder::new(
        std::fs::File::create(output)?,
        flate2::Compression::default(),
    ));
    for path in files {
        let bytes = std::fs::read(root.join(&path))?;
        let mut header = tar::Header::new_gnu();
        header.set_path(&path)?;
        header.set_size(bytes.len() as u64);
        use std::os::unix::fs::PermissionsExt;
        header.set_mode(
            if std::fs::metadata(root.join(&path))?.permissions().mode() & 0o111 != 0 {
                0o755
            } else {
                0o644
            },
        );
        header.set_mtime(0);
        header.set_cksum();
        archive.append(&header, bytes.as_slice())?;
    }
    archive.into_inner()?.finish()?.sync_all()?;
    Ok(
        serde_json::json!({"id":manifest.id,"content_hash":content_hash(root)?,"sha256":crate::digest(std::fs::read(output)?)}),
    )
}
fn index(base: &Path) -> Result<serde_json::Value> {
    let path = base.join("registry.json");
    if path.is_file() {
        Ok(serde_json::from_slice(&std::fs::read(path)?)?)
    } else {
        let mut modules = serde_json::Map::new();
        for item in std::fs::read_dir(base)? {
            let path = item?.path();
            if path
                .file_name()
                .is_some_and(|n| n.to_string_lossy().starts_with('.'))
            {
                continue;
            }
            if path.join("module.json").is_file() {
                let id = path.file_name().unwrap().to_string_lossy().into_owned();
                modules.insert(id.clone(), serde_json::json!(id));
            }
        }
        Ok(serde_json::json!({"api_version":2,"modules":modules,"previous":{}}))
    }
}
fn validate_index(base: &Path, value: &serde_json::Value) -> Result<()> {
    let catalog = crate::catalog::Catalog::load_index(base.to_path_buf(), Some(value.clone()))?;
    // A change may only reject modules the current registry already rejects.
    let current = crate::catalog::Catalog::load_index(base.to_path_buf(), Some(index(base)?)).ok();
    catalog.ensure_accepted(current.as_ref())?;
    for module in catalog.modules.values() {
        let entries = std::iter::once(&module.manifest.handler)
            .chain(module.manifest.jobs.values().map(|j| &j.entry))
            .chain(module.manifest.services.values().map(|s| &s.entry))
            .chain(module.manifest.cli.values())
            .chain((!module.manifest.lifecycle.is_empty()).then_some(&module.manifest.lifecycle));
        for entry in entries {
            ensure!(
                crate::catalog::valid_entry(entry),
                "invalid executable entry"
            );
            if entry[0] == "python" {
                ensure!(
                    entry[1]
                        .split('.')
                        .all(|s| crate::identifier(s) && !s.contains('-')),
                    "invalid Python module"
                );
                if !entry[1].starts_with("pylon_sdk.") {
                    let path = entry[1].replace('.', "/");
                    ensure!(
                        catalog.modules.values().any(|m| m
                            .root
                            .join("src")
                            .join(format!("{path}.py"))
                            .is_file()
                            || m.root.join("src").join(&path).join("__main__.py").is_file()),
                        "Python entrypoint missing"
                    );
                }
            } else {
                let path = module.root.join(&entry[1]).canonicalize()?;
                ensure!(
                    path.starts_with(&module.root) && path.is_file(),
                    "executable escapes module"
                );
            }
        }
    }
    Ok(())
}
fn remove_tree(path: &Path) -> Result<()> {
    for entry in std::fs::read_dir(path)? {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            remove_tree(&entry.path())?
        } else {
            std::fs::remove_file(entry.path())?;
        }
    }
    std::fs::remove_dir(path)?;
    Ok(())
}
pub fn install(
    config: &crate::config::Config,
    archive: &Path,
    expected: &str,
    health: bool,
) -> Result<serde_json::Value> {
    ensure!(
        expected.len() == 64 && crate::digest(std::fs::read(archive)?) == expected,
        "module archive checksum mismatch"
    );
    let base = config.home.join("modules");
    std::fs::create_dir_all(&base)?;
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(base.join(".leases.lock"))?;
    crate::runtime::lock(&lock)?;
    let staging = base.join(format!(".install-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&staging)?;
    let result = (|| -> Result<serde_json::Value> {
        let mut tar =
            tar::Archive::new(flate2::read::GzDecoder::new(std::fs::File::open(archive)?));
        let mut total = 0;
        let mut seen = BTreeSet::new();
        for item in tar.entries()? {
            let mut item = item?;
            let path = item.path()?.into_owned();
            ensure!(
                path.components()
                    .all(|c| matches!(c, std::path::Component::Normal(_))),
                "unsafe archive path"
            );
            ensure!(
                item.header().entry_type().is_file()
                    && source_name(&path)
                    && seen.insert(path.clone()),
                "only unique regular source files accepted"
            );
            total += item.size();
            ensure!(
                total <= 128 * 1024 * 1024,
                "module source exceeds size limit"
            );
            let executable = item.header().mode()? & 0o111 != 0;
            ensure!(item.unpack_in(&staging)?, "archive path escapes module");
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(
                staging.join(path),
                std::fs::Permissions::from_mode(if executable { 0o755 } else { 0o644 }),
            )?;
        }
        let manifest: crate::catalog::Manifest =
            serde_json::from_slice(&std::fs::read(staging.join("module.json"))?)?;
        ensure!(
            crate::identifier(&manifest.id) && manifest.api_version == 2,
            "invalid module"
        );
        let hash = content_hash(&staging)?;
        let relative = format!("releases/{}/{hash}", manifest.id);
        let destination = base.join(&relative);
        let mut registry = index(&base)?;
        let previous = registry["modules"][&manifest.id].clone();
        registry["modules"][&manifest.id] =
            serde_json::json!(staging.file_name().unwrap().to_string_lossy());
        validate_index(&base, &registry)?;
        if health {
            ensure_workspaces(config, &manifest)?;
            accept(config, &base, &registry, &manifest.id)?;
            if let Some(path) = previous.as_str() {
                let old: crate::catalog::Manifest =
                    serde_json::from_slice(&std::fs::read(base.join(path).join("module.json"))?)?;
                // Since v0.5.29 a data version change needs no global freeze:
                // the module's own drain marker plus this active-job check suffice.
                data_version_ready(&old, &manifest, unit_active, "change")?;
            }
            ensure_runtime(config, &manifest.id)?;
            health_candidate(
                config,
                &crate::catalog::Catalog::load_index(base.clone(), Some(registry.clone()))?,
                &manifest.id,
            )?;
        }
        if !destination.exists() {
            std::fs::create_dir_all(destination.parent().context("destination parent missing")?)?;
            std::fs::rename(&staging, &destination)?;
        }
        registry["modules"][&manifest.id] = serde_json::json!(relative);
        let changed = previous != registry["modules"][&manifest.id];
        if !previous.is_null() && changed {
            registry["previous"][&manifest.id] = previous;
        }
        if !health {
            crate::config::atomic_json(&base.join("registry.json"), &registry, 0o644)?;
        }
        Ok(
            serde_json::json!({"id":manifest.id,"hash":hash,"services":manifest.services.keys().collect::<Vec<_>>(),"changed":changed,"candidate_registry":registry}),
        )
    })();
    if staging.exists() {
        remove_tree(&staging)?;
    }
    result
}
pub fn rollback(
    config: &crate::config::Config,
    id: &str,
    health: bool,
) -> Result<serde_json::Value> {
    ensure!(crate::identifier(id), "invalid module");
    let base = config.home.join("modules");
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(base.join(".leases.lock"))?;
    crate::runtime::lock(&lock)?;
    let mut registry = index(&base)?;
    let previous = registry["previous"][id].clone();
    ensure!(previous.is_string(), "no previous module version");
    let current = registry["modules"][id].clone();
    registry["modules"][id] = previous;
    registry["previous"][id] = current.clone();
    validate_index(&base, &registry)?;
    if health {
        accept(config, &base, &registry, id)?;
        let original: crate::catalog::Manifest = serde_json::from_slice(&std::fs::read(
            base.join(current.as_str().context("current module version missing")?)
                .join("module.json"),
        )?)?;
        let candidate: crate::catalog::Manifest = serde_json::from_slice(&std::fs::read(
            base.join(
                registry["modules"][id]
                    .as_str()
                    .context("candidate module version missing")?,
            )
            .join("module.json"),
        )?)?;
        data_version_ready(&original, &candidate, unit_active, "rollback")?;

        health_candidate(
            config,
            &crate::catalog::Catalog::load_index(base.clone(), Some(registry.clone()))?,
            id,
        )?;
    }
    if !health {
        crate::config::atomic_json(&base.join("registry.json"), &registry, 0o644)?;
    }
    Ok(serde_json::json!({"id":id,"rolled_back":true,"candidate_registry":registry}))
}

fn field(s: &str, min: u32, max: u32) -> Result<String> {
    if s == "*" {
        return Ok("*".into());
    }
    let mut values = BTreeSet::new();
    for part in s.split(',') {
        let (base, step) = part
            .split_once('/')
            .map(|(a, b)| Ok::<_, anyhow::Error>((a, b.parse::<u32>()?)))
            .unwrap_or(Ok((part, 1)))?;
        ensure!(step > 0 && step <= max + 1, "invalid cron step");
        let (start, end) = if base == "*" {
            (min, max)
        } else if let Some((a, b)) = base.split_once('-') {
            (a.parse()?, b.parse()?)
        } else {
            let n = base.parse()?;
            (n, if part.contains('/') { max } else { n })
        };
        ensure!(
            start >= min && end <= max && start <= end,
            "invalid cron field"
        );
        for n in (start..=end).step_by(step as usize) {
            values.insert(n);
        }
    }
    ensure!(!values.is_empty(), "empty cron field");
    Ok(values
        .into_iter()
        .map(|v| v.to_string())
        .collect::<Vec<_>>()
        .join(","))
}
pub fn calendar(cron: &str) -> Result<String> {
    let f = cron.split_whitespace().collect::<Vec<_>>();
    ensure!(f.len() == 5, "cron must contain five fields");
    ensure!(
        f[2] == "*" || f[4] == "*",
        "combined day-of-month and weekday require separate schedules"
    );
    let minute = field(f[0], 0, 59)?;
    let hour = field(f[1], 0, 23)?;
    let day = field(f[2], 1, 31)?;
    let month = field(f[3], 1, 12)?;
    let weekday = if f[4] == "*" {
        String::new()
    } else {
        let days = field(f[4], 0, 7)?;
        format!(
            "{} ",
            days.split(',')
                .map(|s| ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
                    [s.parse::<usize>().unwrap()])
                .collect::<Vec<_>>()
                .join(",")
        )
    };
    Ok(format!(
        "{weekday}*-{month}-{day} {hour}:{minute}:00 Asia/Shanghai"
    ))
}
pub fn check(path: &Path) -> Result<serde_json::Value> {
    let catalog = crate::catalog::Catalog::load_from(path.to_path_buf())?;
    catalog.ensure_accepted(None)?;
    Ok(
        serde_json::json!({"api_version":2,"modules":catalog.modules.keys().collect::<Vec<_>>(),"hash":catalog.hash}),
    )
}
pub fn render(config: &crate::config::Config, destination: &Path) -> Result<()> {
    let catalog = crate::catalog::Catalog::load(config)?;
    std::fs::create_dir_all(destination)?;
    for module in catalog.modules.values() {
        for (id, job) in &module.manifest.jobs {
            let schedule = if job.monotonic {
                "OnBootSec=2min\nOnUnitInactiveSec=5min".into()
            } else {
                format!("OnCalendar={}\nPersistent=true", calendar(&job.schedule)?)
            };
            let timer = format!(
                "[Unit]\nDescription=Pylon task {id}\n[Timer]\n{schedule}\nAccuracySec=1s\n{}Unit=pylon-task@{id}.service\n[Install]\nWantedBy=timers.target\n",
                if job.jitter {
                    "RandomizedDelaySec=300\n"
                } else {
                    ""
                }
            );
            std::fs::write(destination.join(format!("pylon-job-{id}.timer")), timer)?;
        }
    }
    let binary = config.home.join("bin/pylon");
    let home = &config.home;
    ensure!(
        !home.to_string_lossy().chars().any(char::is_whitespace),
        "production service path contains whitespace"
    );
    for (name, command, user, group) in [
        ("pylon-gateway.service", "gateway", "pylon-gateway", "pylon"),
        ("pylon-executor.service", "executor", "root", "pylon"),
        ("pylon-broker.service", "broker", "root", "pylon"),
        ("pylon-task@.service", "execute %i", "root", "pylon"),
        ("pylon-module@.service", "serve %i", "root", "pylon"),
    ] {
        let oneshot = name.contains("task@");
        let broker = user == "root";
        let content = format!(
            "[Unit]\nDescription=Pylon {command}\nAfter=network-online.target\n{}[Service]\nType={}\nUser={user}\nGroup={group}\nWorkingDirectory={}\nExecStart={} --home {} {command}\nUMask=0007\nKillMode=control-group\nTimeoutStartSec=infinity\nTimeoutStopSec=30\n{}{}\n[Install]\nWantedBy=multi-user.target\n",
            if user != "pylon-gateway" {
                "After=pylon-gateway.service\nWants=pylon-gateway.service\n"
            } else {
                ""
            },
            if oneshot { "oneshot" } else { "simple" },
            home.display(),
            binary.display(),
            home.display(),
            if oneshot {
                ""
            } else {
                "Restart=on-failure\nRestartSec=5\n"
            },
            if broker {
                ""
            } else {
                "NoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nReadWritePaths=/srv/pylon\n"
            }
        );
        std::fs::write(destination.join(name), content)?;
    }
    Ok(())
}

fn systemctl(args: &[&str]) -> Result<()> {
    let output = std::process::Command::new("/usr/bin/systemctl")
        .args(args)
        .output()?;
    ensure!(
        output.status.success(),
        "service operation failed; inspect systemd status"
    );
    Ok(())
}
fn enabled(unit: &str) -> bool {
    std::process::Command::new("/usr/bin/systemctl")
        .args(["is-enabled", "--quiet", unit])
        .status()
        .is_ok_and(|s| s.success())
}
fn health_candidate(
    config: &crate::config::Config,
    catalog: &crate::catalog::Catalog,
    id: &str,
) -> Result<()> {
    use std::{os::unix::fs::PermissionsExt, time::Duration};
    let module = &catalog.modules[id];
    let root = config
        .home
        .join("run")
        .join(format!(".health-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&root)?;
    let outcome = (|| -> Result<()> {
        let users = config.settings()?;
        let user = users
            .runtime_users
            .get(id)
            .ok_or_else(|| anyhow::anyhow!("runtime identity missing"))?;
        for name in ["data", "home"] {
            std::fs::create_dir_all(root.join(name))?;
        }
        for workspace in users.workspaces.values() {
            let sandbox = crate::config::Config::new(root.clone());
            let path = sandbox.contained(&workspace.root)?;
            std::fs::create_dir_all(&path)?;
            ensure!(
                sandbox.contained(&workspace.root)? == path,
                "workspace changed during creation"
            );
        }
        std::fs::create_dir_all(root.join("profiles").join(&module.manifest.workspace))?;
        fn own(path: &Path, uid: u32, gid: u32) -> Result<()> {
            std::os::unix::fs::chown(path, Some(uid), Some(gid))?;
            for e in std::fs::read_dir(path)? {
                let p = e?.path();
                if p.is_dir() {
                    own(&p, uid, gid)?;
                }
            }
            Ok(())
        }
        own(&root, user.uid, user.gid)?;
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o750))?;
        let entry = &module.manifest.handler;
        let mut cmd = std::process::Command::new(match entry[0].as_str() {
            "python" => config.python(),
            "bash" => "/bin/bash".into(),
            "node" => "/usr/bin/node".into(),
            _ => module.root.join(&entry[1]),
        });
        if entry[0] == "python" {
            cmd.arg("-m").args(&entry[1..]);
        } else if entry[0] != "native" {
            cmd.arg(module.root.join(&entry[1])).args(&entry[2..]);
        } else {
            cmd.args(&entry[2..]);
        }
        cmd.env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("HOME", root.join("home"))
            .env("PYLON_HOME", &root)
            .env(
                "PYLON_PROFILE_ROOT",
                root.join("profiles").join(&module.manifest.workspace),
            )
            .env("PYLON_MODULES_DIR", config.home.join("modules"))
            .env(
                "PYLON_MODULE_SNAPSHOT",
                serde_json::to_string(
                    &catalog
                        .modules
                        .iter()
                        .map(|(id, m)| (id, m.root.to_string_lossy()))
                        .collect::<std::collections::BTreeMap<_, _>>(),
                )?,
            )
            .env(
                "PYTHONPATH",
                std::iter::once(
                    config
                        .home
                        .join("sdk/current/src")
                        .to_string_lossy()
                        .into_owned(),
                )
                .chain(
                    catalog
                        .modules
                        .values()
                        .map(|m| m.root.join("src").to_string_lossy().into_owned()),
                )
                .collect::<Vec<_>>()
                .join(":"),
            );
        cmd.current_dir(&root);
        crate::child_identity(&mut cmd, user.uid, user.gid);
        let input = serde_json::to_vec(
            &serde_json::json!({"api_version":2,"action":"health","profile":module.manifest.workspace,"payload":{}}),
        )?;
        let output =
            crate::check_process::run(&mut cmd, input, Duration::from_secs(60), 1024 * 1024)
                .context("module health check failed")?;
        let value: serde_json::Value = serde_json::from_slice(&output)?;
        ensure!(
            value["api_version"] == 2 && value["value"]["status"] == "ok",
            "module health check rejected"
        );
        Ok(())
    })();
    remove_tree(&root)?;
    outcome
}
/// Modules only read drain markers (SDK `lifecycle.draining()` and operations
/// diagnostics), so every module identity needs search/list access, while only
/// root writes. If a restrictive umask created the directory privately, a
/// module would silently miss the marker and keep accepting work mid-deploy.
fn drain_mode(current: u32) -> Option<u32> {
    // Add-only: never removes a bit an operator granted deliberately.
    let wanted = (current & 0o7777) | 0o2750;
    (current & 0o7777 != wanted).then_some(wanted)
}
fn ensure_drain_directory(directory: &std::path::Path) -> Result<()> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    let meta = std::fs::symlink_metadata(directory)?;
    anyhow::ensure!(meta.is_dir(), "drain path is not a directory");
    if unsafe { libc::geteuid() } != 0 || meta.uid() != 0 {
        return Ok(());
    }
    // Same group as run/ (the service group every module identity belongs to).
    let group = std::fs::metadata(directory.parent().context("drain parent missing")?)?.gid();
    if meta.gid() != group {
        std::os::unix::fs::lchown(directory, None, Some(group))?;
    }
    if let Some(mode) = drain_mode(meta.mode()) {
        std::fs::set_permissions(directory, std::fs::Permissions::from_mode(mode))?;
    }
    Ok(())
}
struct DrainGuard(std::path::PathBuf);
impl Drop for DrainGuard {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}
const STOP_LIMIT: std::time::Duration = std::time::Duration::from_secs(600);
fn prepare_stop(
    config: &crate::config::Config,
    catalog: &crate::catalog::Catalog,
    id: &str,
) -> Result<DrainGuard> {
    prepare_stop_within(config, catalog, id, STOP_LIMIT)
}
fn prepare_stop_within(
    config: &crate::config::Config,
    catalog: &crate::catalog::Catalog,
    id: &str,
    limit: std::time::Duration,
) -> Result<DrainGuard> {
    let path = config.home.join("run/drain").join(id);
    let directory = path.parent().context("destination parent missing")?;
    std::fs::create_dir_all(directory)?;
    ensure_drain_directory(directory)?;
    // Module changes are serialized, so an existing marker is a leftover of
    // an interrupted deployment: take it over instead of failing forever.
    if std::fs::symlink_metadata(&path).is_ok() {
        crate::warn("stale-drain-marker", serde_json::json!({"module":id}));
    }
    let marker = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(&path)?;
    marker.sync_all()?;
    let deadline = std::time::Instant::now() + limit;
    let guard = DrainGuard(path);
    let Some(module) = catalog.modules.get(id) else {
        return Ok(guard);
    };
    loop {
        let mut active = false;
        for (job_id, job) in &module.manifest.jobs {
            let name = if job.lock.is_empty() {
                job_id
            } else {
                &job.lock
            };
            let lock = crate::runtime::open_task_lock(
                &config.home.join("run").join(format!("{name}.lock")),
            )?;
            if !crate::runtime::try_lock(&lock)? {
                active = true;
                break;
            }
        }
        if !active && !module.manifest.lifecycle.is_empty() {
            let entry = &module.manifest.lifecycle;
            let mut command = std::process::Command::new(match entry[0].as_str() {
                "python" => config.python(),
                "bash" => "/bin/bash".into(),
                "node" => "/usr/bin/node".into(),
                _ => module.root.join(&entry[1]),
            });
            if entry[0] == "python" {
                command.arg("-m").args(&entry[1..]);
            } else if entry[0] != "native" {
                command.arg(module.root.join(&entry[1])).args(&entry[2..]);
            } else {
                command.args(&entry[2..]);
            }
            let env = crate::runtime::environment(
                config,
                catalog,
                module,
                &module.manifest.workspace,
                "",
                "",
            )?;
            let users = config.settings()?;
            let user = users
                .runtime_users
                .get(id)
                .ok_or_else(|| anyhow::anyhow!("runtime identity missing"))?;
            crate::child_identity(&mut command, user.uid, user.gid);
            command.env_clear().envs(env);
            let output = crate::check_process::run(
                &mut command,
                Vec::new(),
                std::time::Duration::from_secs(30),
                65536,
            )
            .context("module stop preparation failed")?;
            let value: serde_json::Value = serde_json::from_slice(&output)?;
            let result = value["ready"]
                .as_bool()
                .context("invalid stop preparation response");
            active = !result?;
        }
        if !active {
            return Ok(guard);
        }
        ensure!(
            std::time::Instant::now() < deadline,
            "module stop preparation timed out after 10 minutes; tasks still active"
        );
        std::thread::sleep(std::time::Duration::from_secs(1).min(limit));
    }
}
fn activate(
    config: &crate::config::Config,
    old: &crate::catalog::Catalog,
    new: &crate::catalog::Catalog,
    id: &str,
) -> Result<()> {
    let before = old.modules.get(id);
    let after = new
        .modules
        .get(id)
        .ok_or_else(|| anyhow::anyhow!("module missing"))?;
    let staging = config
        .home
        .join("run")
        .join(format!(".units-{}", uuid::Uuid::new_v4()));
    render(config, &staging)?;
    let result = (|| -> Result<()> {
        let old_jobs = before
            .map(|m| m.manifest.jobs.keys().cloned().collect::<BTreeSet<_>>())
            .unwrap_or_default();
        let new_jobs = after.manifest.jobs.keys().cloned().collect::<BTreeSet<_>>();
        let mut start = vec![];
        for job in &new_jobs {
            let unit = format!("pylon-job-{job}.timer");
            if !old_jobs.contains(job) || enabled(&unit) {
                start.push(unit.clone());
            }
            std::fs::copy(
                staging.join(&unit),
                Path::new("/etc/systemd/system").join(&unit),
            )?;
        }
        for job in old_jobs.difference(&new_jobs) {
            let unit = format!("pylon-job-{job}.timer");
            systemctl(&["disable", "--now", &unit])?;
            let path = Path::new("/etc/systemd/system").join(unit);
            if path.exists() {
                std::fs::remove_file(path)?;
            }
        }
        let services = before
            .map(|m| m.manifest.services.keys().cloned().collect::<BTreeSet<_>>())
            .unwrap_or_default();
        let mut start_services = vec![];
        for service in after.manifest.services.keys() {
            let unit = format!("pylon-module@{id}--{service}.service");
            if !services.contains(service) || enabled(&unit) {
                start_services.push(unit);
            }
        }
        for service in services {
            let unit = format!("pylon-module@{id}--{service}.service");
            systemctl(&["stop", &unit])?;
            if !after.manifest.services.contains_key(&service) {
                systemctl(&["disable", &unit])?;
            }
        }
        systemctl(&["daemon-reload"])?;
        let status = std::process::Command::new(std::env::current_exe()?)
            .args([
                "--home",
                config.home.to_str().context("invalid home path")?,
                "rpc",
                "admin.reload",
            ])
            .stdout(std::process::Stdio::null())
            .status()?;
        ensure!(status.success(), "gateway refused module reload");
        for unit in start.iter().chain(&start_services) {
            systemctl(&["enable", "--now", unit])?;
        }
        std::thread::sleep(std::time::Duration::from_secs(2));
        for unit in start_services {
            systemctl(&["is-active", "--quiet", &unit])?;
        }
        Ok(())
    })();
    remove_tree(&staging)?;
    result
}
pub fn change(
    config: &crate::config::Config,
    archive: Option<(&Path, &str)>,
    undo: Option<&str>,
    live: bool,
) -> Result<serde_json::Value> {
    let base = config.home.join("modules");
    std::fs::create_dir_all(&base)?;
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(base.join(".deploy.lock"))?;
    crate::runtime::lock(&lock)?;
    let original = index(&base)?;
    let old = crate::catalog::Catalog::load_from(base.clone())?;
    if live {
        ensure!(
            unsafe { libc::geteuid() } == 0 && !config.offline,
            "live deployment requires the root service manager"
        );
    }
    let mut result = if let Some((archive, sha)) = archive {
        install(config, archive, sha, live)?
    } else {
        rollback(
            config,
            undo.ok_or_else(|| anyhow::anyhow!("module required"))?,
            live,
        )?
    };
    let candidate = result
        .as_object_mut()
        .context("invalid module result")?
        .remove("candidate_registry")
        .context("candidate registry missing")?;
    let id = result["id"].as_str().context("module identity missing")?;
    if live {
        let next = crate::catalog::Catalog::load_index(base.clone(), Some(candidate.clone()))?;
        let _drain = match prepare_stop(config, &old, id) {
            Ok(guard) => guard,
            Err(error) => {
                crate::config::atomic_json(&base.join("registry.json"), &original, 0o644)?;
                return Err(error);
            }
        };
        let applied = (|| -> Result<()> {
            if let Some(before) = old.modules.get(id) {
                data_version_ready(
                    &before.manifest,
                    &next.modules.get(id).context("module missing")?.manifest,
                    unit_active,
                    "change",
                )?;
            }
            crate::config::atomic_json(&base.join("registry.json"), &candidate, 0o644)?;
            activate(config, &old, &next, id)
        })();
        if let Err(error) = applied {
            crate::config::atomic_json(&base.join("registry.json"), &original, 0o644)?;
            let restored = if old.modules.contains_key(id) {
                activate(config, &next, &old, id)
            } else {
                (|| -> Result<()> {
                    for service in next.modules[id].manifest.services.keys() {
                        systemctl(&[
                            "disable",
                            "--now",
                            &format!("pylon-module@{id}--{service}.service"),
                        ])?;
                    }
                    for job in next.modules[id].manifest.jobs.keys() {
                        systemctl(&["disable", "--now", &format!("pylon-job-{job}.timer")])?;
                    }
                    let status = std::process::Command::new(std::env::current_exe()?)
                        .args([
                            "--home",
                            config.home.to_str().context("invalid home path")?,
                            "rpc",
                            "admin.reload",
                        ])
                        .status()?;
                    ensure!(status.success(), "gateway registry restore failed");
                    Ok(())
                })()
            };
            ensure!(
                restored.is_ok(),
                "module activation and service restoration failed; previous registry preserved, inspect services"
            );
            return Err(error);
        }
    }
    collect_versions(config)?;
    Ok(result)
}

fn unit_active(unit: &str) -> Result<bool> {
    Ok(std::process::Command::new("/usr/bin/systemctl")
        .args(["is-active", "--quiet", unit])
        .status()?
        .success())
}
/// A data version change waits only for the module's own active jobs; the
/// per-module drain marker already stops new runs of this module.
fn data_version_ready(
    before: &crate::catalog::Manifest,
    after: &crate::catalog::Manifest,
    active: impl Fn(&str) -> Result<bool>,
    action: &str,
) -> Result<()> {
    if before.data_version == after.data_version {
        return Ok(());
    }
    for job in before.jobs.keys() {
        if active(&format!("pylon-task@{job}.service"))? {
            anyhow::bail!(if action == "rollback" {
                "data version rollback waits for active jobs"
            } else {
                "data version change waits for active jobs"
            });
        }
    }
    Ok(())
}
/// Install and rollback reject only the candidate: other modules the current
/// configuration already rejects stay rejected without blocking the change.
fn accept(
    config: &crate::config::Config,
    base: &Path,
    registry: &serde_json::Value,
    id: &str,
) -> Result<()> {
    let settings = config.settings()?;
    let mut next = crate::catalog::Catalog::load_index(base.to_path_buf(), Some(registry.clone()))?;
    next.apply_settings(&settings);
    if let Some(reason) = next.rejected.get(id) {
        anyhow::bail!("{reason}");
    }
    let mut current = crate::catalog::Catalog::load_index(base.to_path_buf(), Some(index(base)?))?;
    current.apply_settings(&settings);
    next.ensure_accepted(Some(&current))
}
/// Register workspaces a module names but bots.json lacks, like runtime
/// users: root `profiles/<id>`, env `profiles/<id>/.env` (optional file).
fn ensure_workspaces(
    config: &crate::config::Config,
    manifest: &crate::catalog::Manifest,
) -> Result<()> {
    let mut settings = config.settings()?;
    let missing = std::iter::once(&manifest.workspace)
        .chain(manifest.jobs.values().map(|j| &j.workspace))
        .chain(manifest.services.values().map(|s| &s.workspace))
        .filter(|w| crate::identifier(w) && !settings.workspaces.contains_key(*w))
        .cloned()
        .collect::<BTreeSet<_>>();
    if missing.is_empty() {
        return Ok(());
    }
    ensure!(
        unsafe { libc::geteuid() } == 0,
        "workspace registration requires root"
    );
    let profiles = config.home.join("profiles");
    for id in &missing {
        let root = format!("profiles/{id}");
        let path = config.contained(&root)?;
        if !path.exists() {
            use std::os::unix::fs::{MetadataExt, PermissionsExt};
            std::fs::create_dir_all(&path)?;
            let group = std::fs::metadata(&profiles)
                .or_else(|_| std::fs::metadata(&config.home))?
                .gid();
            std::os::unix::fs::lchown(&path, Some(0), Some(group))?;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o2770))?;
        }
        settings.workspaces.insert(
            id.clone(),
            crate::config::Workspace {
                root,
                env: format!("profiles/{id}/.env"),
            },
        );
        crate::warn("workspace-registered", serde_json::json!({"workspace":id}));
    }
    crate::config::atomic_json(&config.home.join("config/bots.json"), &settings, 0o644)?;
    Ok(())
}
fn ensure_runtime(config: &crate::config::Config, id: &str) -> Result<()> {
    let mut settings = config.settings()?;
    if settings.runtime_users.contains_key(id) {
        return Ok(());
    }
    ensure!(
        unsafe { libc::geteuid() } == 0,
        "module runtime registration requires root"
    );
    let name = format!(
        "pyl-m-{}-{}",
        &id[..id.len().min(17)],
        &crate::digest(id)[..6]
    );
    if !std::process::Command::new("id")
        .args(["-u", &name])
        .output()?
        .status
        .success()
    {
        ensure!(
            std::process::Command::new("useradd")
                .args([
                    "--system",
                    "--no-create-home",
                    "--home-dir",
                    "/nonexistent",
                    "--shell",
                    "/usr/sbin/nologin",
                    "--gid",
                    "pylon",
                    &name
                ])
                .status()?
                .success(),
            "cannot create module runtime identity"
        );
    }
    let number = |flag: &str| -> Result<u32> {
        let out = std::process::Command::new("id")
            .args([flag, &name])
            .output()?;
        ensure!(out.status.success(), "runtime identity lookup failed");
        Ok(String::from_utf8(out.stdout)?.trim().parse()?)
    };
    settings.runtime_users.insert(
        id.into(),
        crate::config::RuntimeUser {
            uid: number("-u")?,
            gid: number("-g")?,
        },
    );
    crate::config::atomic_json(&config.home.join("config/bots.json"), &settings, 0o644)?;
    Ok(())
}

fn collect_versions(config: &crate::config::Config) -> Result<()> {
    let base = config.home.join("modules");
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(base.join(".leases.lock"))?;
    crate::runtime::lock(&lock)?;
    let registry = index(&base)?;
    let mut retained = BTreeSet::new();
    for key in ["modules", "previous"] {
        if let Some(values) = registry[key].as_object() {
            for value in values.values() {
                if let Some(path) = value.as_str() {
                    retained.insert(base.join(path).canonicalize()?);
                }
            }
        }
    }
    let leases = config.home.join("run/module-leases");
    if leases.is_dir() {
        for file in std::fs::read_dir(leases)? {
            let file = file?;
            let handle = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(file.path())?;
            if !crate::runtime::try_lock(&handle)? {
                let paths: Vec<String> = serde_json::from_slice(&std::fs::read(file.path())?)?;
                retained.extend(paths.into_iter().map(std::path::PathBuf::from));
            } else {
                std::fs::remove_file(file.path())?;
            }
        }
    }
    let releases = base.join("releases");
    if releases.is_dir() {
        for module in std::fs::read_dir(&releases)? {
            let module = module?;
            ensure!(module.file_type()?.is_dir(), "unexpected release path");
            for version in std::fs::read_dir(module.path())? {
                let version = version?;
                let name = version.file_name();
                let hash = name.to_string_lossy();
                ensure!(
                    version.file_type()?.is_dir()
                        && hash.len() == 64
                        && hash.bytes().all(|b| b.is_ascii_hexdigit()),
                    "unexpected release directory"
                );
                if !retained.contains(&version.path().canonicalize()?) {
                    remove_tree(&version.path())?;
                }
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod drain_tests {
    use super::{data_version_ready, drain_mode, prepare_stop_within};
    use std::time::Duration;

    fn module(home: &std::path::Path, data_version: u32) -> crate::catalog::Catalog {
        let root = home.join("modules/alpha");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("job.sh"), "true\n").unwrap();
        std::fs::write(
            root.join("module.json"),
            serde_json::json!({
                "api_version":2,"data_version":data_version,"id":"alpha",
                "handler":["bash","job.sh"],"workspace":"default",
                "jobs":{"sync":{"name":"sync","entry":["bash","job.sh"],"workspace":"default",
                    "topic":"personal","schedule":"0 * * * *"}}
            })
            .to_string(),
        )
        .unwrap();
        crate::catalog::Catalog::load_from(home.join("modules")).unwrap()
    }

    #[test]
    fn stale_drain_marker_is_taken_over_and_removed() {
        let temp = tempfile::tempdir().unwrap();
        let config = crate::config::Config::new(temp.path().to_path_buf());
        let catalog = module(temp.path(), 1);
        let marker = temp.path().join("run/drain/alpha");
        std::fs::create_dir_all(marker.parent().unwrap()).unwrap();
        std::fs::write(&marker, "left by an interrupted deployment").unwrap();
        let guard = prepare_stop_within(&config, &catalog, "alpha", Duration::from_secs(5))
            .expect("stale marker must not block a new deployment");
        assert_eq!(std::fs::read(&marker).unwrap(), b"");
        drop(guard);
        assert!(!marker.exists());
    }

    #[test]
    fn stop_preparation_times_out_with_a_clear_error() {
        let temp = tempfile::tempdir().unwrap();
        let config = crate::config::Config::new(temp.path().to_path_buf());
        let catalog = module(temp.path(), 1);
        std::fs::create_dir_all(temp.path().join("run")).unwrap();
        let lock = crate::runtime::open_task_lock(&temp.path().join("run/sync.lock")).unwrap();
        assert!(crate::runtime::try_lock(&lock).unwrap());
        let error = match prepare_stop_within(&config, &catalog, "alpha", Duration::from_millis(50))
        {
            Ok(_) => panic!("held task lock must time out"),
            Err(error) => error,
        };
        assert_eq!(
            error.to_string(),
            "module stop preparation timed out after 10 minutes; tasks still active"
        );
        assert!(!temp.path().join("run/drain/alpha").exists());
    }

    #[test]
    fn data_version_change_needs_no_freeze_only_idle_jobs() {
        let temp = tempfile::tempdir().unwrap();
        let before = module(temp.path(), 1).modules["alpha"].manifest.clone();
        let after = module(temp.path(), 2).modules["alpha"].manifest.clone();
        assert!(!temp.path().join("run/freeze").exists());
        data_version_ready(&before, &after, |_| Ok(false), "change").unwrap();
        data_version_ready(&before, &before, |_| Ok(true), "change").unwrap();
        let busy = data_version_ready(
            &before,
            &after,
            |unit| Ok(unit == "pylon-task@sync.service"),
            "change",
        );
        assert_eq!(
            busy.unwrap_err().to_string(),
            "data version change waits for active jobs"
        );
        let busy = data_version_ready(&after, &before, |_| Ok(true), "rollback");
        assert_eq!(
            busy.unwrap_err().to_string(),
            "data version rollback waits for active jobs"
        );
    }

    #[test]
    fn drain_directory_gains_list_access_without_losing_bits() {
        assert_eq!(drain_mode(0o40700), Some(0o2750));
        assert_eq!(drain_mode(0o40750), Some(0o2750));
        assert_eq!(drain_mode(0o42755), None);
        assert_eq!(drain_mode(0o42770), None);
        assert_eq!(drain_mode(0o40755), Some(0o2755));
    }
}
