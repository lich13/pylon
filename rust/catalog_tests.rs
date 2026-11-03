use super::Catalog;
use serde_json::json;
use std::path::Path;

fn write(base: &Path, id: &str, manifest: serde_json::Value) {
    let root = base.join(id);
    std::fs::create_dir_all(&root).unwrap();
    std::fs::write(root.join("run.sh"), "true\n").unwrap();
    std::fs::write(root.join("module.json"), manifest.to_string()).unwrap();
}
fn manifest(id: &str, command: &str, topic: &str) -> serde_json::Value {
    json!({"api_version":2,"id":id,"handler":["bash","run.sh"],"workspace":"default",
        "commands":[{"name":command,"description":"fixture","topic":topic}],
        "jobs":{format!("{id}-job"):{"name":"job","entry":["bash","run.sh"],"workspace":"default",
            "topic":topic,"schedule":"0 * * * *"}}})
}
fn settings(routes: serde_json::Value, default_bot: bool) -> crate::config::Settings {
    let mut bots = json!({"ops":{"owner":"1","credential":"ops"}});
    if default_bot {
        bots["default"] = json!({"owner":"1","credential":"main"});
    }
    let config = crate::config::Config::new(std::env::temp_dir());
    config
        .settings_from_bytes(
            json!({"bots":bots,"routes":routes,
                "workspaces":{"default":{"root":"profiles/default"}}})
            .to_string()
            .as_bytes(),
        )
        .unwrap()
}

#[test]
fn one_invalid_module_never_rejects_the_catalog() {
    let temp = tempfile::tempdir().unwrap();
    let base = temp.path();
    write(base, "alpha", manifest("alpha", "status", "personal"));
    // Same command as alpha: only the later module (id order) is rejected.
    write(base, "beta", manifest("beta", "status", "personal"));
    write(
        base,
        "gamma",
        json!({"api_version":2,"id":"gamma","handler":"broken"}),
    );
    let mut needs = manifest("delta", "delta", "personal");
    needs["requires"] = json!(["beta"]);
    write(base, "delta", needs);
    let catalog = Catalog::load_from(base.to_path_buf()).unwrap();
    assert_eq!(catalog.modules.keys().collect::<Vec<_>>(), ["alpha"]);
    assert_eq!(catalog.rejected["beta"], "duplicate or invalid command");
    assert_eq!(catalog.rejected["gamma"], "invalid module manifest");
    assert_eq!(catalog.rejected["delta"], "missing module dependency");
    // Rejected modules are not served, so they do not change the hash.
    std::fs::remove_dir_all(base.join("beta")).unwrap();
    std::fs::remove_dir_all(base.join("gamma")).unwrap();
    std::fs::remove_dir_all(base.join("delta")).unwrap();
    assert_eq!(
        Catalog::load_from(base.to_path_buf()).unwrap().hash,
        catalog.hash
    );
    assert_eq!(
        catalog.ensure_accepted(None).unwrap_err().to_string(),
        "duplicate or invalid command"
    );
    assert!(catalog.ensure_accepted(Some(&catalog)).is_ok());
}

#[test]
fn unrouted_topics_use_the_fallback_route() {
    let temp = tempfile::tempdir().unwrap();
    write(
        temp.path(),
        "alpha",
        manifest("alpha", "status", "personal"),
    );
    write(temp.path(), "beta", manifest("beta", "other", "newtopic"));
    let load = || Catalog::load_from(temp.path().to_path_buf()).unwrap();

    // No explicit fallback: bot `default` serves unrouted topics.
    let s = settings(json!({"personal":"ops"}), true);
    assert_eq!(s.route_base("newtopic").as_deref(), Some("default"));
    assert_eq!(s.route_base("personal").as_deref(), Some("ops"));
    assert_eq!(s.route_base(""), None);
    let mut catalog = load();
    catalog.apply_settings(&s);
    assert!(catalog.rejected.is_empty());
    assert_eq!(catalog.fallback_bot.as_deref(), Some("default"));
    assert_eq!(
        catalog.fallback_topics.iter().collect::<Vec<_>>(),
        ["newtopic"]
    );

    // An explicit `*` route wins over bot `default`.
    let s = settings(json!({"personal":"ops","*":"ops"}), true);
    assert_eq!(s.route_base("newtopic").as_deref(), Some("ops"));
    let mut catalog = load();
    catalog.apply_settings(&s);
    assert_eq!(catalog.fallback_bot.as_deref(), Some("ops"));

    // No fallback at all: only the module with the unrouted topic is rejected.
    let s = settings(json!({"personal":"ops"}), false);
    let mut catalog = load();
    catalog.apply_settings(&s);
    assert_eq!(catalog.modules.keys().collect::<Vec<_>>(), ["alpha"]);
    assert_eq!(catalog.rejected["beta"], "task topic is not configured");
    assert!(catalog.fallback_topics.is_empty());
    // Logging the change must not panic and carries no payloads.
    Catalog::warn_changes(None, &catalog);
}

#[test]
fn unknown_policy_keys_are_accepted() {
    let config = crate::config::Config::new(std::env::temp_dir());
    let s = config
        .settings_from_bytes(
            json!({"bots":{},"admission":{"recovery_concurrency":2,"future_knob":true},
                "retention":{"runs_days":7,"later":1}})
            .to_string()
            .as_bytes(),
        )
        .expect("unknown admission and retention keys are ignored");
    assert_eq!(s.admission.recovery_concurrency, 2);
    assert_eq!(s.retention.runs_days, 7);
    assert!(s.admission.unknown.contains_key("future_knob"));
    // Unknown keys survive a rewrite of bots.json.
    let written = serde_json::to_value(&s).unwrap();
    assert_eq!(written["admission"]["future_knob"], true);
}
