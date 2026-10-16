use anyhow::{Context, Result, ensure};
use clap::{Parser, Subcommand};
use pylon::{
    api,
    config::{Bot, Config, atomic_json},
};
use serde_json::{Value, json};
use std::path::PathBuf;

#[derive(Parser)]
#[command(version, about = "Pylon gateway and extension runtime")]
struct Args {
    #[arg(long, env = "PYLON_HOME", default_value = "/srv/pylon")]
    home: PathBuf,
    #[arg(long, hide = true)]
    offline: bool,
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    Gateway,
    Executor,
    Broker,
    Execute {
        id: String,
    },
    #[command(hide = true)]
    Resume {
        id: String,
        #[arg(long, hide = true)]
        dispatch_fd: Option<i32>,
    },
    Diagnose,
    Serve {
        id: String,
    },
    Check,
    Rpc {
        method: String,
        #[arg(long, default_value = "{}")]
        params: String,
    },
    Bots {
        #[command(subcommand)]
        action: Bots,
    },
    Routes {
        topic: String,
        bot: String,
    },
    Modules {
        #[command(subcommand)]
        action: Modules,
    },
    Migrate {
        #[arg(long)]
        source: PathBuf,
        #[arg(long)]
        mapping: PathBuf,
    },
}
#[derive(Subcommand)]
enum Bots {
    Add {
        id: String,
        #[arg(long)]
        owner: String,
        #[arg(long, default_value = "")]
        username: String,
    },
    Check {
        #[arg(long)]
        online: bool,
    },
    Reload,
    Disable {
        id: String,
    },
}
#[derive(Subcommand)]
enum Modules {
    Check {
        module: Option<String>,
        #[arg(long)]
        directory: Option<PathBuf>,
    },
    Preview {
        module: String,
        #[arg(long, default_value_t = 8795)]
        port: u16,
        #[arg(long)]
        directory: Option<PathBuf>,
    },
    Deploy {
        module: String,
        #[arg(long)]
        host: String,
        #[arg(long)]
        directory: Option<PathBuf>,
    },
    Ci {
        module: String,
        #[arg(long)]
        gateway: PathBuf,
        #[arg(long)]
        directory: Option<PathBuf>,
    },
    Exec {
        module: String,
        alias: String,
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<String>,
    },
    Render {
        destination: PathBuf,
    },
    Pack {
        source: PathBuf,
        #[arg(long)]
        output: PathBuf,
    },
    Install {
        archive: PathBuf,
        #[arg(long)]
        sha256: String,
        #[arg(long)]
        activate: bool,
    },
    Rollback {
        id: String,
        #[arg(long)]
        activate: bool,
    },
}
fn main() {
    if let Err(e) = start(Args::parse()) {
        pylon::errors::log("cli", &e);
        eprintln!("{}", pylon::errors::cli_message(&e));
        std::process::exit(1)
    }
}
fn start(args: Args) -> Result<()> {
    let gateway = matches!(args.command, Command::Gateway);
    // Keep the supervisor lock outside the runtime until all bounded shutdown
    // work has finished. Other commands retain their normal runtime lifetime.
    let lock = if gateway {
        Some(pylon::daemon::acquire_lock(&Config::new(
            args.home.clone(),
        ))?)
    } else {
        None
    };
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    let result = runtime.block_on(run(args, lock.as_ref()));
    if gateway {
        runtime.shutdown_timeout(std::time::Duration::from_secs(2));
    } else {
        drop(runtime);
    }
    result
}
async fn run(args: Args, lock: Option<&std::fs::File>) -> Result<()> {
    let mut config = Config::new(args.home);
    config.offline = args.offline;
    let result = match args.command {
        Command::Gateway => {
            pylon::daemon::run(
                api::App::new(config)?,
                lock.context("gateway lock missing")?,
            )
            .await?;
            Value::Null
        }
        Command::Executor => {
            pylon::runtime::executor(config).await?;
            Value::Null
        }
        Command::Broker => {
            pylon::broker::serve(config).await?;
            Value::Null
        }
        Command::Execute { id } => {
            let code = pylon::runtime::execute(&config, &id).await?;
            if code != 0 {
                std::process::exit(code)
            }
            Value::Null
        }
        Command::Resume { id, dispatch_fd } => {
            let _dispatch = pylon::runtime::inherited_dispatch_lock(&config, &id, dispatch_fd)?;
            let code = pylon::runtime::resume(&config, &id).await?;
            if code != 0 {
                std::process::exit(code)
            }
            Value::Null
        }
        Command::Diagnose => {
            api::rpc(
                &config,
                "diagnostics",
                json!({}),
                &std::env::var("PYLON_CONTEXT").unwrap_or_default(),
            )
            .await?
        }
        Command::Serve { id } => {
            let code = pylon::runtime::service(&config, &id).await?;
            if code != 0 {
                std::process::exit(code)
            }
            Value::Null
        }
        Command::Check => {
            let s = config.settings()?;
            config.validate_credentials(&s)?;
            let c = pylon::catalog::Catalog::load(&config)?;
            json!({"ok":true,"version":env!("CARGO_PKG_VERSION"),"bots":s.bots.keys().collect::<Vec<_>>(),"modules":c.modules.keys().collect::<Vec<_>>()})
        }
        Command::Rpc { method, params } => {
            api::rpc(
                &config,
                &method,
                serde_json::from_str(&params)?,
                &std::env::var("PYLON_CONTEXT").unwrap_or_default(),
            )
            .await?
        }
        Command::Bots { action } => match action {
            Bots::Add {
                id,
                owner,
                username,
            } => {
                ensure!(
                    pylon::identifier(&id)
                        && !owner.is_empty()
                        && owner.bytes().all(|b| b.is_ascii_digit()),
                    "invalid bot identity"
                );
                let mut settings = config.settings()?;
                ensure!(!settings.bots.contains_key(&id), "bot already registered");
                let token = pylon::config::credential_input()?;
                let credential = format!("bot-{id}");
                let path = config.home.join("gateway/secrets").join(&credential);
                std::fs::create_dir_all(path.parent().context("credential parent missing")?)?;
                use std::io::Write;
                use std::os::unix::fs::OpenOptionsExt;
                let mut file = std::fs::OpenOptions::new()
                    .create_new(true)
                    .write(true)
                    .mode(0o600)
                    .open(&path)?;
                file.write_all(token.as_bytes())?;
                file.sync_all()?;
                if unsafe { libc::geteuid() } == 0 {
                    use std::os::unix::fs::MetadataExt;
                    let parent =
                        std::fs::metadata(path.parent().context("credential parent missing")?)?;
                    std::os::unix::fs::chown(&path, Some(parent.uid()), Some(parent.gid()))?;
                }
                settings.bots.insert(
                    id.clone(),
                    Bot {
                        require_start: true,
                        owner,
                        credential,
                        username,
                        enabled: true,
                        menus: vec![],
                    },
                );
                if let Err(e) = config.validate_credentials(&settings) {
                    std::fs::remove_file(path)?;
                    return Err(e);
                }
                atomic_json(&config.home.join("config/bots.json"), &settings, 0o644)?;
                json!({"ok":true,"bot":id})
            }
            Bots::Check { online } => {
                let settings = config.settings()?;
                config.validate_credentials(&settings)?;
                let mut bots = vec![];
                for (id, bot) in settings.bots.iter().filter(|(_, b)| b.enabled) {
                    let verified = if online {
                        let value = pylon::telegram::Telegram::new(&config, bot)?
                            .call("getMe", &json!({}), true)
                            .await?;
                        ensure!(
                            bot.username.is_empty() || value["username"] == bot.username,
                            "bot username does not match configured credential"
                        );
                        true
                    } else {
                        false
                    };
                    bots.push(json!({"id":id,"enabled":true,"verified":verified}));
                }
                json!(bots)
            }
            Bots::Reload => {
                let s = config.settings()?;
                config.validate_credentials(&s)?;
                api::rpc(&config, "health", json!({}), "").await?;
                json!({"ok":true,"reload":"configuration will apply within five seconds"})
            }
            Bots::Disable { id } => {
                let mut s = config.settings()?;
                s.bots.get_mut(&id).context("unknown bot")?.enabled = false;
                atomic_json(&config.home.join("config/bots.json"), &s, 0o644)?;
                json!({"ok":true,"bot":id,"enabled":false})
            }
        },
        Command::Routes { topic, bot } => {
            let mut s = config.settings()?;
            ensure!(s.bots.contains_key(&bot), "unknown bot");
            ensure!(pylon::identifier(&topic), "invalid topic");
            s.routes.insert(topic, bot);
            atomic_json(&config.home.join("config/bots.json"), &s, 0o644)?;
            json!({"ok":true})
        }
        Command::Modules { action } => {
            pylon::isolated(async move {
                Ok(match action {
                    Modules::Check { module, directory } => {
                        if let Some(name) = module {
                            developer_tool(directory, "check", &[name])?;
                            json!({"ok":true})
                        } else {
                            pylon::modules::check(
                                &directory.unwrap_or_else(|| config.home.join("modules")),
                            )?
                        }
                    }
                    Modules::Preview {
                        module,
                        port,
                        directory,
                    } => {
                        developer_tool(
                            directory,
                            "preview",
                            &[module, "--port".into(), port.to_string()],
                        )?;
                        Value::Null
                    }
                    Modules::Deploy {
                        module,
                        host,
                        directory,
                    } => {
                        developer_tool(
                            directory,
                            "deploy",
                            &[
                                module,
                                "--host".into(),
                                host,
                                "--binary".into(),
                                std::env::current_exe()?.to_string_lossy().into_owned(),
                            ],
                        )?;
                        Value::Null
                    }
                    Modules::Ci {
                        module,
                        gateway,
                        directory,
                    } => {
                        developer_tool(
                            directory,
                            "ci",
                            &[
                                module,
                                "--gateway".into(),
                                gateway.to_string_lossy().into_owned(),
                            ],
                        )?;
                        Value::Null
                    }
                    Modules::Exec {
                        module,
                        alias,
                        args,
                    } => {
                        let code =
                            pylon::runtime::module_exec(&config, &module, &alias, &args).await?;
                        if code != 0 {
                            std::process::exit(code)
                        }
                        Value::Null
                    }
                    Modules::Render { destination } => {
                        pylon::modules::render(&config, &destination)?;
                        json!({"ok":true})
                    }
                    Modules::Pack { source, output } => pylon::modules::pack(&source, &output)?,
                    Modules::Install {
                        archive,
                        sha256,
                        activate,
                    } => {
                        pylon::modules::change(&config, Some((&archive, &sha256)), None, activate)?
                    }
                    Modules::Rollback { id, activate } => {
                        pylon::modules::change(&config, None, Some(&id), activate)?
                    }
                })
            })
            .await?
        }
        Command::Migrate { source, mapping } => {
            pylon::migration::import(&config, &source, &mapping)?
        }
    };
    if !result.is_null() {
        println!("{}", serde_json::to_string(&result)?)
    }
    Ok(())
}

fn developer_tool(directory: Option<PathBuf>, action: &str, args: &[String]) -> Result<()> {
    let source = directory
        .or_else(|| std::env::var_os("PYLON_MODULES_DIR").map(PathBuf::from))
        .map(Ok)
        .unwrap_or_else(|| std::env::current_dir().map(|p| p.join("../pylon-tasks")))?
        .canonicalize()?;
    let python = std::env::var_os("PYLON_TASK_PYTHON")
        .map(PathBuf::from)
        .unwrap_or(
            source
                .parent()
                .context("module source has no parent")?
                .join("pylon/.venv/bin/python"),
        );
    let status = std::process::Command::new(python)
        .env("PYTHONPATH", source.join("sdk/src"))
        .args(["-m", "pylon_sdk.tools", "--source"])
        .arg(source)
        .arg(action)
        .args(args)
        .status()?;
    ensure!(status.success(), "module operation failed");
    Ok(())
}
