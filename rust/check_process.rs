//! Short module checks have one deadline and bounded output, including children.
use anyhow::{Context, Result, ensure};
use std::{
    io::{Read, Write},
    os::unix::process::CommandExt,
    process::{Command, Stdio},
    sync::mpsc,
    time::{Duration, Instant},
};

pub(crate) fn run(
    command: &mut Command,
    input: Vec<u8>,
    timeout: Duration,
    limit: usize,
) -> Result<Vec<u8>> {
    let deadline = Instant::now() + timeout;
    let mut child = command
        .process_group(0)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    struct Group(u32);
    impl Drop for Group {
        fn drop(&mut self) {
            unsafe {
                libc::kill(-(self.0 as i32), libc::SIGKILL);
            }
        }
    }
    let group = Group(child.id());
    let mut stdin = child.stdin.take().context("check input missing")?;
    let stdout = child.stdout.take().context("check output missing")?;
    let (sender, receiver) = mpsc::channel();
    let writer = std::thread::spawn(move || stdin.write_all(&input));
    let reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = stdout
            .take(limit as u64 + 1)
            .read_to_end(&mut bytes)
            .map(|_| bytes);
        let _ = sender.send(result);
    });
    let result = (|| -> Result<Vec<u8>> {
        let mut output = None;
        let mut status = None;
        loop {
            if output.is_none() {
                match receiver.try_recv() {
                    Ok(bytes) => {
                        let bytes = bytes?;
                        ensure!(bytes.len() < limit, "module check response too large");
                        output = Some(bytes);
                    }
                    Err(mpsc::TryRecvError::Empty) => {}
                    Err(mpsc::TryRecvError::Disconnected) => {
                        anyhow::bail!("module check output failed")
                    }
                }
            }
            if status.is_none() {
                status = child.try_wait()?;
            }
            if let Some(status) = status {
                ensure!(status.success(), "module check failed");
                if writer.is_finished()
                    && let Some(output) = output.take()
                {
                    return Ok(output);
                }
            }
            ensure!(Instant::now() < deadline, "module check timed out");
            std::thread::sleep(Duration::from_millis(20));
        }
    })();
    // Also close inherited pipes if a successful check left a descendant behind.
    drop(group);
    let _ = child.wait();
    let written = writer
        .join()
        .map_err(|_| anyhow::anyhow!("module check input failed"))?;
    reader
        .join()
        .map_err(|_| anyhow::anyhow!("module check output failed"))?;
    let output = result?;
    written?;
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bounds_output_and_deadline_including_inherited_pipes() {
        for (script, input, timeout) in [
            ("yes output", Vec::new(), Duration::from_secs(3)),
            ("sleep 30", vec![0; 1_000_000], Duration::from_millis(150)),
            ("sleep 30 & exit 0", Vec::new(), Duration::from_millis(150)),
        ] {
            let mut cmd = Command::new("/bin/sh");
            cmd.args(["-c", script]);
            assert!(run(&mut cmd, input, timeout, 1024).is_err());
        }
        let mut cmd = Command::new("/bin/sh");
        cmd.args(["-c", "cat"]);
        assert_eq!(
            run(&mut cmd, b"fixture".to_vec(), Duration::from_secs(3), 1024).unwrap(),
            b"fixture"
        );
    }
}
