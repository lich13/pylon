pub mod api;
pub mod auth;
pub mod bark;
pub mod broker;
pub mod catalog;
mod completion;
pub mod config;
pub mod daemon;
pub mod db;
pub mod deployment;
pub mod downgrade;
pub mod errors;
mod event_history;
mod lock_reaper;
pub mod migration;
pub mod modules;
mod optimization;
pub mod queue;
mod reload;
mod retention;
pub mod runtime;
pub mod telegram;

pub(crate) fn child_identity(command: &mut std::process::Command, uid: u32, gid: u32) {
    use std::os::unix::process::CommandExt;
    // Command's uid/gid setters run before pre_exec. Drop supplementary groups
    // while still privileged, then permanently change the primary GID and UID.
    unsafe {
        command.pre_exec(move || {
            if libc::setgroups(0, std::ptr::null()) != 0
                || libc::setgid(gid) != 0
                || libc::setuid(uid) != 0
            {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
}

pub fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

pub fn digest(value: impl AsRef<[u8]>) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(value.as_ref()))
}

pub fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 80
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
}

/// Bound blocking work without occupying Tokio's network executor threads.
pub async fn blocking<T: Send + 'static>(
    operation: impl FnOnce() -> anyhow::Result<T> + Send + 'static,
) -> anyhow::Result<T> {
    static SLOTS: std::sync::OnceLock<std::sync::Arc<tokio::sync::Semaphore>> =
        std::sync::OnceLock::new();
    let permit = SLOTS
        .get_or_init(|| std::sync::Arc::new(tokio::sync::Semaphore::new(32)))
        .clone()
        .acquire_owned()
        .await?;
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        operation()
    })
    .await
    .map_err(|_| anyhow::Error::from(errors::ReconcileRequired))?
}

/// A request owns its blocking thread through async waits, so synchronous SQLite and
/// filesystem operations cannot stall the network runtime. Cancellation never replays it.
pub async fn isolated<T: Send + 'static>(
    future: impl std::future::Future<Output = anyhow::Result<T>> + Send + 'static,
) -> anyhow::Result<T> {
    let runtime = tokio::runtime::Handle::current();
    static REQUESTS: std::sync::OnceLock<std::sync::Arc<tokio::sync::Semaphore>> =
        std::sync::OnceLock::new();
    let permit = REQUESTS
        .get_or_init(|| std::sync::Arc::new(tokio::sync::Semaphore::new(64)))
        .clone()
        .acquire_owned()
        .await?;
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        runtime.block_on(future)
    })
    .await
    .map_err(|_| anyhow::Error::from(errors::ReconcileRequired))?
}

/// Public RPC admission is separate from executor work and bot delivery.
pub async fn rpc_isolated<T: Send + 'static>(
    future: impl std::future::Future<Output = anyhow::Result<T>> + Send + 'static,
) -> anyhow::Result<T> {
    static SLOTS: std::sync::OnceLock<std::sync::Arc<tokio::sync::Semaphore>> =
        std::sync::OnceLock::new();
    let slots = SLOTS.get_or_init(|| std::sync::Arc::new(tokio::sync::Semaphore::new(64)));
    let permit = tokio::time::timeout(
        std::time::Duration::from_secs(1),
        slots.clone().acquire_owned(),
    )
    .await
    .map_err(|_| errors::RemoteFailure { retryable: true })??;
    let runtime = tokio::runtime::Handle::current();
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        runtime.block_on(future)
    })
    .await
    .map_err(|_| anyhow::Error::from(errors::ReconcileRequired))?
}
