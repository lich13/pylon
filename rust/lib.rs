pub mod api;
pub mod auth;
pub mod bark;
pub mod broker;
pub mod catalog;
pub mod config;
pub mod daemon;
pub mod db;
pub mod errors;
pub mod migration;
pub mod modules;
pub mod queue;
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
