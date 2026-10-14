use anyhow::{Result, ensure};
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
fn source_files(root: &Path) -> Result<Vec<std::path::PathBuf>> {
    fn visit(root: &Path, dir: &Path, out: &mut Vec<std::path::PathBuf>) -> Result<()> {
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
                visit(root, &path, out)?
            } else if meta.is_file() {
                out.push(relative.to_path_buf());
            }
        }
        Ok(())
    }
    let mut files = vec![];
    visit(root, root, &mut files)?;
    files.sort();
    Ok(files)
}
pub fn content_hash(root: &Path) -> Result<String> {
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
    for module in catalog.modules.values() {
        let entries = std::iter::once(&module.manifest.handler)
            .chain(module.manifest.jobs.values().map(|j| &j.entry))
            .chain(module.manifest.services.values().map(|s| &s.entry))
            .chain(module.manifest.cli.values());
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
    use fs2::FileExt;
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
    lock.lock_exclusive()?;
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
            crate::catalog::Catalog::load_index(base.clone(), Some(registry.clone()))?
                .validate_settings(&config.settings()?)?;
            if let Some(path) = previous.as_str() {
                let old: crate::catalog::Manifest =
                    serde_json::from_slice(&std::fs::read(base.join(path).join("module.json"))?)?;
                if old.data_version != manifest.data_version {
                    ensure!(
                        config.home.join("run/freeze").is_file(),
                        "data version changes require frozen triggers and drained tasks"
                    );
                    for job in old.jobs.keys() {
                        let status = std::process::Command::new("/usr/bin/systemctl")
                            .args(["is-active", "--quiet", &format!("pylon-task@{job}.service")])
                            .status()?;
                        ensure!(
                            !status.success(),
                            "data version change waits for active jobs"
                        );
                    }
                }
            }
            ensure_runtime(config, &manifest.id)?;
            health_candidate(
                config,
                &crate::catalog::Catalog::load_index(base.clone(), Some(registry.clone()))?,
                &manifest.id,
            )?;
        }
        if !destination.exists() {
            std::fs::create_dir_all(destination.parent().unwrap())?;
            std::fs::rename(&staging, &destination)?;
        }
        registry["modules"][&manifest.id] = serde_json::json!(relative);
        let changed = previous != registry["modules"][&manifest.id];
        if !previous.is_null() && changed {
            registry["previous"][&manifest.id] = previous;
        }
        crate::config::atomic_json(&base.join("registry.json"), &registry, 0o644)?;
        Ok(
            serde_json::json!({"id":manifest.id,"hash":hash,"services":manifest.services.keys().collect::<Vec<_>>(),"changed":changed}),
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
    use fs2::FileExt;
    ensure!(crate::identifier(id), "invalid module");
    let base = config.home.join("modules");
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(base.join(".leases.lock"))?;
    lock.lock_exclusive()?;
    let mut registry = index(&base)?;
    let previous = registry["previous"][id].clone();
    ensure!(previous.is_string(), "no previous module version");
    let current = registry["modules"][id].clone();
    registry["modules"][id] = previous;
    registry["previous"][id] = current.clone();
    validate_index(&base, &registry)?;
    if health {
        crate::catalog::Catalog::load_index(base.clone(), Some(registry.clone()))?
            .validate_settings(&config.settings()?)?;
        let original: crate::catalog::Manifest = serde_json::from_slice(&std::fs::read(
            base.join(current.as_str().unwrap()).join("module.json"),
        )?)?;
        let candidate: crate::catalog::Manifest = serde_json::from_slice(&std::fs::read(
            base.join(registry["modules"][id].as_str().unwrap())
                .join("module.json"),
        )?)?;
        if original.data_version != candidate.data_version {
            ensure!(
                config.home.join("run/freeze").is_file(),
                "data version rollback requires frozen triggers and drained tasks"
            );
            for job in original.jobs.keys() {
                ensure!(
                    !std::process::Command::new("/usr/bin/systemctl")
                        .args(["is-active", "--quiet", &format!("pylon-task@{job}.service")])
                        .status()?
                        .success(),
                    "data version rollback waits for active jobs"
                );
            }
        }

        health_candidate(
            config,
            &crate::catalog::Catalog::load_index(base.clone(), Some(registry.clone()))?,
            id,
        )?;
    }
    crate::config::atomic_json(&base.join("registry.json"), &registry, 0o644)?;
    Ok(serde_json::json!({"id":id,"rolled_back":true}))
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
                "After=pylon-gateway.service\nRequires=pylon-gateway.service\n"
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
    use std::{
        io::Write,
        os::unix::{fs::PermissionsExt, process::CommandExt},
        process::Stdio,
        time::{Duration, Instant},
    };
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
            std::fs::create_dir_all(root.join(&workspace.root))?;
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
        let output = root.join("health.json");
        cmd.current_dir(&root)
            .uid(user.uid)
            .gid(user.gid)
            .stdin(Stdio::piped())
            .stdout(std::fs::File::create(&output)?)
            .stderr(Stdio::null());
        unsafe {
            cmd.pre_exec(|| {
                if libc::setgroups(0, std::ptr::null()) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = cmd.spawn()?;
        child.stdin.take().unwrap().write_all(serde_json::json!({"api_version":2,"action":"health","profile":module.manifest.workspace,"payload":{}}).to_string().as_bytes())?;
        let deadline = Instant::now() + Duration::from_secs(60);
        loop {
            if let Some(status) = child.try_wait()? {
                ensure!(status.success(), "module health check failed");
                break;
            }
            if Instant::now() >= deadline {
                child.kill()?;
                child.wait()?;
                anyhow::bail!("module health check timed out")
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        ensure!(
            std::fs::metadata(&output)?.len() < 1024 * 1024,
            "module health response too large"
        );
        let value: serde_json::Value = serde_json::from_slice(&std::fs::read(&output)?)?;
        ensure!(
            value["api_version"] == 2 && value["value"]["status"] == "ok",
            "module health check rejected"
        );
        Ok(())
    })();
    remove_tree(&root)?;
    outcome
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
                config.home.to_str().unwrap(),
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
    use fs2::FileExt;
    let base = config.home.join("modules");
    std::fs::create_dir_all(&base)?;
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(base.join(".deploy.lock"))?;
    lock.lock_exclusive()?;
    let original = index(&base)?;
    let old = crate::catalog::Catalog::load_from(base.clone())?;
    if live {
        ensure!(
            unsafe { libc::geteuid() } == 0 && !config.offline,
            "live deployment requires the root service manager"
        );
    }
    let result = if let Some((archive, sha)) = archive {
        install(config, archive, sha, live)?
    } else {
        rollback(
            config,
            undo.ok_or_else(|| anyhow::anyhow!("module required"))?,
            live,
        )?
    };
    let id = result["id"].as_str().unwrap();
    if live {
        let next = crate::catalog::Catalog::load(config)?;
        let applied =
            (|| -> Result<()> {
                if old.modules.get(id).is_some_and(|m| {
                    m.manifest.data_version != next.modules[id].manifest.data_version
                }) {
                    for job in old.modules[id].manifest.jobs.keys() {
                        let status = std::process::Command::new("/usr/bin/systemctl")
                            .args(["is-active", "--quiet", &format!("pylon-task@{job}.service")])
                            .status()?;
                        ensure!(
                            !status.success(),
                            "data version change waits for active jobs"
                        );
                    }
                }
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
                            config.home.to_str().unwrap(),
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
    use fs2::FileExt;
    let base = config.home.join("modules");
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(base.join(".leases.lock"))?;
    lock.lock_exclusive()?;
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
            if handle.try_lock_exclusive().is_err() {
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
