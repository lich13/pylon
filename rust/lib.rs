pub mod api;
pub mod auth;
pub mod broker;
pub mod catalog;
pub mod config;
pub mod daemon;
pub mod db;
pub mod migration;
pub mod modules;
pub mod runtime;
pub mod telegram;

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
