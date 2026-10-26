use super::*;
use crate::api::App;
use serde_json::{Value, json};
use std::os::unix::fs::{PermissionsExt, symlink};

fn fixture() -> (tempfile::TempDir, App) {
    let temp = tempfile::tempdir().unwrap();
    let config = Config::new(temp.path().to_owned());
    for dir in ["config", "modules/fixture", "gateway/secrets", "workspace"] {
        fs::create_dir_all(temp.path().join(dir)).unwrap();
    }
    fs::write(
        temp.path().join("config/bots.json"),
        json!({
            "bots":{"fixture":{"owner":"101","credential":"fixture"}},
            "routes":{"personal":"fixture"},
            "workspaces":{"fixture":{"root":"workspace"}}
        })
        .to_string(),
    )
    .unwrap();
    let secret = temp.path().join("gateway/secrets/fixture");
    fs::write(&secret, "123:fixture_credential_000001").unwrap();
    fs::set_permissions(&secret, fs::Permissions::from_mode(0o600)).unwrap();
    fs::write(
        temp.path().join("modules/fixture/module.json"),
        json!({
            "api_version":2,"id":"fixture","handler":["native","handler.sh"],"workspace":"fixture"
        })
        .to_string(),
    )
    .unwrap();
    fs::write(temp.path().join("modules/fixture/handler.sh"), "echo one\n").unwrap();
    (temp, App::new(config).unwrap())
}

async fn hash(app: &App) -> String {
    app.snapshot.read().await.catalog.hash.clone()
}

#[tokio::test]
async fn reload_replaces_the_snapshot_without_changing_inflight_requests() {
    let (temp, app) = fixture();
    let original = app.snapshot.read().await.clone();
    assert!(std::sync::Arc::ptr_eq(
        &original,
        &*app.snapshot.read().await
    ));
    fs::write(temp.path().join("modules/fixture/handler.sh"), "echo two\n").unwrap();
    app.reload().await.unwrap();
    let current = app.snapshot.read().await.clone();
    assert!(!std::sync::Arc::ptr_eq(&original, &current));
    assert_ne!(original.catalog.hash, current.catalog.hash);
    let expected = original.catalog.modules["fixture"].hash.clone();
    fs::write(temp.path().join("config/bots.json"), "invalid fixture").unwrap();
    assert!(app.reload().await.is_err());
    assert!(std::sync::Arc::ptr_eq(
        &current,
        &*app.snapshot.read().await
    ));
    assert_eq!(original.catalog.modules["fixture"].hash, expected);
}

#[tokio::test]
async fn unchanged_checks_do_not_read_source_or_credentials_and_force_still_does() {
    let (temp, app) = fixture();
    let root = temp.path().join("modules/fixture");
    let secret = temp.path().join("gateway/secrets/fixture");
    let original = (reads::count(&root), reads::count(&secret));
    for _ in 0..4 {
        assert!(!app.reload_if_changed().await.unwrap());
    }
    assert_eq!((reads::count(&root), reads::count(&secret)), original);
    assert_eq!(app.store.runtime.metrics.snapshot()["reload_skipped"], 4);
    assert_eq!(
        crate::api::dispatch(&app, &json!({"api_version":2,"method":"admin.reload"}))
            .await
            .unwrap(),
        json!({"ok":true})
    );
    assert_eq!(
        (reads::count(&root), reads::count(&secret)),
        (original.0 + 1, original.1 + 1)
    );
}

#[tokio::test]
async fn complete_source_inventory_catches_rewrites_modes_moves_and_replacements() {
    let (temp, app) = fixture();
    let root = temp.path().join("modules/fixture");
    let source = root.join("handler.sh");
    let before = hash(&app).await;
    let mtime = fs::metadata(&source).unwrap().modified().unwrap();
    // Same byte length and restored mtime: ctime still invalidates the precheck.
    fs::write(&source, "echo two\n").unwrap();
    fs::File::options()
        .write(true)
        .open(&source)
        .unwrap()
        .set_times(fs::FileTimes::new().set_modified(mtime))
        .unwrap();
    assert!(app.reload_if_changed().await.unwrap());
    assert_ne!(before, hash(&app).await);
    fs::set_permissions(&source, fs::Permissions::from_mode(0o755)).unwrap();
    assert!(app.reload_if_changed().await.unwrap());
    fs::write(root.join("extra.rs"), "fixture").unwrap();
    assert!(app.reload_if_changed().await.unwrap());
    fs::rename(root.join("extra.rs"), root.join("renamed.rs")).unwrap();
    assert!(app.reload_if_changed().await.unwrap());
    fs::remove_file(root.join("renamed.rs")).unwrap();
    assert!(app.reload_if_changed().await.unwrap());
    fs::rename(&root, temp.path().join("saved-module")).unwrap();
    fs::create_dir(&root).unwrap();
    for file in ["module.json", "handler.sh"] {
        fs::copy(temp.path().join("saved-module").join(file), root.join(file)).unwrap();
    }
    assert!(app.reload_if_changed().await.unwrap());
    assert!(!app.reload_if_changed().await.unwrap());
}

#[tokio::test]
async fn discovery_registry_and_ignored_data_share_the_content_hash_rules() {
    let (temp, app) = fixture();
    let root = temp.path().join("modules/fixture");
    let before = hash(&app).await;
    fs::create_dir(root.join("data")).unwrap();
    fs::write(root.join("data/runtime.json"), "changed").unwrap();
    assert!(!app.reload_if_changed().await.unwrap());
    assert_eq!(before, hash(&app).await);
    let registry = temp.path().join("modules/registry.json");
    fs::write(&registry, json!({"api_version":2,"modules":{}}).to_string()).unwrap();
    assert!(app.reload_if_changed().await.unwrap());
    assert!(app.snapshot.read().await.catalog.modules.is_empty());
    fs::remove_file(registry).unwrap();
    assert!(app.reload_if_changed().await.unwrap());
    assert_eq!(before, hash(&app).await);
    symlink("handler.sh", root.join("linked.sh")).unwrap();
    assert!(app.reload_if_changed().await.is_err());
    assert_eq!(before, hash(&app).await);
}

#[tokio::test]
async fn rejected_credentials_and_settings_never_replace_valid_snapshot() {
    let (temp, app) = fixture();
    let path = temp.path().join("gateway/secrets/fixture");
    let config = temp.path().join("config/bots.json");
    let original = app.snapshot.read().await.clone();
    let temporary = path.with_extension("new");
    fs::write(&temporary, "456:fixture_credential_000002").unwrap();
    fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600)).unwrap();
    fs::rename(&temporary, &path).unwrap();
    assert!(app.reload_if_changed().await.unwrap());
    assert_ne!(original.credentials, app.snapshot.read().await.credentials);
    let accepted = app.snapshot.read().await.credentials.clone();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(app.reload_if_changed().await.is_err());
    assert_eq!(accepted, app.snapshot.read().await.credentials);
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    assert!(app.reload_if_changed().await.unwrap());
    fs::rename(&path, &temporary).unwrap();
    assert!(app.reload_if_changed().await.is_err());
    symlink(&temporary, &path).unwrap();
    assert!(app.reload_if_changed().await.is_err());
    fs::remove_file(&path).unwrap();
    fs::rename(&temporary, &path).unwrap();
    let mut settings: Value = serde_json::from_slice(&fs::read(&config).unwrap()).unwrap();
    settings["bots"]["duplicate"] = settings["bots"]["fixture"].clone();
    fs::write(&config, settings.to_string()).unwrap();
    assert!(app.reload_if_changed().await.is_err());
    assert_eq!(accepted, app.snapshot.read().await.credentials);
    // Removing a bot and its secret together must validate the new settings,
    // rather than requiring a no-longer-referenced credential to be repaired.
    settings["bots"] = json!({});
    settings["routes"] = json!({});
    fs::remove_file(&path).unwrap();
    fs::write(&config, settings.to_string()).unwrap();
    assert!(app.reload_if_changed().await.unwrap());
    assert!(app.snapshot.read().await.credentials.is_empty());
}

#[tokio::test]
async fn unchanged_config_still_checks_workspace_and_module_containment() {
    let (temp, app) = fixture();
    let outside = tempfile::tempdir().unwrap();
    let workspace = temp.path().join("workspace");
    fs::remove_dir(&workspace).unwrap();
    symlink(outside.path(), &workspace).unwrap();
    assert!(app.reload_if_changed().await.is_err());
    fs::remove_file(&workspace).unwrap();
    fs::create_dir(&workspace).unwrap();
    // No content change is needed to resume after restoring a safe path.
    app.reload_if_changed().await.unwrap();
    fs::write(
        temp.path().join("modules/registry.json"),
        json!({
            "api_version":2,"modules":{"fixture":outside.path()}
        })
        .to_string(),
    )
    .unwrap();
    assert!(app.reload_if_changed().await.is_err());
}

#[tokio::test]
async fn racing_load_retries_once_and_unstable_candidate_keeps_cache() {
    let (temp, app) = fixture();
    let root = temp.path().join("modules/fixture");
    reads::change_during_hash(&root, &[b"echo two\n"]);
    app.reload().await.unwrap();
    let accepted = hash(&app).await;
    assert_eq!(accepted, Catalog::load(&app.config).unwrap().hash);
    reads::change_during_hash(&root, &[b"echo three\n", b"echo four\n"]);
    assert!(app.reload().await.is_err());
    assert_eq!(accepted, hash(&app).await);
    assert!(app.reload_if_changed().await.unwrap());
    assert_ne!(accepted, hash(&app).await);
    let (forced, automatic) = tokio::join!(app.reload(), app.reload_if_changed());
    forced.unwrap();
    automatic.unwrap();
    assert_eq!(hash(&app).await, Catalog::load(&app.config).unwrap().hash);
}

#[tokio::test]
async fn transient_source_entries_invalidate_full_load_even_when_inventory_is_restored() {
    let (temp, app) = fixture();
    let root = temp.path().join("modules/fixture");
    let empty = root.join("empty");
    fs::create_dir(&empty).unwrap();
    app.reload().await.unwrap();
    let settings = app.snapshot.read().await.settings.clone();
    let before = Inputs::capture(&app.config, &settings).unwrap();
    // A full hash could have seen this source between its two inventory scans.
    fs::write(empty.join("transient.rs"), "fixture").unwrap();
    fs::remove_file(empty.join("transient.rs")).unwrap();
    let after = Inputs::capture(&app.config, &settings).unwrap();
    assert_ne!(
        before, after,
        "full-load race check lost directory change times"
    );
    assert_eq!(before.paths, after.paths);
    assert!(
        !app.reload_if_changed().await.unwrap(),
        "unchanged accepted source remains valid"
    );
}
