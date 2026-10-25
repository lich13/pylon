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
