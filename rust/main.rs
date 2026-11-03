use anyhow::{Context, Result, ensure};
use clap::{Parser, Subcommand};
use pylon::{api, config::Config};
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
    /// Inspect all in-flight work without modifying state; exits 0 only when drained and frozen.
    DrainStatus,
    /// Create run/freeze so new triggers and non-system delivery pause.
    Freeze,
    /// Remove run/freeze after a successful upgrade. Refuses if the marker is absent.
    Thaw,
    /// Inspect compatibility before replacing the gateway with an older binary.
    DowngradeCheck {
        #[arg(long)]
        target_version: String,
    },
    Serve {
        id: String,
    },
    Check,
    Rpc {
        method: String,
        #[arg(long, default_value = "{}")]
        params: String,
    },
    /// Manage bots (writes are root only, backed up and audited).
    Bots {
        #[command(subcommand)]
        action: Bots,
    },
    /// Show or change topic routes. `routes <topic> <bot>` is the old form of
    /// `routes set`.
    #[command(args_conflicts_with_subcommands = true)]
    Routes {
        #[command(subcommand)]
        action: Option<Routes>,
        #[arg(hide = true)]
        topic: Option<String>,
        #[arg(hide = true)]
        bot: Option<String>,
    },
    /// Inspect or resolve deliveries (root only; never prints message bodies).
    Deliveries {
        #[command(subcommand)]
        action: Deliveries,
    },
    /// Operator change backups.
    Ops {
        #[command(subcommand)]
        action: Ops,
    },
    Modules {
        #[command(subcommand)]
        action: Modules,
    },
}
#[derive(Subcommand)]
enum Ops {
    /// Restore bots.json and credentials from a gateway-ops backup. Database
    /// changes are not reverted.
    Restore {
        dir: PathBuf,
        #[arg(long)]
        dry_run: bool,
    },
}
#[derive(clap::Args)]
struct Token {
    /// Read the token from this file instead of stdin.
    #[arg(long)]
    token_file: Option<PathBuf>,
    /// Do not verify the token with Telegram getMe (needs --reason).
    #[arg(long)]
    skip_getme: bool,
    #[arg(long, default_value = "")]
    reason: String,
}
#[derive(Subcommand)]
enum Bots {
    /// Bots with routes, connection and menu state (owners are not shown).
    List,
    /// Register a bot; the token is read from stdin or --token-file.
    Add {
        id: String,
        #[arg(long)]
        owner: String,
        #[arg(long, default_value = "")]
        username: String,
        #[command(flatten)]
        token: Token,
        #[arg(long)]
        dry_run: bool,
    },
    /// Replace a bot token (same bot id); the old token is kept in the backup.
    Rotate {
        id: String,
        #[command(flatten)]
        token: Token,
        /// Wait until the bot polls successfully with the new token.
        #[arg(long)]
        wait_healthy: bool,
        #[arg(long)]
        dry_run: bool,
    },
    /// Enable a bot.
    Enable {
        id: String,
        #[arg(long)]
        dry_run: bool,
    },
    /// Disable a bot; routed topics need --reroute-to.
    Disable {
        id: String,
        #[arg(long, value_enum, default_value = "keep")]
        pending: pylon::ops::Pending,
        #[arg(long)]
        to: Option<String>,
        /// Move topics the bot serves to this bot.
        #[arg(long)]
        reroute_to: Option<String>,
        #[arg(long)]
        dry_run: bool,
    },
    /// Remove a bot; its credential moves into the backup.
    Remove {
        id: String,
        #[arg(long, value_enum)]
        pending: Option<pylon::ops::Pending>,
        #[arg(long)]
        to: Option<String>,
        #[arg(long)]
        reroute_to: Option<String>,
        /// Also delete the bot's runtime state rows.
        #[arg(long)]
        purge_state: bool,
        #[arg(long)]
        dry_run: bool,
    },
    /// Delete runtime state rows of bots that are no longer configured.
    PruneState {
        #[arg(long)]
        dry_run: bool,
    },
    /// Push the command menu again and wait for Telegram to confirm it.
    MenuResync {
        ids: Vec<String>,
        #[arg(long)]
        all: bool,
        #[arg(long)]
        dry_run: bool,
    },
    /// Validate credentials; --online also calls getMe.
    Check {
        #[arg(long)]
        online: bool,
    },
    /// Ask the gateway to load the configuration now.
    Reload,
}
#[derive(Subcommand)]
enum Routes {
    /// Base route, override and the bot serving each topic now.
    List,
    /// Route a topic to an enabled bot.
    Set {
        topic: String,
        bot: String,
        #[arg(long)]
        dry_run: bool,
    },
    /// Remove a topic route.
    Unset {
        topic: String,
        #[arg(long)]
        dry_run: bool,
    },
    /// Temporarily serve a topic from another bot.
    Override {
        topic: String,
        #[arg(long)]
        to: String,
        /// Duration such as 30m, 2h or 1d (60s to 7d).
        #[arg(long = "for")]
        duration: Option<String>,
        /// RFC 3339 end time.
        #[arg(long)]
        until: Option<String>,
        /// Return to the base bot once it has been healthy this long.
        #[arg(long)]
        revert_when_healthy: Option<String>,
        #[arg(long, default_value = "")]
        reason: String,
        #[arg(long)]
        dry_run: bool,
    },
    /// Remove a topic override, or every expired one.
    Clear {
        topic: Option<String>,
        #[arg(long)]
        expired: bool,
        #[arg(long)]
        dry_run: bool,
    },
}
#[derive(Subcommand)]
enum Deliveries {
    /// List delivery metadata by status (no payloads or receipts).
    List {
        #[arg(long, default_value = "uncertain")]
        status: String,
        #[arg(long, default_value_t = 50)]
        limit: usize,
    },
    /// Mark an uncertain delivery as delivered after checking Telegram.
    Confirm {
        id: String,
        #[arg(long)]
        dry_run: bool,
    },
    /// Mark an uncertain delivery as suppressed; nothing is sent.
    Discard {
        id: String,
        #[arg(long)]
        dry_run: bool,
    },
    /// Return an uncertain delivery to pending once; may duplicate a message.
    Resend {
        id: String,
        #[arg(long)]
        accept_duplicate_risk: bool,
        #[arg(long)]
        dry_run: bool,
    },
    /// Move pending deliveries of a removed or disabled bot to an enabled bot
    /// that delivers to the same chat. All selected items move or none do.
    Retarget {
        /// Delivery ids (alternatively use --from-bot).
        ids: Vec<String>,
        /// Select every pending delivery of this bot.
        #[arg(long)]
        from_bot: Option<String>,
        /// Configured, enabled bot that will deliver them.
        #[arg(long)]
        to: String,
        #[arg(long)]
        dry_run: bool,
    },
    /// Suppress every pending delivery of a bot.
    PurgeBot {
        bot: String,
        #[arg(long)]
        discard: bool,
        #[arg(long)]
        dry_run: bool,
    },
    /// Hold new delivery attempts of a bot for a while (60s to 7d).
    Defer {
        bot: String,
        #[arg(long = "for")]
        duration: String,
        #[arg(long)]
        dry_run: bool,
    },
    /// Lift a deferral early.
    Undefer {
        bot: String,
        #[arg(long)]
        dry_run: bool,
    },
}
#[derive(Subcommand)]
enum Modules {
    Check {
        module: Option<String>,
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
    let args = Args::parse();
    if matches!(args.command, Command::DrainStatus) {
        let mut config = Config::new(args.home.clone());
        config.offline = args.offline;
        match pylon::deployment::drain_status(&config) {
            Ok(status) => {
                println!(
                    "{}",
                    serde_json::to_string(&status).expect("drain status serializes")
                );
                std::process::exit(i32::from(!status.ready));
            }
            Err(error) => {
                pylon::errors::log("drain-status", &error);
                std::process::exit(1);
            }
        }
    }
    if let Command::DowngradeCheck { target_version } = &args.command {
        let report = pylon::downgrade::check(&Config::new(args.home.clone()), target_version);
        match serde_json::to_string(&report) {
            Ok(value) => println!("{value}"),
            Err(error) => {
                pylon::errors::log("downgrade-output", &error.into());
                std::process::exit(1);
            }
        }
        std::process::exit(i32::from(!report.ok));
    }
    if let Err(e) = start(args) {
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
        Command::Freeze => {
            ensure!(unsafe { libc::geteuid() } == 0, "freeze requires root");
            let path = config.home.join("run/freeze");
            std::fs::create_dir_all(path.parent().context("run directory missing")?)?;
            let result = if path.exists() {
                ensure!(path.is_file(), "freeze path is not a regular file");
                json!({"ok":true,"freeze":true,"created":false})
            } else {
                use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
                let file = std::fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o660)
                    .open(&path)?;
                file.sync_all()?;
                // Match historical deploy ownership so the gateway group can observe the marker.
                let meta = std::fs::metadata(config.home.join("run"))?;
                let gid = meta.gid();
                std::os::unix::fs::chown(&path, Some(0), Some(gid))?;
                std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o660))?;
                json!({"ok":true,"freeze":true,"created":true})
            };
            std::fs::File::open(path.parent().context("run directory missing")?)?.sync_all()?;
            if config.home.join("supervisor/task-state.sqlite3").is_file() {
                pylon::queue::record_freeze(&config)?;
            }
            result
        }
        Command::Thaw => {
            ensure!(unsafe { libc::geteuid() } == 0, "thaw requires root");
            let path = config.home.join("run/freeze");
            ensure!(path.is_file(), "freeze marker missing");
            std::fs::remove_file(&path)?;
            std::fs::File::open(path.parent().context("run directory missing")?)?.sync_all()?;
            json!({"ok":true,"freeze":false})
        }
        Command::DowngradeCheck { .. } | Command::DrainStatus => {
            anyhow::bail!("invalid command dispatch")
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
            Bots::List => pylon::ops::bots_list(&config)?,
            Bots::Add {
                id,
                owner,
                username,
                token,
                dry_run,
            } => {
                let secret = read_token(&token, dry_run, None)?;
                pylon::ops::preflight_token(&config, &secret.0, None)?;
                let verified = verify(&config, &token, &secret, &username, dry_run).await?;
                let args = json!({"id":id,"owner":owner,"username":username,"skip_getme":token.skip_getme,"reason":token.reason});
                pylon::ops::run(&config, "bots-add", args, dry_run, |ctx| {
                    let mut change = pylon::ops::bots_add(ctx, &id, &owner, &username, &secret.0)?;
                    change.notes.insert("getme".into(), verified);
                    Ok(change)
                })
                .await?
            }
            Bots::Rotate {
                id,
                token,
                wait_healthy,
                dry_run,
            } => {
                let settings = config.settings()?;
                let bot = settings.bots.get(&id).context("unknown bot")?;
                let current = config.token(bot)?;
                let prefix = current.split_once(':').map(|(a, _)| a).unwrap_or("");
                let secret = read_token(&token, dry_run, Some(prefix))?;
                pylon::ops::preflight_token(&config, &secret.0, Some(&id))?;
                let verified = verify(&config, &token, &secret, &bot.username, dry_run).await?;
                let args = json!({"id":id,"skip_getme":token.skip_getme,"reason":token.reason,"wait_healthy":wait_healthy});
                pylon::ops::run(&config, "bots-rotate", args, dry_run, |ctx| {
                    let mut change = pylon::ops::bots_rotate(ctx, &id, &secret.0)?;
                    change.notes.insert("getme".into(), verified);
                    if wait_healthy {
                        change.wait_healthy = vec![id.clone()];
                    }
                    Ok(change)
                })
                .await?
            }
            Bots::Enable { id, dry_run } => {
                pylon::ops::run(&config, "bots-enable", json!({"id":id}), dry_run, |ctx| {
                    pylon::ops::bots_enable(ctx, &id)
                })
                .await?
            }
            Bots::Disable {
                id,
                pending,
                to,
                reroute_to,
                dry_run,
            } => {
                let args = json!({"id":id,"pending":format!("{pending:?}").to_lowercase(),"to":to,"reroute_to":reroute_to});
                pylon::ops::run(&config, "bots-disable", args, dry_run, |ctx| {
                    pylon::ops::bots_disable(
                        ctx,
                        &id,
                        pending,
                        to.as_deref(),
                        reroute_to.as_deref(),
                    )
                })
                .await?
            }
            Bots::Remove {
                id,
                pending,
                to,
                reroute_to,
                purge_state,
                dry_run,
            } => {
                let args = json!({"id":id,"pending":pending.map(|p| format!("{p:?}").to_lowercase()),"to":to,"reroute_to":reroute_to,"purge_state":purge_state});
                pylon::ops::run(&config, "bots-remove", args, dry_run, |ctx| {
                    pylon::ops::bots_remove(
                        ctx,
                        &id,
                        pending,
                        to.as_deref(),
                        reroute_to.as_deref(),
                        purge_state,
                    )
                })
                .await?
            }
            Bots::PruneState { dry_run } => {
                pylon::ops::run(&config, "bots-prune-state", json!({}), dry_run, |ctx| {
                    pylon::ops::bots_prune_state(ctx)
                })
                .await?
            }
            Bots::MenuResync { ids, all, dry_run } => {
                let args = json!({"ids":ids,"all":all});
                pylon::ops::run(&config, "bots-menu-resync", args, dry_run, |ctx| {
                    pylon::ops::bots_menu_resync(ctx, &ids, all)
                })
                .await?
            }
            Bots::Check { online } => {
                let settings = config.settings()?;
                config.validate_credentials(&settings)?;
                let mut failed = false;
                // One line per bot; a failing bot never hides the others.
                for (id, bot) in settings.bots.iter().filter(|(_, b)| b.enabled) {
                    let mut line = json!({"id":id,"enabled":true,"verified":false,"error_class":null,
                        "http_status":null,"telegram_error_code":null,"elapsed_ms":null});
                    if online {
                        let started = std::time::Instant::now();
                        let result = pylon::telegram::Telegram::new(&config, bot)?
                            .call("getMe", &json!({}), true)
                            .await;
                        line["elapsed_ms"] = json!(started.elapsed().as_millis() as u64);
                        match result {
                            Ok(value)
                                if bot.username.is_empty() || value["username"] == bot.username =>
                            {
                                line["verified"] = json!(true)
                            }
                            Ok(_) => {
                                failed = true;
                                line["error_class"] = json!("username_mismatch");
                            }
                            Err(failure) => {
                                failed = true;
                                line["error_class"] = json!(if failure.class.is_empty() {
                                    "invalid_response"
                                } else {
                                    failure.class
                                });
                                line["http_status"] = json!(failure.http_status);
                                line["telegram_error_code"] = json!(failure.telegram_error_code);
                            }
                        }
                    }
                    println!("{line}");
                }
                if failed {
                    std::process::exit(1)
                }
                Value::Null
            }
            Bots::Reload => {
                let s = config.settings()?;
                config.validate_credentials(&s)?;
                api::rpc(&config, "health", json!({}), "").await?;
                match pylon::ops::reload(&config, &s).await? {
                    "applied" => json!({"ok":true,"reload":"applied"}),
                    _ => json!({"ok":true,"reload":"configuration will apply within five seconds"}),
                }
            }
        },
        Command::Routes { action, topic, bot } => match action {
            None => {
                let (Some(topic), Some(bot)) = (topic, bot) else {
                    anyhow::bail!("unknown topic")
                };
                pylon::ops::run(
                    &config,
                    "routes-set",
                    json!({"topic":topic,"bot":bot}),
                    false,
                    |ctx| pylon::ops::routes_set(ctx, &topic, &bot),
                )
                .await?
            }
            Some(Routes::List) => pylon::ops::routes_list(&config)?,
            Some(Routes::Set {
                topic,
                bot,
                dry_run,
            }) => {
                pylon::ops::run(
                    &config,
                    "routes-set",
                    json!({"topic":topic,"bot":bot}),
                    dry_run,
                    |ctx| pylon::ops::routes_set(ctx, &topic, &bot),
                )
                .await?
            }
            Some(Routes::Unset { topic, dry_run }) => {
                pylon::ops::run(
                    &config,
                    "routes-unset",
                    json!({"topic":topic}),
                    dry_run,
                    |ctx| pylon::ops::routes_unset(ctx, &topic),
                )
                .await?
            }
            Some(Routes::Override {
                topic,
                to,
                duration,
                until,
                revert_when_healthy,
                reason,
                dry_run,
            }) => {
                let args = json!({"topic":topic,"to":to,"for":duration,"until":until,
                    "revert_when_healthy":revert_when_healthy,"reason":reason});
                pylon::ops::run(&config, "routes-override", args, dry_run, |ctx| {
                    pylon::ops::routes_override(
                        ctx,
                        &topic,
                        &pylon::ops::OverrideArgs {
                            to: &to,
                            duration: duration.as_deref(),
                            until: until.as_deref(),
                            revert_when_healthy: revert_when_healthy.as_deref(),
                            reason: &reason,
                        },
                        pylon::now(),
                    )
                })
                .await?
            }
            Some(Routes::Clear {
                topic,
                expired,
                dry_run,
            }) => {
                let args = json!({"topic":topic,"expired":expired});
                pylon::ops::run(&config, "routes-clear", args, dry_run, |ctx| {
                    pylon::ops::routes_clear(ctx, topic.as_deref(), expired, pylon::now())
                })
                .await?
            }
        },
        Command::Ops { action } => match action {
            Ops::Restore { dir, dry_run } => {
                let args = json!({"dir":dir.display().to_string()});
                pylon::ops::run(&config, "ops-restore", args, dry_run, |ctx| {
                    pylon::ops::restore(ctx, &dir)
                })
                .await?
            }
        },
        Command::Deliveries { action } => {
            ensure!(
                unsafe { libc::geteuid() } == 0 || config.offline,
                "deliveries requires root"
            );
            let path = config.db();
            ensure!(path.is_file(), "gateway state missing");
            let store = pylon::db::Store::new(&path)?;
            let actor = format!("uid:{}", unsafe { libc::geteuid() });
            let preview = |id: &str, action: &str| -> Result<Value> {
                let event = store.event(id)?;
                Ok(
                    json!({"ok":true,"dry_run":true,"action":action,"backup":null,
                    "delivery":{"id":event["id"],"bot":event["profile"],"module":event["module"],
                        "status":event["status"],"attempts":event["attempts"],"updated_at":event["updated_at"],
                        "has_receipt":!event["receipt"].is_null()}}),
                )
            };
            match action {
                Deliveries::List { status, limit } => {
                    let rows = store.deliveries(&status, limit)?;
                    json!({"status":status,"count":rows.len(),"deliveries":rows})
                }
                Deliveries::Confirm { id, dry_run: true } => preview(&id, "confirm")?,
                Deliveries::Discard { id, dry_run: true } => preview(&id, "discard")?,
                Deliveries::Confirm { id, .. } => store.resolve_delivery(&id, "confirm", &actor)?,
                Deliveries::Discard { id, .. } => store.resolve_delivery(&id, "discard", &actor)?,
                Deliveries::Resend {
                    id,
                    accept_duplicate_risk,
                    dry_run,
                } => {
                    if dry_run {
                        preview(&id, "resend")?
                    } else {
                        ensure!(
                            accept_duplicate_risk,
                            "resend can duplicate a message; pass --accept-duplicate-risk"
                        );
                        store.resolve_delivery(&id, "resend", &actor)?
                    }
                }
                Deliveries::Retarget {
                    ids,
                    from_bot,
                    to,
                    dry_run,
                } => {
                    let args = json!({"ids":ids,"from_bot":from_bot,"to":to});
                    let mut value =
                        pylon::ops::run(&config, "deliveries-retarget", args, dry_run, |ctx| {
                            pylon::ops::deliveries_retarget(ctx, &ids, from_bot.as_deref(), &to)
                        })
                        .await?;
                    if !dry_run {
                        value["to"] = json!(to);
                        value["count"] = value["database"]["retargeted"].clone();
                        value["deliveries"] = value["database"]["deliveries"].take();
                    }
                    value
                }
                Deliveries::PurgeBot {
                    bot,
                    discard,
                    dry_run,
                } => {
                    pylon::ops::run(
                        &config,
                        "deliveries-purge-bot",
                        json!({"bot":bot}),
                        dry_run,
                        |ctx| pylon::ops::deliveries_purge_bot(ctx, &bot, discard),
                    )
                    .await?
                }
                Deliveries::Defer {
                    bot,
                    duration,
                    dry_run,
                } => {
                    let args = json!({"bot":bot,"for":duration});
                    pylon::ops::run(&config, "deliveries-defer", args, dry_run, |ctx| {
                        pylon::ops::deliveries_defer(ctx, &bot, &duration, pylon::now())
                    })
                    .await?
                }
                Deliveries::Undefer { bot, dry_run } => {
                    pylon::ops::run(
                        &config,
                        "deliveries-undefer",
                        json!({"bot":bot}),
                        dry_run,
                        |ctx| pylon::ops::deliveries_undefer(ctx, &bot),
                    )
                    .await?
                }
            }
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
                    Modules::Deploy {
                        module,
                        host,
                        directory,
                    } => {
                        // Packs locally with this binary; no CI artifacts are involved.
                        let binary = std::env::current_exe()?;
                        developer_tool(
                            directory,
                            "deploy",
                            &[
                                module,
                                "--host".into(),
                                host,
                                "--binary".into(),
                                binary.to_string_lossy().into_owned(),
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

/// Token text and whether it was supplied. A dry run without a token uses a
/// well-formed placeholder so the preview still shows the change.
fn read_token(token: &Token, dry_run: bool, prefix: Option<&str>) -> Result<(String, bool)> {
    use std::io::{IsTerminal, Read};
    let text = match &token.token_file {
        Some(path) => {
            let mut text = String::new();
            std::fs::File::open(path)?
                .take(4096)
                .read_to_string(&mut text)?;
            text.trim().to_owned()
        }
        None if dry_run => {
            let mut text = String::new();
            if !std::io::stdin().is_terminal() {
                std::io::stdin().take(4096).read_to_string(&mut text)?;
            }
            text.trim().to_owned()
        }
        None => pylon::config::credential_input()?,
    };
    if text.is_empty() {
        ensure!(dry_run, "credential required on stdin");
        let id = prefix.filter(|p| !p.is_empty()).unwrap_or("1000000000");
        return Ok((format!("{id}:{}", "x".repeat(35)), false));
    }
    Ok((text, true))
}

async fn verify(
    config: &Config,
    token: &Token,
    secret: &(String, bool),
    username: &str,
    dry_run: bool,
) -> Result<Value> {
    if token.skip_getme {
        ensure!(
            !token.reason.trim().is_empty(),
            "skipping getMe needs --reason"
        );
        return Ok(json!({"skipped":true}));
    }
    if dry_run && !secret.1 {
        return Ok(json!({"skipped":true,"credential":"not supplied"}));
    }
    pylon::ops::verify_token(config, &secret.0, username).await
}
