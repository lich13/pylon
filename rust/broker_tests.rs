use super::*;

fn command(script: &str) -> tokio::process::Command {
    let mut command = tokio::process::Command::new("/bin/sh");
    command.args(["-c", script]);
    command
}

async fn assert_stopped(path: &Path) {
    let pids = std::fs::read_to_string(path).unwrap();
    for pid in pids.split_whitespace() {
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                let status = tokio::process::Command::new("ps")
                    .args(["-p", pid, "-o", "stat="])
                    .output()
                    .await
                    .unwrap();
                if !status.status.success()
                    || String::from_utf8_lossy(&status.stdout)
                        .trim_start()
                        .starts_with('Z')
                {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await
        .expect("helper process survived cleanup");
    }
}

#[tokio::test]
async fn drains_output_while_writing_input() {
    let mut child = command(
        "head -c 1048576 /dev/zero | tr '\\000' ' '; cat >/dev/null; printf '{\"fixture\":true}'",
    );
    let output = run_helper(&mut child, vec![b'x'; 1024 * 1024], Duration::from_secs(5))
        .await
        .unwrap();
    assert_eq!(output, json!({"fixture":true}));
}

#[tokio::test]
async fn deadline_covers_blocked_stdin_and_reaps_process_group() {
    let temp = tempfile::tempdir().unwrap();
    let mut child = command("sleep 30 & echo \"$$ $!\" > pids; wait");
    child.current_dir(temp.path());
    let error = run_helper(&mut child, vec![b'x'; 1024 * 1024], Duration::from_secs(1))
        .await
        .unwrap_err();
    assert!(error.is::<crate::errors::ReconcileRequired>());
    assert!(!crate::errors::retryable(&error));
    assert_stopped(&temp.path().join("pids")).await;
}

#[tokio::test]
async fn deadline_kills_descendant_after_leader_exits() {
    let temp = tempfile::tempdir().unwrap();
    let mut child = command("sleep 30 & echo \"$!\" > pids; exit 0");
    child.current_dir(temp.path());
    let error = run_helper(&mut child, Vec::new(), Duration::from_secs(1))
        .await
        .unwrap_err();
    assert!(error.is::<crate::errors::ReconcileRequired>());
    assert_stopped(&temp.path().join("pids")).await;
}

#[tokio::test]
async fn excessive_output_is_uncertain_and_stops_the_helper() {
    let temp = tempfile::tempdir().unwrap();
    let mut child = command("echo $$ > pids; cat >/dev/null; head -c 8388609 /dev/zero; sleep 30");
    child.current_dir(temp.path());
    let error = run_helper(&mut child, Vec::new(), Duration::from_secs(5))
        .await
        .unwrap_err();
    assert!(error.is::<crate::errors::ReconcileRequired>());
    assert!(!crate::errors::retryable(&error));
    assert_stopped(&temp.path().join("pids")).await;
}

#[test]
fn job_bound_grants_may_omit_modules() {
    let check = |grant: Value, module: &str, origin: &str| {
        authorize(&grant, &json!({"module": module, "origin": origin})).map_err(|e| e.to_string())
    };
    let module_denied = Err("capability not granted to module".to_string());
    let origin_denied = Err("capability not granted to this execution origin".to_string());
    let job_only = json!({"origins": ["job:reconcile"]});
    for module in ["operations", "content"] {
        assert_eq!(check(job_only.clone(), module, "job:reconcile"), Ok(()));
        assert_eq!(check(job_only.clone(), module, "job:other"), origin_denied);
        assert_eq!(check(job_only.clone(), module, "command"), origin_denied);
    }
    for grant in [
        json!({"origins": ["job:reconcile", "command"]}),
        json!({"origins": ["service:reconcile"]}),
        json!({"origins": ["job:"]}),
        json!({"origins": []}),
        json!({}),
        json!({"modules": null, "origins": ["job:reconcile"]}),
        json!({"modules": [], "origins": ["job:reconcile"]}),
        json!({"modules": ["operations"], "origins": ["job:reconcile"]}),
    ] {
        assert_eq!(check(grant, "content", "job:reconcile"), module_denied);
    }
    let scoped = json!({"modules": ["operations"], "origins": ["job:reconcile", "command"]});
    assert_eq!(check(scoped.clone(), "operations", "command"), Ok(()));
    assert_eq!(
        check(scoped.clone(), "operations", "callback"),
        origin_denied
    );
    assert_eq!(check(scoped, "content", "command"), module_denied);
}

fn grant_allows(grant: Value, module: &str, origin: &str) -> std::result::Result<(), String> {
    authorize(&grant, &json!({"module":module,"origin":origin})).map_err(|e| e.to_string())
}

#[test]
fn exact_job_grants_keep_working() {
    let grant = json!({"origins":["job:alpha-sync"]});
    assert!(grant_allows(grant.clone(), "alpha", "job:alpha-sync").is_ok());
    assert_eq!(
        grant_allows(grant, "alpha", "job:alpha-new").unwrap_err(),
        "capability not granted to this execution origin"
    );
    let grant = json!({"modules":["ops"],"origins":["command","job:ops-sync"]});
    assert!(grant_allows(grant.clone(), "ops", "command").is_ok());
    assert!(grant_allows(grant.clone(), "ops", "job:ops-sync").is_ok());
    assert!(grant_allows(grant.clone(), "ops", "job:ops-new").is_err());
    assert_eq!(
        grant_allows(grant, "other", "command").unwrap_err(),
        "capability not granted to module"
    );
}

#[test]
fn module_scoped_grant_accepts_a_new_job_without_grant_edit() {
    for grant in [
        json!({"modules":["ops"]}),
        json!({"modules":["ops"],"origins":["module:*"]}),
        json!({"origins":["module:ops"]}),
        json!({"modules":["gmail"],"origins":["command","module:ops"]}),
    ] {
        for origin in [
            "job:ops-added-later",
            "service:worker",
            "command",
            "callback",
            "document",
        ] {
            assert!(
                grant_allows(grant.clone(), "ops", origin).is_ok(),
                "{grant} {origin}"
            );
        }
        assert_eq!(
            grant_allows(grant.clone(), "other", "job:ops-added-later").unwrap_err(),
            "capability not granted to module",
            "{grant}"
        );
    }
    // `module:*` only widens origins for modules the grant lists.
    assert!(grant_allows(json!({"origins":["module:*"]}), "ops", "command").is_err());
    // An explicit empty origin list still denies everything.
    assert!(grant_allows(json!({"modules":["ops"],"origins":[]}), "ops", "command").is_err());
    assert!(grant_allows(json!({}), "ops", "command").is_err());
}
