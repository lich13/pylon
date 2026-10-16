use pylon::{config::Config, runtime};
use std::{
    env,
    fs::{self, OpenOptions},
    io::{Read, Write},
    os::{
        fd::{AsRawFd, IntoRawFd},
        unix::{fs::MetadataExt, process::CommandExt},
    },
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

const CHILD_MODE: &str = "PYLON_TEST_INHERITED_LOCK_CHILD";
const CHILD_HOME: &str = "PYLON_TEST_LOCK_HOME";
const CHILD_RUN: &str = "PYLON_TEST_LOCK_RUN";
const CHILD_FD: &str = "PYLON_TEST_LOCK_FD";
const PROBE_PATH: &str = "PYLON_TEST_FLOCK_PROBE_PATH";
const PROBE_MODE: &str = "PYLON_TEST_FLOCK_PROBE_MODE";

fn dispatch_path(home: &Path, run: &str) -> PathBuf {
    home.join("run")
        .join(format!("dispatch-{}.lock", pylon::digest(run)))
}

fn run_flock_probe(path: &Path, mode: &str) {
    let output = Command::new(env::current_exe().unwrap())
        .arg("--exact")
        .arg("flock_probe_child")
        .arg("--nocapture")
        .env(PROBE_PATH, path)
        .env(PROBE_MODE, mode)
        .env_remove(CHILD_MODE)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "flock probe ({mode}) failed: stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    );
}

#[test]
fn inherited_dispatch_lock_rejects_a_different_inode() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path().join("home");
    let run_dir = home.join("run");
    fs::create_dir_all(&run_dir).unwrap();
    let run = "fixture-run";
    let expected_path = dispatch_path(&home, run);
    fs::write(&expected_path, b"dispatch lock fixture").unwrap();
    let wrong_path = run_dir.join("other.lock");
    let wrong_file = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(&wrong_path)
        .unwrap();

    let expected_meta = fs::symlink_metadata(&expected_path).unwrap();
    let wrong_meta = wrong_file.metadata().unwrap();
    assert_eq!(expected_meta.dev(), wrong_meta.dev());
    assert_ne!(expected_meta.ino(), wrong_meta.ino());

    let mut config = Config::new(home);
    // The offline flag allows this contract test to run as a normal macOS user.
    config.offline = true;
    let fd = wrong_file.into_raw_fd();
    let error = runtime::inherited_dispatch_lock(&config, run, Some(fd)).unwrap_err();
    assert!(
        format!("{error:#}").contains("dispatch descriptor identity mismatch"),
        "unexpected error: {error:#}"
    );
    assert_eq!(unsafe { libc::fcntl(fd, libc::F_GETFD) }, -1);
    assert_eq!(
        std::io::Error::last_os_error().raw_os_error(),
        Some(libc::EBADF)
    );
}

#[test]
fn inherited_dispatch_lock_uses_flock_inherited_fd_and_releases_on_drop() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path().join("home");
    fs::create_dir_all(home.join("run")).unwrap();
    let run = "fixture-inherited-run";
    let lock_path = dispatch_path(&home, run);
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(&lock_path)
        .unwrap();
    let fd = file.as_raw_fd();
    assert!(fd >= 3);
    let original_flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    assert!(original_flags >= 0);
    assert_ne!(original_flags & libc::FD_CLOEXEC, 0);
    assert_eq!(
        unsafe { libc::fcntl(fd, libc::F_SETFD, original_flags & !libc::FD_CLOEXEC) },
        0
    );

    let mut child = Command::new(env::current_exe().unwrap());
    child
        .arg("--exact")
        .arg("inherited_dispatch_lock_child_entrypoint")
        .arg("--nocapture")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env(CHILD_MODE, "1")
        .env(CHILD_HOME, &home)
        .env(CHILD_RUN, run)
        .env(CHILD_FD, fd.to_string());
    // Keep the test descriptor available in the child through exec on macOS.
    unsafe {
        child.pre_exec(|| Ok(()));
    }
    let mut child = child.spawn().unwrap();
    let mut child_stdin = child.stdin.take().unwrap();

    // The child now owns an inherited copy; closing the parent's copy lets its
    // File drop release the flock after the child verifies contention.
    drop(file);
    child_stdin.write_all(b"go").unwrap();
    drop(child_stdin);
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "inherited lock child failed: stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    );
}

#[test]
fn inherited_dispatch_lock_child_entrypoint() {
    if env::var_os(CHILD_MODE).is_none() {
        return;
    }

    let mut start = [0u8; 2];
    std::io::stdin().read_exact(&mut start).unwrap();
    assert_eq!(&start, b"go");

    let home = PathBuf::from(env::var_os(CHILD_HOME).unwrap());
    let run = env::var(CHILD_RUN).unwrap();
    let fd = env::var(CHILD_FD).unwrap().parse::<i32>().unwrap();
    let mut config = Config::new(home.clone());
    config.offline = true;
    let lock = runtime::inherited_dispatch_lock(&config, &run, Some(fd))
        .unwrap()
        .expect("the inherited descriptor should produce a lock");
    let flags = unsafe { libc::fcntl(lock.as_raw_fd(), libc::F_GETFD) };
    assert!(flags >= 0);
    assert_ne!(flags & libc::FD_CLOEXEC, 0);

    let path = dispatch_path(&home, &run);
    run_flock_probe(&path, "busy");
    drop(lock);
    run_flock_probe(&path, "available");
}

#[test]
fn flock_probe_child() {
    let Some(path) = env::var_os(PROBE_PATH) else {
        return;
    };
    let mode = env::var(PROBE_MODE).unwrap();
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .unwrap();
    match mode.as_str() {
        "busy" => match file.try_lock() {
            Err(std::fs::TryLockError::WouldBlock) => {}
            Err(std::fs::TryLockError::Error(error)) => {
                panic!("flock probe failed with an OS error: {error}")
            }
            Ok(()) => panic!("flock probe unexpectedly acquired the busy lock"),
        },
        "available" => {
            file.try_lock().unwrap();
            file.unlock().unwrap();
        }
        other => panic!("unknown probe mode: {other}"),
    }
}
