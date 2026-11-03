//! Per-bot Telegram connection state: stable error classes, poll backoff,
//! journal transitions and delivery deferral while a bot is known to fail.
//!
//! Nothing here reads or stores request URLs, tokens, descriptions or bodies.
use serde_json::{Value, json};

/// Every connection error class that can be stored or logged.
pub const CLASSES: &[&str] = &[
    "timeout",
    "dns",
    "tls",
    "connect",
    "http_status",
    "unauthorized",
    "conflict",
    "rate_limited",
    "invalid_response",
];

/// Fields of `connection:<bot>` and diagnose `bots[].connection`.
pub const FIELDS: &[&str] = &[
    "status",
    "at",
    "since",
    "last_ok_at",
    "consecutive_failures",
    "error_class",
    "http_status",
    "telegram_error_code",
    "elapsed_ms",
    "retry_after",
    "next_attempt_at",
];

/// Delivery waits while the last poll failure of these classes is recent.
pub const DEFER_WINDOW: f64 = 120.0;
const BACKOFF_BASE: f64 = 2.0;
const BACKOFF_CAP: f64 = 60.0;
const BLOCKED_DELAY: f64 = 300.0;

/// Classify a transport failure from its type flags and library-owned causes.
/// The caller must strip the URL first; only `source()` causes are inspected.
pub(crate) fn transport_class(error: &reqwest::Error) -> &'static str {
    let mut cause = std::error::Error::source(error);
    let (mut dns, mut tls) = (false, false);
    while let Some(current) = cause {
        let text = current.to_string().to_ascii_lowercase();
        dns |= text.starts_with("dns error")
            || text.contains("failed to lookup address")
            || text.contains("name or service not known")
            || text.contains("temporary failure in name resolution");
        tls |= text.contains("tls") || text.contains("certificate") || text.contains("handshake");
        cause = current.source();
    }
    if dns {
        "dns"
    } else if tls {
        "tls"
    } else if error.is_timeout() {
        "timeout"
    } else {
        "connect"
    }
}

/// Class of a Telegram API `error_code`.
pub(crate) fn api_class(code: u64) -> &'static str {
    match code {
        401 | 404 => "unauthorized",
        409 => "conflict",
        429 => "rate_limited",
        _ => "http_status",
    }
}

/// Uniform random number in [0, 1) from the existing UUID generator.
fn random_unit() -> f64 {
    let bits = uuid::Uuid::new_v4().as_u128() as u64;
    (bits >> 11) as f64 / (1_u64 << 53) as f64
}

/// Seconds before the next poll after `failures` consecutive failures.
pub fn backoff(class: &str, failures: u32, retry_after: f64, random: f64) -> f64 {
    match class {
        "rate_limited" => retry_after.max(1.0),
        "unauthorized" | "conflict" => BLOCKED_DELAY,
        _ => {
            let exponent = failures.clamp(1, 16) - 1;
            let ceiling = (BACKOFF_BASE * 2_f64.powi(exponent as i32)).min(BACKOFF_CAP);
            ceiling * (0.5 + 0.5 * random.clamp(0.0, 1.0))
        }
    }
}

/// One poll result, without any request data.
pub struct Attempt {
    pub ok: bool,
    pub class: &'static str,
    pub http_status: Option<u16>,
    pub telegram_error_code: Option<i64>,
    pub elapsed_ms: u64,
    pub retry_after: f64,
}
impl Attempt {
    pub fn failed(failure: &crate::telegram::Failure, elapsed_ms: u64) -> Self {
        Self {
            ok: false,
            class: if CLASSES.contains(&failure.class) {
                failure.class
            } else {
                "invalid_response"
            },
            http_status: failure.http_status,
            telegram_error_code: failure.telegram_error_code,
            elapsed_ms,
            retry_after: failure.delay,
        }
    }
    pub fn succeeded(elapsed_ms: u64) -> Self {
        Self {
            ok: true,
            class: "",
            http_status: None,
            telegram_error_code: None,
            elapsed_ms,
            retry_after: 0.0,
        }
    }
}

/// Next stored record, the poll delay and an optional journal event.
pub fn transition(
    bot: &str,
    previous: &Value,
    attempt: &Attempt,
    now: f64,
    ok_delay: f64,
    random: f64,
) -> (Value, f64, Option<Value>) {
    let was_failed = previous["status"] == "failed";
    let previous_failures = if was_failed {
        previous["consecutive_failures"].as_u64().unwrap_or(1)
    } else {
        0
    };
    let previous_since = previous["since"].as_f64();
    let last_ok_at = previous["last_ok_at"].as_f64();
    if attempt.ok {
        let since = if previous["status"] == "ok" {
            previous_since.unwrap_or(now)
        } else {
            now
        };
        let record = json!({"status":"ok","at":now,"since":since,"last_ok_at":now,
            "consecutive_failures":0,"error_class":null,"http_status":null,
            "telegram_error_code":null,"elapsed_ms":attempt.elapsed_ms,
            "retry_after":null,"next_attempt_at":now+ok_delay});
        let event = was_failed.then(|| {
            let started = previous_since.unwrap_or(now);
            json!({"event":"telegram-connection","bot":bot,"at":now,"transition":"recovered",
                "status":"ok","previous_error_class":previous["error_class"],
                "consecutive_failures":previous_failures,"failing_since":started,
                "outage_seconds":((now-started).max(0.0)*10.0).round()/10.0})
        });
        return (record, ok_delay, event);
    }
    let failures = u32::try_from(previous_failures.saturating_add(1)).unwrap_or(u32::MAX);
    let delay = backoff(attempt.class, failures, attempt.retry_after, random);
    let delay = (delay * 10.0).round() / 10.0;
    let since = if was_failed {
        previous_since.unwrap_or(now)
    } else {
        now
    };
    let record = json!({"status":"failed","at":now,"since":since,"last_ok_at":last_ok_at,
        "consecutive_failures":failures,"error_class":attempt.class,
        "http_status":attempt.http_status,"telegram_error_code":attempt.telegram_error_code,
        "elapsed_ms":attempt.elapsed_ms,"retry_after":delay,"next_attempt_at":now+delay});
    let changed = was_failed
        && (previous["error_class"] != record["error_class"]
            || previous["http_status"] != record["http_status"]);
    let event = (!was_failed || changed).then(|| {
        json!({"event":"telegram-connection","bot":bot,"at":now,
            "transition":if was_failed {"changed"} else {"failed"},"status":"failed",
            "error_class":attempt.class,"http_status":attempt.http_status,
            "telegram_error_code":attempt.telegram_error_code,
            "previous_error_class":previous["error_class"],
            "consecutive_failures":failures,"failing_since":since,"retry_after":delay})
    });
    (record, delay, event)
}

/// Poll outcome: persist the new state and log state changes.
pub(crate) fn record(
    store: &crate::db::Store,
    bot: &str,
    attempt: &Attempt,
    ok_delay: f64,
) -> anyhow::Result<(f64, bool)> {
    let key = format!("connection:{bot}");
    let previous = store.get(&key)?;
    let (record, delay, event) = transition(
        bot,
        &previous,
        attempt,
        crate::now(),
        ok_delay,
        random_unit(),
    );
    store.put(&key, &record)?;
    let recovered = event
        .as_ref()
        .is_some_and(|e| e["transition"] == "recovered");
    if let Some(event) = event {
        eprintln!("{event}");
    }
    Ok((delay, recovered))
}

/// Whether a Telegram delivery should stay pending because the bot's poll
/// failed recently with a transient upstream class. Stale records expire.
pub fn should_defer(connection: &Value, now: f64) -> bool {
    should_defer_with(connection, now, &crate::config::DeliveryPolicy::default())
}

/// Policy-driven form. Past `max_defer_seconds` of continuous failure the send
/// is attempted anyway (see `deferral_exhausted`).
pub fn should_defer_with(
    connection: &Value,
    now: f64,
    policy: &crate::config::DeliveryPolicy,
) -> bool {
    if !policy.defer_while_failing || connection["status"] != "failed" {
        return false;
    }
    let Some(at) = connection["at"].as_f64() else {
        return false;
    };
    let age = now - at;
    if !(0.0..=policy.defer_window_seconds as f64).contains(&age) {
        return false;
    }
    if deferral_exhausted(connection, now, policy) {
        return false;
    }
    let allowed = |class: &str| policy.defer_error_classes.iter().any(|c| c == class);
    match connection["error_class"].as_str() {
        Some(class @ ("timeout" | "connect" | "dns")) => allowed(class),
        Some("http_status") => {
            allowed("http_5xx")
                && connection["http_status"]
                    .as_u64()
                    .or_else(|| connection["telegram_error_code"].as_u64())
                    .is_some_and(|code| code >= 500)
        }
        _ => false,
    }
}

/// The bot has been failing continuously for longer than the policy allows.
pub fn deferral_exhausted(
    connection: &Value,
    now: f64,
    policy: &crate::config::DeliveryPolicy,
) -> bool {
    connection["status"] == "failed"
        && connection["since"]
            .as_f64()
            .is_some_and(|since| now - since > policy.max_defer_seconds as f64)
}

/// Diagnose view: every field is present; older records keep their values.
pub fn public(connection: &Value) -> Value {
    if connection.is_null() {
        return Value::Null;
    }
    let mut out = serde_json::Map::new();
    for field in FIELDS {
        out.insert(
            (*field).into(),
            connection.get(*field).cloned().unwrap_or(Value::Null),
        );
    }
    out.insert(
        "delivery_deferred".into(),
        json!(should_defer(connection, crate::now())),
    );
    Value::Object(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn failure(class: &'static str, http: Option<u16>) -> Attempt {
        Attempt {
            ok: false,
            class,
            http_status: http,
            telegram_error_code: http.map(i64::from),
            elapsed_ms: 16_000,
            retry_after: 30.0,
        }
    }

    #[test]
    fn backoff_is_exponential_jittered_and_capped() {
        assert_eq!(backoff("http_status", 1, 30.0, 1.0), 2.0);
        assert_eq!(backoff("http_status", 1, 30.0, 0.0), 1.0);
        assert_eq!(backoff("timeout", 3, 30.0, 1.0), 8.0);
        assert_eq!(backoff("connect", 6, 30.0, 1.0), 60.0);
        assert_eq!(backoff("dns", 40, 30.0, 0.0), 30.0);
        for n in 1..30 {
            let d = backoff("http_status", n, 30.0, random_unit());
            assert!((0.5..=60.0).contains(&d));
        }
        assert_eq!(backoff("rate_limited", 9, 17.0, 0.3), 17.0);
        assert_eq!(backoff("unauthorized", 1, 30.0, 0.0), 300.0);
        assert_eq!(backoff("conflict", 5, 30.0, 1.0), 300.0);
        let r = random_unit();
        assert!((0.0..1.0).contains(&r));
    }

    #[test]
    fn transitions_log_only_state_changes_and_reset_on_success() {
        let (first, delay, event) = transition(
            "bot",
            &Value::Null,
            &failure("http_status", Some(502)),
            100.0,
            1.0,
            1.0,
        );
        assert_eq!(delay, 2.0);
        assert_eq!(first["consecutive_failures"], 1);
        assert_eq!(first["since"], 100.0);
        assert_eq!(first["next_attempt_at"], 102.0);
        assert_eq!(event.unwrap()["transition"], "failed");
        for field in FIELDS {
            assert!(first.get(*field).is_some(), "{field}");
        }
        let (second, delay, event) = transition(
            "bot",
            &first,
            &failure("http_status", Some(502)),
            120.0,
            1.0,
            1.0,
        );
        assert_eq!(delay, 4.0);
        assert_eq!(second["consecutive_failures"], 2);
        assert_eq!(second["since"], 100.0);
        assert!(event.is_none());
        let (third, _, event) =
            transition("bot", &second, &failure("timeout", None), 150.0, 1.0, 1.0);
        assert_eq!(event.unwrap()["transition"], "changed");
        assert_eq!(third["consecutive_failures"], 3);
        let (ok, delay, event) =
            transition("bot", &third, &Attempt::succeeded(900), 200.0, 1.0, 0.5);
        assert_eq!(delay, 1.0);
        assert_eq!(ok["consecutive_failures"], 0);
        assert_eq!(ok["since"], 200.0);
        assert_eq!(ok["last_ok_at"], 200.0);
        assert!(ok["error_class"].is_null());
        let event = event.unwrap();
        assert_eq!(event["transition"], "recovered");
        assert_eq!(event["outage_seconds"], 100.0);
        let (again, delay, event) = transition(
            "bot",
            &ok,
            &failure("http_status", Some(502)),
            210.0,
            1.0,
            1.0,
        );
        assert_eq!(delay, 2.0, "backoff resets after success");
        assert_eq!(again["last_ok_at"], 200.0);
        assert!(event.is_some());
        let (_, _, event) = transition("bot", &ok, &Attempt::succeeded(5), 220.0, 1.0, 0.5);
        assert!(event.is_none());
        let legacy = json!({"status":"failed","at":1.0,"retry_after":30.0});
        let (upgraded, _, event) = transition(
            "bot",
            &legacy,
            &failure("http_status", Some(502)),
            50.0,
            1.0,
            1.0,
        );
        assert_eq!(upgraded["consecutive_failures"], 2);
        assert_eq!(upgraded["since"], 50.0);
        assert_eq!(event.unwrap()["transition"], "changed");
    }

    #[test]
    fn blocked_classes_wait_five_minutes() {
        let (record, delay, _) = transition(
            "bot",
            &Value::Null,
            &failure("unauthorized", Some(401)),
            1.0,
            1.0,
            0.0,
        );
        assert_eq!(delay, 300.0);
        assert_eq!(record["error_class"], "unauthorized");
        assert_eq!(record["status"], "failed");
        assert!(!should_defer(&record, 2.0));
    }

    #[test]
    fn deferral_needs_a_recent_transient_failure() {
        let now = 1000.0;
        let failed = |class: &str, http: Value, at: f64| json!({"status":"failed","at":at,"error_class":class,"http_status":http});
        assert!(should_defer(
            &failed("http_status", json!(502), now - 10.0),
            now
        ));
        assert!(should_defer(
            &failed("timeout", Value::Null, now - 120.0),
            now
        ));
        assert!(should_defer(&failed("dns", Value::Null, now), now));
        assert!(should_defer(&failed("connect", Value::Null, now), now));
        assert!(!should_defer(&failed("http_status", json!(400), now), now));
        assert!(!should_defer(&failed("tls", Value::Null, now), now));
        assert!(!should_defer(&failed("conflict", json!(409), now), now));
        assert!(!should_defer(&failed("rate_limited", json!(429), now), now));
        assert!(!should_defer(
            &failed("timeout", Value::Null, now - 121.0),
            now
        ));
        assert!(!should_defer(
            &failed("timeout", Value::Null, now + 5.0),
            now
        ));
        assert!(!should_defer(&json!({"status":"ok","at":now}), now));
        assert!(!should_defer(
            &json!({"status":"failed","at":now,"retry_after":30.0}),
            now
        ));
        assert!(!should_defer(&Value::Null, now));
    }

    #[test]
    fn deferral_follows_the_configured_policy() {
        let now = 10_000.0;
        let failed = |class: &str, http: Value, since: f64| json!({"status":"failed","at":now - 1.0,"since":since,"error_class":class,"http_status":http});
        let mut policy = crate::config::DeliveryPolicy::default();
        assert!(should_defer_with(
            &failed("timeout", Value::Null, now - 10.0),
            now,
            &policy
        ));
        assert!(!should_defer_with(
            &failed("timeout", Value::Null, now - 3601.0),
            now,
            &policy
        ));
        assert!(deferral_exhausted(
            &failed("timeout", Value::Null, now - 3601.0),
            now,
            &policy
        ));
        policy.defer_error_classes = vec!["dns".into()];
        assert!(!should_defer_with(
            &failed("timeout", Value::Null, now),
            now,
            &policy
        ));
        assert!(!should_defer_with(
            &failed("http_status", json!(502), now),
            now,
            &policy
        ));
        assert!(should_defer_with(
            &failed("dns", Value::Null, now),
            now,
            &policy
        ));
        policy.defer_while_failing = false;
        assert!(!should_defer_with(
            &failed("dns", Value::Null, now),
            now,
            &policy
        ));
        let mut wide = crate::config::DeliveryPolicy {
            defer_window_seconds: 600,
            ..Default::default()
        };
        let old =
            json!({"status":"failed","at":now - 300.0,"since":now - 300.0,"error_class":"connect"});
        assert!(should_defer_with(&old, now, &wide));
        wide.defer_window_seconds = 120;
        assert!(!should_defer_with(&old, now, &wide));
    }

    #[test]
    fn diagnose_view_has_every_field() {
        let view = public(&json!({"status":"failed","at":1.0,"retry_after":30.0}));
        for field in FIELDS {
            assert!(view.get(*field).is_some(), "{field}");
        }
        assert_eq!(view["retry_after"], 30.0);
        assert_eq!(view["delivery_deferred"], false);
        assert!(public(&Value::Null).is_null());
    }

    #[test]
    fn api_codes_map_to_classes() {
        assert_eq!(api_class(401), "unauthorized");
        assert_eq!(api_class(404), "unauthorized");
        assert_eq!(api_class(409), "conflict");
        assert_eq!(api_class(429), "rate_limited");
        assert_eq!(api_class(502), "http_status");
        assert_eq!(api_class(400), "http_status");
    }
}
