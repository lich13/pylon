//! Public RPC failures never contain arbitrary error display strings.
use anyhow::Error;
use serde_json::{Value, json};

#[derive(Debug)]
pub struct RemoteFailure {
    pub retryable: bool,
    /// Stable, program-owned failure class reported by the remote service.
    pub reason: Option<&'static str>,
    /// Exit status of a privileged helper, when that is what failed.
    pub exit_code: Option<i32>,
}
impl RemoteFailure {
    pub fn unavailable() -> Self {
        Self {
            retryable: true,
            reason: Some("unavailable"),
            exit_code: None,
        }
    }
}
impl std::fmt::Display for RemoteFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(if self.retryable {
            "service temporarily unavailable"
        } else {
            "request rejected"
        })
    }
}
impl std::error::Error for RemoteFailure {}

#[derive(Debug)]
pub struct ReconcileRequired;
impl std::fmt::Display for ReconcileRequired {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("operation result uncertain; inspect retained state")
    }
}
impl std::error::Error for ReconcileRequired {}

/// A privileged helper ran and exited unsuccessfully. Its output is never
/// relayed; only the numeric exit status (or terminating signal) is kept.
#[derive(Debug)]
pub struct HelperFailed {
    pub code: Option<i32>,
    pub signal: Option<i32>,
}
impl std::fmt::Display for HelperFailed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("privileged helper rejected or failed; inspect retained state")
    }
}
impl std::error::Error for HelperFailed {}

/// Every public failure class. Only these exact strings cross a process
/// boundary, so a remote peer cannot inject arbitrary text through `reason`.
pub const REASONS: &[&str] = &[
    "uncertain",
    "unavailable",
    "rejected",
    "permission_denied",
    "not_found",
    "symlink_rejected",
    "io_error",
    "database_busy",
    "database_readonly",
    "database_unavailable",
    "database_error",
    "invalid_json",
    "helper_failed",
    "helper_untrusted",
    "capability_not_installed",
    "capability_undeclared",
    "capability_module_denied",
    "capability_origin_denied",
    "context_required",
    "owner_mismatch",
    "path_outside_home",
    "path_invalid",
    "module_draining",
];

fn known_reason(value: &str) -> Option<&'static str> {
    REASONS.iter().copied().find(|r| *r == value)
}

/// Classify one cause without reading its free-form display text, except for
/// exact program-owned messages.
fn cause_reason(cause: &(dyn std::error::Error + 'static)) -> Option<&'static str> {
    if let Some(remote) = cause.downcast_ref::<RemoteFailure>() {
        return remote.reason;
    }
    if cause.is::<HelperFailed>() {
        return Some("helper_failed");
    }
    if let Some(e) = cause.downcast_ref::<std::io::Error>() {
        return Some(match (e.raw_os_error(), e.kind()) {
            (Some(libc::ELOOP), _) => "symlink_rejected",
            (Some(libc::EROFS), _) => "permission_denied",
            (_, std::io::ErrorKind::PermissionDenied) => "permission_denied",
            (_, std::io::ErrorKind::NotFound) => "not_found",
            _ => "io_error",
        });
    }
    if let Some(e) = cause.downcast_ref::<rusqlite::Error>() {
        use rusqlite::ErrorCode::*;
        return Some(match e {
            rusqlite::Error::SqliteFailure(code, _) => match code.code {
                DatabaseBusy | DatabaseLocked => "database_busy",
                ReadOnly => "database_readonly",
                PermissionDenied | AuthorizationForStatementDenied => "permission_denied",
                CannotOpen | NotADatabase | DatabaseCorrupt | DiskFull | SystemIoFailure => {
                    "database_unavailable"
                }
                _ => "database_error",
            },
            _ => "database_error",
        });
    }
    if cause.is::<serde_json::Error>() {
        return Some("invalid_json");
    }
    if let Some(e) = cause.downcast_ref::<reqwest::Error>() {
        return e.is_connect().then_some("unavailable");
    }
    match cause.to_string().as_str() {
        "helper path is not exclusively root-owned" => Some("helper_untrusted"),
        "capability is not installed" => Some("capability_not_installed"),
        "capability not declared" => Some("capability_undeclared"),
        "capability not granted to module" => Some("capability_module_denied"),
        "capability not granted to this execution origin" => Some("capability_origin_denied"),
        "context required" | "capability required" => Some("context_required"),
        "owner mismatch" | "delivery owner mismatch" => Some("owner_mismatch"),
        "path escapes home" | "workspace escapes home" => Some("path_outside_home"),
        "unsafe relative path" => Some("path_invalid"),
        "module deployment in progress; request was not started" => Some("module_draining"),
        _ => None,
    }
}

/// Stable failure class for diagnostics. Never contains caller-controlled text.
pub fn reason(error: &Error) -> &'static str {
    if error.is::<ReconcileRequired>() {
        return "uncertain";
    }
    // Outer program-owned contexts are more specific than generic I/O roots.
    error
        .chain()
        .find_map(cause_reason)
        .unwrap_or(if retryable(error) {
            "unavailable"
        } else {
            "rejected"
        })
}

fn exit_code(error: &Error) -> Option<i32> {
    error.chain().find_map(|cause| {
        cause
            .downcast_ref::<HelperFailed>()
            .and_then(|h| h.code)
            .or_else(|| {
                cause
                    .downcast_ref::<RemoteFailure>()
                    .and_then(|r| r.exit_code)
            })
    })
}

/// A lost write response is not evidence that the operation did not run.
/// Share this classification across the executor and privileged broker clients.
pub(crate) async fn rpc_response(
    response: Result<reqwest::Response, reqwest::Error>,
) -> anyhow::Result<Value> {
    use anyhow::{Context, ensure};
    let response = response.map_err(|error| {
        if error.is_connect() {
            Error::from(error).context(RemoteFailure::unavailable())
        } else {
            Error::from(error).context(ReconcileRequired)
        }
    })?;
    ensure!(response.status().is_success(), ReconcileRequired);
    let value: Value = response.json().await.context(ReconcileRequired)?;
    match value["ok"].as_bool() {
        Some(true) => value.get("value").cloned().context(ReconcileRequired),
        Some(false) => {
            if value["error"] == "operation result uncertain; inspect retained state" {
                return Err(ReconcileRequired.into());
            }
            ensure!(value["error"].is_string(), ReconcileRequired);
            Err(RemoteFailure {
                retryable: value["retryable"].as_bool().context(ReconcileRequired)?,
                reason: value["reason"].as_str().and_then(known_reason),
                exit_code: value["exit_code"]
                    .as_i64()
                    .and_then(|code| i32::try_from(code).ok())
                    .filter(|code| (0..=255).contains(code)),
            }
            .into())
        }
        None => Err(ReconcileRequired.into()),
    }
}

pub fn retryable(error: &Error) -> bool {
    // Never let a lower-level I/O cause turn an already-started privileged action into a retry.
    if error.is::<ReconcileRequired>() {
        return false;
    }
    if let Some(remote) = error.downcast_ref::<RemoteFailure>() {
        return remote.retryable;
    }
    error.chain().any(|cause| {
        cause.is::<reqwest::Error>()
            || cause.is::<crate::bark::DnsUnavailable>()
            || cause.downcast_ref::<std::io::Error>().is_some_and(|e| {
                !matches!(
                    e.kind(),
                    std::io::ErrorKind::NotFound | std::io::ErrorKind::PermissionDenied
                )
            })
            || cause.downcast_ref::<rusqlite::Error>().is_some_and(|e| {
                matches!(e,
                rusqlite::Error::SqliteFailure(code, _) if matches!(code.code,
                    rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked))
            })
    })
}

pub fn public(error: &Error) -> Value {
    let transient = retryable(error);
    // `error` and `error_code` stay unchanged for existing callers; `reason`
    // adds a stable class so a failure is diagnosable without leaking details.
    let mut value = json!({"ok": false, "error": if error.is::<ReconcileRequired>() {
        "operation result uncertain; inspect retained state"
    } else if transient { "service temporarily unavailable" } else { "request rejected" },
        "error_code": if transient { "unavailable" } else { "rejected" }, "retryable": transient,
        "reason": reason(error)});
    if let Some(code) = exit_code(error) {
        value["exit_code"] = json!(code);
    }
    value
}

/// Keep established local CLI diagnostics without exposing arbitrary error strings.
pub fn cli_message(error: &Error) -> &'static str {
    for cause in error.chain() {
        if cause
            .downcast_ref::<std::io::Error>()
            .is_some_and(|e| e.raw_os_error() == Some(libc::ELOOP))
        {
            return "symbolic link rejected";
        }
        let safe = match cause.to_string().as_str() {
            "duplicate bot credential" => "duplicate bot credential",
            "duplicate or invalid command" => "duplicate or invalid command",
            "unsafe supervisor state directory" => "unsafe supervisor state directory",
            "unsafe supervisor state file" => "unsafe supervisor state file",
            "task workspace is not configured" => "task workspace is not configured",
            "task topic is not configured" => "task topic is not configured",
            "module stop preparation failed" => "module stop preparation failed",
            "module health check rejected" => "module health check rejected",
            "deliveries requires root" => "deliveries requires root",
            "gateway state missing" => "gateway state missing",
            "unknown event" => "unknown event",
            "invalid delivery status" => "invalid delivery status",
            "only uncertain deliveries can be resolved" => {
                "only uncertain deliveries can be resolved"
            }
            "delivery was already resent once" => "delivery was already resent once",
            "delivery changed concurrently" => "delivery changed concurrently",
            "resend can duplicate a message; pass --accept-duplicate-risk" => {
                "resend can duplicate a message; pass --accept-duplicate-risk"
            }
            "no deliveries selected" => "no deliveries selected",
            "unknown bot" => "unknown bot",
            "target bot is not configured" => "target bot is not configured",
            "target bot is disabled" => "target bot is disabled",
            "only pending deliveries can be retargeted" => {
                "only pending deliveries can be retargeted"
            }
            "delivery already uses the target bot" => "delivery already uses the target bot",
            "source bot is still configured" => "source bot is still configured",
            "only text or document deliveries can be retargeted" => {
                "only text or document deliveries can be retargeted"
            }
            "delivery already has a receipt" => "delivery already has a receipt",
            "original chat of the removed bot is unknown" => {
                "original chat of the removed bot is unknown"
            }
            "target bot delivers to a different chat" => "target bot delivers to a different chat",
            "invalid delivery payload" => "invalid delivery payload",
            _ => continue,
        };
        return safe;
    }
    if error.is::<ReconcileRequired>() {
        "operation result uncertain; inspect retained state"
    } else if retryable(error) {
        "service temporarily unavailable"
    } else {
        "request rejected"
    }
}

pub fn log(scope: &'static str, error: &Error) {
    let causes = error
        .chain()
        .map(|cause| {
            if let Some(e) = cause.downcast_ref::<std::io::Error>() {
                format!("io:{:?}:{}", e.kind(), e.raw_os_error().unwrap_or_default())
            } else if let Some(e) = cause.downcast_ref::<rusqlite::Error>() {
                match e {
                    rusqlite::Error::SqliteFailure(code, _) => {
                        format!("sqlite:{:?}:{}", code.code, code.extended_code)
                    }
                    _ => "sqlite:invalid-operation".into(),
                }
            } else if let Some(e) = cause.downcast_ref::<serde_json::Error>() {
                format!("json:{:?}:{}:{}", e.classify(), e.line(), e.column())
            } else if let Some(e) = cause.downcast_ref::<reqwest::Error>() {
                format!(
                    "http:connect={}:timeout={}:status={}",
                    e.is_connect(),
                    e.is_timeout(),
                    e.status().map(|s| s.as_u16()).unwrap_or_default()
                )
            } else if cause.is::<crate::bark::DnsUnavailable>() {
                "bark:dns-unavailable".into()
            } else if cause.is::<ReconcileRequired>() {
                "helper:reconcile-required".into()
            } else if let Some(remote) = cause.downcast_ref::<RemoteFailure>() {
                match (remote.reason, remote.exit_code) {
                    (Some(reason), Some(code)) => format!("remote:{reason}:exit={code}"),
                    (Some(reason), None) => format!("remote:{reason}"),
                    _ => "broker:remote-failure".into(),
                }
            } else if let Some(helper) = cause.downcast_ref::<HelperFailed>() {
                match (helper.code, helper.signal) {
                    (Some(code), _) => format!("helper:exit={code}"),
                    (None, Some(signal)) => format!("helper:signal={signal}"),
                    _ => "helper:failed".into(),
                }
            } else {
                // Only exact program-owned messages are safe. Do not log arbitrary contexts or URLs.
                match cause.to_string().as_str() {
                    "invalid Bark endpoint" => "bark:invalid-endpoint",
                    "Bark endpoint is not public" => "bark:nonpublic-address",
                    "owner mismatch" | "delivery owner mismatch" => "auth:owner-mismatch",
                    "event belongs to another module" => "delivery:module-mismatch",
                    "event id already has different content" => "delivery:id-conflict",
                    "path escapes home" => "path:outside-home",
                    "unsafe relative path" => "path:invalid-relative",
                    "helper path is not exclusively root-owned" => "broker:untrusted-helper",
                    "capability not declared" => "broker:undeclared-capability",
                    "capability not granted to module" => "broker:module-denied",
                    "capability not granted to this execution origin" => "broker:origin-denied",
                    "capability is not installed" => "broker:capability-not-installed",
                    "context required" | "capability required" => "broker:context-required",
                    "module deployment in progress; request was not started" => {
                        "runtime:module-draining"
                    }
                    _ => "validation:details-redacted",
                }
                .into()
            }
        })
        .collect::<Vec<_>>();
    eprintln!(
        "{}",
        json!({"event":"failure", "scope":scope,"at":crate::now(),
                          "retryable":retryable(error),"reason":reason(error),"causes":causes})
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn timeout_after_request_is_uncertain() {
        use std::time::Duration;
        use tokio::io::AsyncReadExt;

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let client = reqwest::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(1))
            .build()
            .unwrap();
        let request = client
            .post(format!("http://{address}/rpc"))
            .body("fixture")
            .send();
        let accepted = async {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut request = Vec::new();
            loop {
                request.push(stream.read_u8().await.unwrap());
                if request.ends_with(b"fixture") {
                    break;
                }
            }
            stream
        };
        // Keep the accepted connection open until the client times out.
        let (_stream, response) = tokio::join!(accepted, request);
        assert!(response.as_ref().unwrap_err().is_timeout());
        let error = rpc_response(response).await.unwrap_err();
        assert!(error.is::<ReconcileRequired>());
        assert!(!retryable(&error));
    }

    #[test]
    fn redact_untrusted_context_without_losing_classification() {
        let e = Error::from(std::io::Error::new(
            std::io::ErrorKind::ConnectionRefused,
            "https://example.invalid/fixture-secret",
        ))
        .context("fixture-sensitive-path");
        let result = public(&e);
        assert_eq!(result["error_code"], "unavailable");
        assert_eq!(result["retryable"], true);
        assert!(!result.to_string().contains("fixture"));
        let e = anyhow::anyhow!("fixture-sensitive-database-error");
        assert_eq!(public(&e)["error"], "request rejected");
        assert_eq!(cli_message(&e), "request rejected");
        let e = anyhow::anyhow!("duplicate bot credential").context("fixture-secret");
        assert_eq!(cli_message(&e), "duplicate bot credential");
        assert_eq!(public(&e)["error"], "request rejected");
        let e = Error::from(std::io::Error::other("fixture")).context(ReconcileRequired);
        assert_eq!(public(&e)["retryable"], false);
        assert_eq!(public(&e)["error_code"], "rejected");
        assert_eq!(public(&e)["reason"], "uncertain");
    }

    #[test]
    fn reason_classes_are_stable_and_never_echo_details() {
        let denied = Error::from(std::io::Error::from(std::io::ErrorKind::PermissionDenied))
            .context("fixture-secret-path");
        assert_eq!(public(&denied)["reason"], "permission_denied");
        assert_eq!(public(&denied)["error"], "request rejected");
        assert!(!public(&denied).to_string().contains("fixture"));
        let looped = Error::from(std::io::Error::from_raw_os_error(libc::ELOOP));
        assert_eq!(reason(&looped), "symlink_rejected");
        let busy = Error::from(rusqlite::Error::SqliteFailure(
            rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_BUSY),
            Some("fixture".into()),
        ));
        assert_eq!(reason(&busy), "database_busy");
        assert_eq!(public(&busy)["retryable"], true);
        let readonly = Error::from(rusqlite::Error::SqliteFailure(
            rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_READONLY),
            None,
        ));
        assert_eq!(reason(&readonly), "database_readonly");
        let undeclared = anyhow::anyhow!("capability not declared");
        assert_eq!(reason(&undeclared), "capability_undeclared");
        let helper = Error::from(HelperFailed {
            code: Some(13),
            signal: None,
        });
        let value = public(&helper);
        assert_eq!(value["error"], "request rejected");
        assert_eq!(value["reason"], "helper_failed");
        assert_eq!(value["exit_code"], 13);
        let unknown = anyhow::anyhow!("fixture-free-form-detail");
        assert_eq!(reason(&unknown), "rejected");
        assert!(
            REASONS
                .iter()
                .all(|r| r.bytes().all(|b| b.is_ascii_lowercase() || b == b'_'))
        );
    }

    #[tokio::test]
    async fn remote_reason_is_allowlisted_and_survives_relay() {
        use axum::{Router, routing::post};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let app = Router::new()
            .route(
                "/known",
                post(|| async {
                    axum::Json(
                        json!({"ok":false,"error":"request rejected","retryable":false,
                        "reason":"permission_denied","exit_code":13}),
                    )
                }),
            )
            .route(
                "/forged",
                post(|| async {
                    axum::Json(
                        json!({"ok":false,"error":"request rejected","retryable":false,
                        "reason":"fixture-injected text","exit_code":99999}),
                    )
                }),
            );
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let client = reqwest::Client::builder().no_proxy().build().unwrap();
        let known = rpc_response(client.post(format!("http://{address}/known")).send().await)
            .await
            .unwrap_err();
        let value = public(&known);
        assert_eq!(value["reason"], "permission_denied");
        assert_eq!(value["exit_code"], 13);
        assert_eq!(value["error"], "request rejected");
        let forged = rpc_response(client.post(format!("http://{address}/forged")).send().await)
            .await
            .unwrap_err();
        let value = public(&forged);
        assert_eq!(value["reason"], "rejected");
        assert!(value.get("exit_code").is_none());
        assert!(!value.to_string().contains("fixture"));
    }
}
