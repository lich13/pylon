use super::*;
use crate::optimization::{DELIVERY_IDLE, Idle, Lane};
use axum::{
    Json, Router,
    extract::{Path, State},
    routing::post,
};
use std::sync::{Arc, Mutex};

fn fixture() -> (tempfile::TempDir, App, Bot) {
    let temp = tempfile::tempdir().unwrap();
    let config = crate::config::Config::new(temp.path().to_owned());
    let bot = Bot {
        owner: "101".into(),
        credential: "fixture".into(),
        username: String::new(),
        enabled: true,
        require_start: false,
        menus: vec![],
    };
    let mut settings = crate::config::Settings::default();
    settings.bots.insert("fixture".into(), bot.clone());
    let app = App {
        store: crate::db::Store::new(&config.db()).unwrap(),
        config,
        snapshot: Arc::new(tokio::sync::RwLock::new(Snapshot {
            settings,
            catalog: crate::catalog::Catalog::default(),
            credentials: BTreeMap::new(),
        })),
        reload_guard: Arc::default(),
    };
    (temp, app, bot)
}
fn message(id: i64) -> Value {
    json!({"update_id":id,"message":{"message_id":id,"chat":{"id":101,"type":"private"},"from":{"id":101},"text":"fixture"}})
}

#[tokio::test]
async fn command_commit_wakes_only_its_lane_and_duplicates_offsets_and_rollbacks_stay_quiet() {
    let (_temp, app, bot) = fixture();
    let (_command, mut changed) = app
        .store
        .runtime
        .delivery_listener("fixture", Lane::Command);
    let (_telegram, telegram) = app
        .store
        .runtime
        .delivery_listener("fixture", Lane::Telegram);
    let (_other, other) = app.store.runtime.delivery_listener("other", Lane::Command);
    // The commit happens after observing the generation and before waiting.
    changed.borrow_and_update();
    save_updates(&app, "fixture", &bot, &[message(10)]).unwrap();
    tokio::time::timeout(Duration::from_secs(1), changed.changed())
        .await
        .unwrap()
        .unwrap();
    assert!(!telegram.has_changed().unwrap());
    assert!(!other.has_changed().unwrap());
    save_updates(&app, "fixture", &bot, &[message(10)]).unwrap();
    assert!(!changed.has_changed().unwrap());
    let mut unauthorized = message(11);
    unauthorized["message"]["from"]["id"] = json!(999);
    save_updates(&app, "fixture", &bot, &[unauthorized]).unwrap();
    assert!(!changed.has_changed().unwrap());
    assert_eq!(app.store.get("offset:fixture").unwrap(), 12);
    assert!(
        save_updates(
            &app,
            "fixture",
            &bot,
            &[message(12), json!({"update_id":"invalid"})]
        )
        .is_err()
    );
    assert!(!changed.has_changed().unwrap());
    assert_eq!(
        app.store
            .connect()
            .unwrap()
            .query_row("SELECT count(*) FROM updates WHERE update_id=12", [], |r| r
                .get::<_, u64>(0))
            .unwrap(),
        0
    );
}

#[tokio::test(start_paused = true)]
async fn command_idle_backoff_and_external_fallback_keep_bounded_waits_and_reset_after_work() {
    let (_temp, app, bot) = fixture();
    let tg = Telegram::fixture("http://127.0.0.1:9".into());
    let (_stop, rx) = watch::channel(false);
    let mut idle = Idle::default();
    for delay in [0.5, 1.0, 2.0, 2.0] {
        let outcome = lane_once(
            app.clone(),
            "fixture".into(),
            bot.clone(),
            tg.clone(),
            "command",
            rx.clone(),
        )
        .await
        .unwrap();
        assert!(matches!(outcome, LaneOutcome::Empty(None)));
        assert_eq!(idle.next(&DELIVERY_IDLE).as_secs_f64(), delay);
    }
    // A writer with another Store has no in-process notifier. The next timer claim sees it.
    let other = crate::db::Store::new(&app.config.db()).unwrap();
    other
        .connect()
        .unwrap()
        .execute(
            "INSERT INTO updates(profile,update_id,payload,kind) VALUES('fixture',1,?,'message')",
            [message(1)["message"].to_string()],
        )
        .unwrap();
    let outcome = lane_once(app.clone(), "fixture".into(), bot, tg, "command", rx)
        .await
        .unwrap();
    assert!(matches!(outcome, LaneOutcome::Worked(0.0)));
    idle.reset();
    assert_eq!(idle.next(&DELIVERY_IDLE), Duration::from_millis(500));
    assert_eq!(
        app.store.runtime.metrics.snapshot()["command_empty_claims"],
        4
    );
}

#[tokio::test]
async fn unknown_and_expired_callbacks_only_acknowledge_and_never_route_or_reply() {
    let (_temp, app, bot) = fixture();
    type Calls = Arc<Mutex<Vec<(String, Value)>>>;
    let calls: Calls = Arc::default();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    async fn telegram(
        Path(method): Path<String>,
        State(calls): State<Calls>,
        Json(body): Json<Value>,
    ) -> Json<Value> {
        calls.lock().unwrap().push((method, body));
        Json(json!({"ok":true,"result":true}))
    }
    let router = Router::new()
        .route("/botfixture/{method}", post(telegram))
        .with_state(calls.clone());
    let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    let tg = Telegram::fixture(url);
    let event = json!({"id":"fixture-event","kind":"text","payload":{"text":"fixture","reply_markup":{"inline_keyboard":[[{"text":"fixture","callback_data":"unregistered:fixture"}]]}}});
    app.store.submit("fixture", &event, "fixture").unwrap();
    app.store
        .finish(
            "fixture-event",
            "delivered",
            Some(&json!([{"message_id":7,"chat_id":101}])),
            "",
            0.0,
        )
        .unwrap();
    app.store
        .connect()
        .unwrap()
        .execute("UPDATE events SET created_at=1,updated_at=1", [])
        .unwrap();
    let raw = json!({"id":"fixture-event","module":"fixture","profile":"fixture","kind":"text","payload":event["payload"],"job_id":"fixture","status":"delivered","attempts":1,"created_at":1,"updated_at":1,"next_attempt":0,"receipt":[{"message_id":7,"chat_id":101}]});
    let mut gone = crate::event_history::from_event(&raw, crate::now(), 90).unwrap();
    gone.expires_at = crate::now() - 1.0;
    let mut db = app.store.connect().unwrap();
    let tx = db.transaction().unwrap();
    crate::event_history::write(&tx, &gone).unwrap();
    tx.execute("DELETE FROM events WHERE id='fixture-event'", [])
        .unwrap();
    tx.commit().unwrap();
    for (uid, mid) in [(1, 999), (2, 7)] {
        let update = json!({"update_id":uid,"callback_query":{"id":format!("fixture-{uid}"),"from":{"id":101},"message":{"message_id":mid,"chat":{"id":101,"type":"private"}},"data":"unregistered:fixture"}});
        save_updates(&app, "fixture", &bot, &[update]).unwrap();
        assert!(command_once(&app, "fixture", &tg).await.unwrap());
        let status: String = db
            .query_row("SELECT status FROM updates WHERE update_id=?", [uid], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(status, "done");
    }
    let calls = calls.lock().unwrap().clone();
    assert_eq!(calls.len(), 2);
    for (method, body) in calls {
        assert_eq!(method, "answerCallbackQuery");
        assert_eq!(body.as_object().unwrap().len(), 1);
        assert!(body["text"].is_null());
    }
    assert_eq!(
        db.query_row("SELECT count(*) FROM events", [], |r| r.get::<_, u64>(0))
            .unwrap(),
        0
    );
    assert_eq!(
        crate::completion::pending_count(&app.store.path).unwrap(),
        0
    );
    server.abort();
}
