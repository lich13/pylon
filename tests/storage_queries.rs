use pylon::{config::Config, db::Store, queue};
use rusqlite::{Connection, Params, params};
use serde_json::{Value, json};
use std::collections::BTreeSet;

fn index_definition(db: &Connection, name: &str) -> String {
    db.query_row(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        [name],
        |row| row.get(0),
    )
    .unwrap_or_else(|error| panic!("missing index {name}: {error}"))
}

fn explain<P: Params>(db: &Connection, sql: &str, params: P) -> Vec<String> {
    let mut statement = db.prepare(&format!("EXPLAIN QUERY PLAN {sql}")).unwrap();
    statement
        .query_map(params, |row| row.get::<_, String>(3))
        .unwrap()
        .collect::<rusqlite::Result<Vec<_>>>()
        .unwrap()
}

fn insert_event(
    db: &Connection,
    id: &str,
    profile: &str,
    module: &str,
    receipt: Option<Value>,
    callback_data: Option<&str>,
) {
    let payload = callback_data.map_or_else(
        || json!({"text":"fixture"}),
        |data| {
            json!({
                "text":"fixture",
                "reply_markup":{"inline_keyboard":[[{"text":"fixture","callback_data":data}] ]}
            })
        },
    );
    db.execute(
        "INSERT INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,receipt,module) VALUES(?,?,?,?,?,'delivered',1,0,1,1,?,?)",
        params![
            id,
            profile,
            "job-fixture",
            "text",
            payload.to_string(),
            receipt.map(|value| value.to_string()),
            module,
        ],
    )
    .unwrap();
}

fn callback_query(mid: i64, data: &str, owner: i64) -> Value {
    json!({
        "from":{"id":owner,"is_bot":false},
        "message":{"message_id":mid,"chat":{"id":owner,"type":"private"}},
        "data":data
    })
}

fn lookup_rows(
    db: &Connection,
    module: &str,
    profile: &str,
    mid: i64,
    optimized: bool,
) -> BTreeSet<String> {
    let sql = if optimized {
        "SELECT id FROM events WHERE id IN (SELECT id FROM events WHERE module=?1 AND profile=?2 AND receipt IS NOT NULL AND json_extract(receipt,'$[#-1].message_id')=?3 UNION SELECT id FROM events WHERE module=?1 AND profile=?2 AND receipt IS NOT NULL AND json_extract(receipt,'$.message_id')=?3) LIMIT 5"
    } else {
        "SELECT id FROM events WHERE module=?1 AND profile=?2 AND receipt IS NOT NULL AND (json_extract(receipt,'$[#-1].message_id')=?3 OR json_extract(receipt,'$.message_id')=?3) LIMIT 5"
    };
    db.prepare(sql)
        .unwrap()
        .query_map(params![module, profile, mid], |row| row.get::<_, String>(0))
        .unwrap()
        .collect::<rusqlite::Result<BTreeSet<_>>>()
        .unwrap()
}

fn reference_lookup(db: &Connection, module: &str, profile: &str, mid: i64) -> BTreeSet<String> {
    let mut statement = db
        .prepare("SELECT id,module,profile,receipt FROM events")
        .unwrap();
    statement
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, Option<String>>(3)?,
            ))
        })
        .unwrap()
        .filter_map(|row| {
            let (id, event_module, event_profile, raw_receipt) = row.ok()?;
            if event_module != module || event_profile != profile {
                return None;
            }
            let receipt = serde_json::from_str::<Value>(raw_receipt.as_deref()?).ok()?;
            let matches = match receipt {
                Value::Object(ref object) => {
                    object.get("message_id").and_then(Value::as_i64) == Some(mid)
                }
                Value::Array(ref entries) => {
                    entries
                        .last()
                        .and_then(Value::as_object)
                        .and_then(|object| object.get("message_id"))
                        .and_then(Value::as_i64)
                        == Some(mid)
                }
                Value::Null => false,
                _ => false,
            };
            matches.then_some(id)
        })
        .collect()
}

fn insert_run(db: &Connection, id: &str, job_id: &str, started_at: f64, status: &str) {
    db.execute(
        "INSERT INTO runs(id,job_id,started_at,finished_at,status,detail) VALUES(?,?,?,? ,?,?)",
        params![id, job_id, started_at, started_at + 1.0, status, "fixture"],
    )
    .unwrap();
}

fn latest_rows(db: &Connection, optimized: bool) -> BTreeSet<(String, String, String)> {
    let sql = if optimized {
        "WITH RECURSIVE jobs(job) AS (SELECT min(job_id) FROM runs UNION ALL SELECT (SELECT min(job_id) FROM runs WHERE job_id>jobs.job) FROM jobs WHERE job IS NOT NULL) SELECT r.id,r.job_id,r.status FROM jobs JOIN runs r ON r.job_id=jobs.job AND r.started_at=(SELECT max(started_at) FROM runs WHERE job_id=jobs.job)"
    } else {
        "SELECT r.id,r.job_id,r.status FROM runs r JOIN (SELECT job_id,max(started_at) AS started_at FROM runs GROUP BY job_id) latest ON r.job_id=latest.job_id AND r.started_at=latest.started_at"
    };
    db.prepare(sql)
        .unwrap()
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        })
        .unwrap()
        .collect::<rusqlite::Result<BTreeSet<_>>>()
        .unwrap()
}

#[test]
fn queue_and_gateway_query_indexes_exist_and_are_selected() {
    let temporary = tempfile::tempdir().unwrap();
    let config = Config::new(temporary.path().join("home"));
    let executor = queue::connect(&config).unwrap();

    for index in [
        "requests_state_created",
        "requests_created",
        "triggers_request",
    ] {
        assert!(index_definition(&executor, index).starts_with("CREATE INDEX"));
    }
    executor
        .execute(
            "INSERT INTO requests(id,job_id,module,topic,model,state,created_at,updated_at) VALUES('fixture-request','fixture-job','fixture-module','fixture-topic',0,'queued',1,1)",
            [],
        )
        .unwrap();
    executor
        .execute(
            "INSERT INTO triggers(id,request_id) VALUES('fixture-trigger','fixture-request')",
            [],
        )
        .unwrap();
    let plan = explain(
        &executor,
        "SELECT id FROM requests WHERE state=? ORDER BY created_at",
        params!["queued"],
    );
    assert!(
        plan.iter()
            .any(|detail| detail.contains("requests_state_created")),
        "{plan:?}"
    );
    let plan = explain(
        &executor,
        "SELECT id FROM requests ORDER BY created_at DESC LIMIT 5",
        [],
    );
    assert!(
        plan.iter()
            .any(|detail| detail.contains("requests_created")),
        "{plan:?}"
    );
    let plan = explain(
        &executor,
        "SELECT request_id FROM triggers WHERE request_id=?",
        params!["fixture-request"],
    );
    assert!(
        plan.iter()
            .any(|detail| detail.contains("triggers_request")),
        "{plan:?}"
    );
    drop(executor);

    let store = Store::new(&temporary.path().join("gateway.sqlite3")).unwrap();
    let gateway = store.connect().unwrap();
    let last_definition = index_definition(&gateway, "events_receipt_last");
    assert!(last_definition.contains("$[#-1].message_id"));
    let one_definition = index_definition(&gateway, "events_receipt_one");
    assert!(one_definition.contains("$.message_id"));
    assert!(index_definition(&gateway, "updates_pending").contains("status='pending'"));
    insert_event(
        &gateway,
        "index-object",
        "fixture-bot",
        "fixture-module",
        Some(json!({"message_id":700,"chat_id":1001})),
        None,
    );
    insert_event(
        &gateway,
        "index-array",
        "fixture-bot",
        "fixture-module",
        Some(json!([
            {"message_id":699,"chat_id":1001},
            {"message_id":700,"chat_id":1001}
        ])),
        None,
    );
    gateway
        .execute(
            "INSERT INTO updates(profile,update_id,payload,status) VALUES('fixture-bot',1,'{}','pending')",
            [],
        )
        .unwrap();

    let plan = explain(
        &gateway,
        "SELECT id FROM events WHERE profile=? AND receipt IS NOT NULL AND json_extract(receipt,'$[#-1].message_id')=?",
        params!["fixture-bot", 700_i64],
    );
    assert!(
        plan.iter()
            .any(|detail| detail.contains("events_receipt_last")),
        "{plan:?}"
    );
    let plan = explain(
        &gateway,
        "SELECT id FROM events WHERE profile=? AND receipt IS NOT NULL AND json_extract(receipt,'$.message_id')=?",
        params!["fixture-bot", 700_i64],
    );
    assert!(
        plan.iter()
            .any(|detail| detail.contains("events_receipt_one")),
        "{plan:?}"
    );
    let plan = explain(
        &gateway,
        "SELECT update_id FROM updates WHERE profile=? AND status='pending' ORDER BY update_id",
        params!["fixture-bot"],
    );
    assert!(
        plan.iter().any(|detail| detail.contains("updates_pending")),
        "{plan:?}"
    );
}

#[test]
fn queue_summary_preserves_recent_and_active_rows_in_busy_quiet_and_sparse_mailboxes() {
    let temporary = tempfile::tempdir().unwrap();
    let config = Config::new(temporary.path().to_owned());
    let db = queue::connect(&config).unwrap();
    let now = pylon::now();
    for n in 0..615 {
        let state = if n % 3 == 0 { "running" } else { "completed" };
        let created = now - 172800.0 + f64::from(n / 3);
        let updated = if n % 3 == 1 { now } else { created };
        db.execute("INSERT INTO requests(id,job_id,module,topic,model,state,created_at,updated_at) VALUES(?,?,'fixture','fixture',0,?,?,?)", params![format!("request-{n}"),format!("job-{n}"),state,created,updated]).unwrap();
    }
    for scenario in ["busy", "quiet", "sparse"] {
        if scenario == "quiet" {
            db.execute(
                "UPDATE requests SET state='completed',updated_at=created_at",
                [],
            )
            .unwrap();
        } else if scenario == "sparse" {
            db.execute(
                "UPDATE requests SET state='unknown' WHERE id='request-0'",
                [],
            )
            .unwrap();
            db.execute(
                "UPDATE requests SET state='running' WHERE id='request-1'",
                [],
            )
            .unwrap();
            db.execute(
                "UPDATE requests SET updated_at=? WHERE id='request-214'",
                [now],
            )
            .unwrap();
        }
        let expected = db.prepare("SELECT id FROM requests WHERE state!='completed' OR updated_at>? ORDER BY +created_at DESC LIMIT 200").unwrap().query_map([now-86400.0], |r| r.get::<_, String>(0)).unwrap().collect::<rusqlite::Result<Vec<_>>>().unwrap();
        queue::summary_flush(&config).unwrap();
        let actual = queue::public(&config)["requests"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| r["id"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>();
        assert_eq!(actual, expected, "{scenario}");
    }
}

#[test]
fn callback_matching_keeps_object_array_null_and_duplicate_mid_semantics() {
    let temporary = tempfile::tempdir().unwrap();
    let store = Store::new(&temporary.path().join("gateway.sqlite3")).unwrap();
    let db = store.connect().unwrap();
    insert_event(
        &db,
        "callback-object",
        "bot-a",
        "module-a",
        Some(json!({"message_id":700,"chat_id":101})),
        Some("object"),
    );
    insert_event(
        &db,
        "callback-object-duplicate",
        "bot-a",
        "module-b",
        Some(json!({"message_id":700,"chat_id":101})),
        Some("duplicate"),
    );
    insert_event(
        &db,
        "callback-array-last",
        "bot-a",
        "module-a",
        Some(json!([
            {"message_id":699,"chat_id":101},
            {"message_id":700,"chat_id":101}
        ])),
        Some("array-last"),
    );
    insert_event(
        &db,
        "callback-array-earlier",
        "bot-a",
        "module-a",
        Some(json!([
            {"message_id":700,"chat_id":101},
            {"message_id":701,"chat_id":101}
        ])),
        Some("array-earlier"),
    );
    insert_event(
        &db,
        "callback-null",
        "bot-a",
        "module-a",
        None,
        Some("null"),
    );
    insert_event(
        &db,
        "callback-other-bot",
        "bot-b",
        "module-a",
        Some(json!({"message_id":700,"chat_id":101})),
        Some("other-bot"),
    );
    drop(db);

    assert!(
        store
            .own_callback("bot-a", "101", &callback_query(700, "object", 101))
            .unwrap()
    );
    assert!(
        store
            .own_callback("bot-a", "101", &callback_query(700, "duplicate", 101))
            .unwrap()
    );
    assert!(
        store
            .own_callback("bot-a", "101", &callback_query(700, "array-last", 101))
            .unwrap()
    );
    assert!(
        !store
            .own_callback("bot-a", "101", &callback_query(700, "array-earlier", 101))
            .unwrap()
    );
    assert!(
        !store
            .own_callback("bot-a", "101", &callback_query(700, "null", 101))
            .unwrap()
    );
    assert!(
        !store
            .own_callback("bot-a", "101", &callback_query(700, "other-bot", 101))
            .unwrap()
    );
}

#[test]
fn lookup_union_matches_the_reference_for_receipt_shapes_duplicate_mid_and_scope() {
    let temporary = tempfile::tempdir().unwrap();
    let store = Store::new(&temporary.path().join("gateway.sqlite3")).unwrap();
    let db = store.connect().unwrap();
    insert_event(
        &db,
        "lookup-object",
        "bot-a",
        "module-a",
        Some(json!({"message_id":700,"chat_id":101})),
        None,
    );
    insert_event(
        &db,
        "lookup-object-duplicate",
        "bot-a",
        "module-a",
        Some(json!({"message_id":700,"chat_id":101})),
        None,
    );
    insert_event(
        &db,
        "lookup-array-last",
        "bot-a",
        "module-a",
        Some(json!([
            {"message_id":699,"chat_id":101},
            {"message_id":700,"chat_id":101}
        ])),
        None,
    );
    insert_event(
        &db,
        "lookup-array-earlier",
        "bot-a",
        "module-a",
        Some(json!([
            {"message_id":700,"chat_id":101},
            {"message_id":701,"chat_id":101}
        ])),
        None,
    );
    insert_event(&db, "lookup-null", "bot-a", "module-a", None, None);
    insert_event(
        &db,
        "lookup-other-bot",
        "bot-b",
        "module-a",
        Some(json!({"message_id":700,"chat_id":101})),
        None,
    );
    insert_event(
        &db,
        "lookup-other-module",
        "bot-a",
        "module-b",
        Some(json!({"message_id":700,"chat_id":101})),
        None,
    );

    let expected = BTreeSet::from([
        "lookup-array-last".to_owned(),
        "lookup-object".to_owned(),
        "lookup-object-duplicate".to_owned(),
    ]);
    let reference = reference_lookup(&db, "module-a", "bot-a", 700);
    let baseline = lookup_rows(&db, "module-a", "bot-a", 700, false);
    let optimized = lookup_rows(&db, "module-a", "bot-a", 700, true);
    assert_eq!(reference, expected);
    assert_eq!(baseline, reference);
    assert_eq!(optimized, reference);
}

#[test]
fn latest_run_query_keeps_all_max_started_ties_and_nearby_job_names() {
    let temporary = tempfile::tempdir().unwrap();
    let store = Store::new(&temporary.path().join("gateway.sqlite3")).unwrap();
    let db = store.connect().unwrap();
    assert!(latest_rows(&db, true).is_empty());

    insert_run(&db, "alpha-old", "alpha", 1.0, "ok");
    insert_run(&db, "alpha-tie-a", "alpha", 10.0, "failed");
    insert_run(&db, "alpha-tie-b", "alpha", 10.0, "ok");
    insert_run(&db, "alpha-extra", "alpha-extra", 9.0, "ok");
    insert_run(&db, "alpha-extra-tie-a", "alpha-extra", 11.0, "uncertain");
    insert_run(&db, "alpha-extra-tie-b", "alpha-extra", 11.0, "ok");
    insert_run(&db, "alpha2-latest", "alpha2", 7.0, "ok");
    insert_run(&db, "beta-latest", "beta", 8.0, "failed");

    let expected = BTreeSet::from([
        (
            "alpha-tie-a".to_owned(),
            "alpha".to_owned(),
            "failed".to_owned(),
        ),
        (
            "alpha-tie-b".to_owned(),
            "alpha".to_owned(),
            "ok".to_owned(),
        ),
        (
            "alpha-extra-tie-a".to_owned(),
            "alpha-extra".to_owned(),
            "uncertain".to_owned(),
        ),
        (
            "alpha-extra-tie-b".to_owned(),
            "alpha-extra".to_owned(),
            "ok".to_owned(),
        ),
        (
            "alpha2-latest".to_owned(),
            "alpha2".to_owned(),
            "ok".to_owned(),
        ),
        (
            "beta-latest".to_owned(),
            "beta".to_owned(),
            "failed".to_owned(),
        ),
    ]);
    let baseline = latest_rows(&db, false);
    let optimized = latest_rows(&db, true);
    assert_eq!(baseline, expected);
    assert_eq!(optimized, expected);
}
