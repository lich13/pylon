use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};
use tokio::sync::watch;

#[derive(Default)]
pub(crate) struct Metrics {
    pub reload_full: AtomicU64,
    pub reload_skipped: AtomicU64,
    pub completion_scans: AtomicU64,
    pub delivery_empty_claims: AtomicU64,
}
impl Metrics {
    pub fn snapshot(&self) -> Value {
        json!({
            "reload_full": self.reload_full.load(Ordering::Relaxed),
            "reload_skipped": self.reload_skipped.load(Ordering::Relaxed),
            "completion_scans": self.completion_scans.load(Ordering::Relaxed),
            "delivery_empty_claims": self.delivery_empty_claims.load(Ordering::Relaxed),
        })
    }
}

pub(crate) struct Wake(watch::Sender<u64>);
impl Default for Wake {
    fn default() -> Self {
        Self(watch::channel(0).0)
    }
}
impl Wake {
    pub fn subscribe(&self) -> watch::Receiver<u64> {
        self.0.subscribe()
    }
    pub fn notify(&self) {
        self.0.send_modify(|value| *value = value.wrapping_add(1));
    }
}

#[derive(Default)]
pub(crate) struct Runtime {
    pub metrics: Metrics,
    pub completion: Wake,
    deliveries: Mutex<BTreeMap<(String, bool), Weak<Wake>>>,
}
impl Runtime {
    pub fn delivery_listener(&self, bot: &str, bark: bool) -> (Arc<Wake>, watch::Receiver<u64>) {
        let mut deliveries = self.deliveries.lock().unwrap_or_else(|e| e.into_inner());
        deliveries.retain(|_, value| value.strong_count() > 0);
        let entry = deliveries.entry((bot.to_owned(), bark)).or_default();
        let wake = entry.upgrade().unwrap_or_default();
        *entry = Arc::downgrade(&wake);
        let receiver = wake.subscribe();
        (wake, receiver)
    }
    // Notification failure must never change a committed result. Missing listeners
    // claim at startup; other Store instances/processes use the bounded timer.
    pub fn delivery_ready(&self, bot: &str, bark: bool) {
        let wake = self
            .deliveries
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(&(bot.to_owned(), bark))
            .and_then(Weak::upgrade);
        if let Some(wake) = wake {
            wake.notify();
        }
    }
}

#[derive(Default)]
pub(crate) struct Idle {
    step: usize,
}
impl Idle {
    pub fn reset(&mut self) {
        self.step = 0;
    }
    pub fn next(&mut self, intervals: &[Duration]) -> Duration {
        let result = intervals[self.step.min(intervals.len() - 1)];
        self.step = self.step.saturating_add(1);
        result
    }
}

pub(crate) const DELIVERY_IDLE: [Duration; 3] = [
    Duration::from_millis(500),
    Duration::from_secs(1),
    Duration::from_secs(2),
];
pub(crate) const COMPLETION_IDLE: [Duration; 4] = [
    Duration::from_secs(1),
    Duration::from_secs(2),
    Duration::from_secs(5),
    Duration::from_secs(10),
];

#[cfg(test)]
#[path = "optimization_tests.rs"]
mod tests;
