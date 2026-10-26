//! Public RPC failures never contain arbitrary error display strings.
use anyhow::Error;
use serde_json::{Value, json};

#[derive(Debug)]
pub struct RemoteFailure {
    pub retryable: bool,
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

/// A lost write response is not evidence that the operation did not run.
/// Share this classification across the executor and privileged broker clients.
pub(crate) async fn rpc_response(
    response: Result<reqwest::Response, reqwest::Error>,
) -> anyhow::Result<Value> {
    use anyhow::{Context, ensure};
    let response = response.map_err(|error| {
        if error.is_connect() {
            Error::from(error).context(RemoteFailure { retryable: true })
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
    json!({"ok": false, "error": if error.is::<ReconcileRequired>() {
        "operation result uncertain; inspect retained state"
    } else if transient { "service temporarily unavailable" } else { "request rejected" },
        "error_code": if transient { "unavailable" } else { "rejected" }, "retryable": transient})
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
            } else if cause.is::<RemoteFailure>() {
                "broker:remote-failure".into()
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
                    _ => "validation:details-redacted",
                }
                .into()
            }
        })
        .collect::<Vec<_>>();
    eprintln!(
        "{}",
        json!({"event":"failure", "scope":scope,"at":crate::now(),
                          "retryable":retryable(error),"causes":causes})
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
    }
}
