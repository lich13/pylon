use crate::{
    config::{Bot, Config},
    db::Store,
};
use anyhow::{Context, Result};
use serde_json::{Value, json};
use std::{
    net::{IpAddr, Ipv4Addr, Ipv6Addr},
    sync::atomic::{AtomicU64, Ordering},
    time::Duration,
};
use tokio::sync::watch;

static PROXY_INVALID: AtomicU64 = AtomicU64::new(0);
static RESPONSE_INVALID: AtomicU64 = AtomicU64::new(0);
pub(crate) fn invalid_responses() -> (u64, u64) {
    (
        PROXY_INVALID.load(Ordering::Relaxed),
        RESPONSE_INVALID.load(Ordering::Relaxed),
    )
}
fn invalid_response(status: u16, read_only: bool) {
    if !read_only {
        let counter = if matches!(status, 502..=504) {
            &PROXY_INVALID
        } else {
            &RESPONSE_INVALID
        };
        counter.fetch_add(1, Ordering::Relaxed);
    }
}

pub fn numeric_id(value: &Value) -> String {
    value
        .as_i64()
        .map(|v| v.to_string())
        .or_else(|| value.as_str().map(str::to_owned))
        .unwrap_or_default()
}
pub fn authorized(owner: &str, message: &Value) -> bool {
    !owner.is_empty()
        && message["chat"]["type"] == "private"
        && numeric_id(&message["chat"]["id"]) == owner
        && numeric_id(&message["from"]["id"]) == owner
        && message["from"]["is_bot"] != true
}
#[derive(Debug)]
pub struct Failure {
    pub status: &'static str,
    pub reason: String,
    pub delay: f64,
}
impl std::fmt::Display for Failure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.reason)
    }
}
impl std::error::Error for Failure {}

#[derive(Clone)]
pub struct Telegram {
    http: reqwest::Client,
    token: String,
    base: String,
}
impl Telegram {
    #[cfg(test)]
    pub(crate) fn fixture(base: String) -> Self {
        assert_eq!(
            reqwest::Url::parse(&base).unwrap().host_str(),
            Some("127.0.0.1")
        );
        Self {
            http: reqwest::Client::builder()
                .no_proxy()
                .timeout(Duration::from_secs(4))
                .build()
                .unwrap(),
            token: "fixture".into(),
            base,
        }
    }
    pub fn new(config: &Config, bot: &Bot) -> Result<Self> {
        Self::with_family(config, bot, &config.settings()?.telegram_ip_family)
    }
    pub fn with_family(config: &Config, bot: &Bot, family: &str) -> Result<Self> {
        Self::with_token(config, family, config.token(bot)?)
    }
    pub(crate) fn with_token(config: &Config, family: &str, token: String) -> Result<Self> {
        let base = if config.offline {
            std::env::var("PYLON_TEST_TELEGRAM").context("offline Telegram endpoint required")?
        } else {
            "https://api.telegram.org".into()
        };
        if config.offline {
            anyhow::ensure!(
                reqwest::Url::parse(&base)?.host_str() == Some("127.0.0.1"),
                "test endpoint must use loopback"
            )
        }
        let builder = reqwest::Client::builder();
        let builder = match family {
            "ipv4" => builder.local_address(IpAddr::V4(Ipv4Addr::UNSPECIFIED)),
            "ipv6" => builder.local_address(IpAddr::V6(Ipv6Addr::UNSPECIFIED)),
            _ => builder,
        };
        let http = builder
            .no_proxy()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(45))
            .build()?;
        Ok(Self { http, token, base })
    }
    async fn response(
        &self,
        result: std::result::Result<reqwest::Response, reqwest::Error>,
        read_only: bool,
    ) -> std::result::Result<Value, Failure> {
        let pending = |s: String| Failure {
            status: "pending",
            reason: s,
            delay: 30.0,
        };
        let uncertain = |s: String| Failure {
            status: if read_only { "pending" } else { "uncertain" },
            reason: s,
            delay: 30.0,
        };
        let response = result.map_err(|e| {
            if e.is_connect() {
                pending("连接 Telegram 失败".into())
            } else {
                uncertain("Telegram 回执未确认".into())
            }
        })?;
        let status = response.status().as_u16();
        let value: Value = response.json().await.map_err(|_| {
            invalid_response(status, read_only);
            uncertain(format!("Telegram HTTP {status}: invalid response"))
        })?;
        if value["ok"] == true
            && value.get("result").is_some_and(|v| !v.is_null())
            && (200..300).contains(&status)
        {
            return Ok(value["result"].clone());
        }
        if value["ok"] != false || value["error_code"].as_u64().is_none() {
            invalid_response(status, read_only);
            return Err(uncertain(format!(
                "Telegram HTTP {status}: invalid response"
            )));
        }
        let code = value["error_code"].as_u64().unwrap_or(status as u64);
        // Telegram descriptions can contain request data. Persist only the code.
        Err(Failure {
            status: if code == 429 {
                "pending"
            } else if code >= 500 {
                if read_only { "pending" } else { "uncertain" }
            } else {
                "failed"
            },
            reason: format!("Telegram API {code}"),
            delay: if code == 429 {
                value["parameters"]["retry_after"]
                    .as_u64()
                    .filter(|value| *value <= 31_536_000)
                    .unwrap_or(30)
                    .max(1) as f64
            } else {
                30.0
            },
        })
    }
    pub async fn call(
        &self,
        method: &str,
        value: &Value,
        read_only: bool,
    ) -> std::result::Result<Value, Failure> {
        self.response(
            self.http
                .post(format!("{}/bot{}/{method}", self.base, self.token))
                .json(value)
                .send()
                .await,
            read_only,
        )
        .await
    }
    pub async fn document(&self, chat: &str, payload: &Value) -> Result<Value> {
        let path = std::path::Path::new(payload["path"].as_str().context("attachment missing")?);
        let bytes = tokio::fs::read(path).await?;
        anyhow::ensure!(
            crate::digest(&bytes) == payload["sha256"].as_str().unwrap_or(""),
            "attachment checksum mismatch"
        );
        let form = reqwest::multipart::Form::new()
            .text("chat_id", chat.to_owned())
            .text(
                "caption",
                payload["caption"]
                    .as_str()
                    .unwrap_or("")
                    .chars()
                    .take(1024)
                    .collect::<String>(),
            )
            .part(
                "document",
                reqwest::multipart::Part::bytes(bytes).file_name(
                    path.file_name()
                        .context("invalid attachment")?
                        .to_string_lossy()
                        .into_owned(),
                ),
            );
        Ok(self
            .response(
                self.http
                    .post(format!("{}/bot{}/sendDocument", self.base, self.token))
                    .multipart(form)
                    .send()
                    .await,
                false,
            )
            .await?)
    }
    pub async fn download(&self, file_id: &str) -> Result<String> {
        let info = self
            .call("getFile", &json!({"file_id":file_id}), true)
            .await?;
        let path = info["file_path"].as_str().context("file unavailable")?;
        anyhow::ensure!(
            path.len() < 1024
                && !path.starts_with('/')
                && !path.contains("..")
                && path
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"_./-".contains(&c)),
            "invalid download path"
        );
        anyhow::ensure!(
            info["file_size"].as_u64().unwrap_or(u64::MAX) <= 2_000_000,
            "document too large"
        );
        let mut response = self
            .http
            .get(format!("{}/file/bot{}/{path}", self.base, self.token))
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("download unavailable"))?;
        anyhow::ensure!(response.status().is_success(), "download unavailable");
        let mut bytes = vec![];
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| anyhow::anyhow!("download incomplete"))?
        {
            anyhow::ensure!(bytes.len() + chunk.len() <= 2_000_000, "document too large");
            bytes.extend_from_slice(&chunk);
        }
        String::from_utf8(bytes).context("document must contain UTF-8 text")
    }
}

fn escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}
/// Split rendered text on UTF-16 boundaries, closing and reopening formatting.
pub fn chunks(text: &str) -> Vec<String> {
    use pulldown_cmark::{Event, Parser, Tag, TagEnd};
    struct Split {
        parts: Vec<String>,
        buf: String,
        stack: Vec<(&'static str, String)>,
        count: usize,
    }
    impl Split {
        fn flush(&mut self) {
            if self.count == 0 {
                return;
            }
            let mut s = self.buf.clone();
            for (t, _) in self.stack.iter().rev() {
                s.push_str(&format!("</{t}>"))
            }
            self.parts.push(s);
            self.buf = self.stack.iter().map(|(_, s)| s.as_str()).collect();
            self.count = 0;
        }
        fn text(&mut self, s: &str) {
            for c in s.chars() {
                if self.count + c.len_utf16() > 3900 {
                    self.flush()
                }
                self.buf.push_str(&escape(&c.to_string()));
                self.count += c.len_utf16();
            }
        }
        fn open(&mut self, tag: &'static str, html: String) {
            self.buf.push_str(&html);
            self.stack.push((tag, html));
        }
        fn close(&mut self) {
            if let Some((t, _)) = self.stack.pop() {
                self.buf.push_str(&format!("</{t}>"))
            }
        }
    }
    let mut s = Split {
        parts: vec![],
        buf: String::new(),
        stack: vec![],
        count: 0,
    };
    let mut lists: Vec<Option<u64>> = vec![];
    for e in Parser::new(text) {
        match e {
            Event::Text(t) | Event::Html(t) | Event::InlineHtml(t) => s.text(&t),
            Event::Code(t) => {
                s.open("code", "<code>".into());
                s.text(&t);
                s.close()
            }
            Event::Start(Tag::Strong | Tag::Heading { .. }) => s.open("b", "<b>".into()),
            Event::Start(Tag::Emphasis) => s.open("i", "<i>".into()),
            Event::Start(Tag::Link { dest_url, .. }) => {
                s.open("a", format!("<a href=\"{}\">", escape(&dest_url)))
            }
            Event::Start(Tag::CodeBlock(_)) => s.open("pre", "<pre>".into()),
            Event::End(TagEnd::Strong | TagEnd::Emphasis | TagEnd::Link) => s.close(),
            Event::End(TagEnd::Heading(_) | TagEnd::CodeBlock) => {
                s.close();
                s.text("\n")
            }
            Event::Start(Tag::List(n)) => lists.push(n),
            Event::End(TagEnd::List(_)) => {
                lists.pop();
                s.text("\n")
            }
            Event::Start(Tag::Item) => {
                s.text(&"  ".repeat(lists.len().saturating_sub(1)));
                if let Some(Some(n)) = lists.last_mut() {
                    s.text(&format!("{n}. "));
                    *n += 1
                } else {
                    s.text("• ")
                }
            }
            Event::SoftBreak
            | Event::HardBreak
            | Event::Rule
            | Event::End(TagEnd::Paragraph | TagEnd::Item) => s.text("\n"),
            _ => {}
        }
    }
    s.flush();
    if s.parts.is_empty() {
        s.parts.push("（空消息）".into())
    }
    s.parts
}

fn valid_receipt(value: &Value, owner: &str) -> Result<()> {
    if !value["message_id"].as_i64().is_some_and(|id| id > 0)
        || value["chat"]["id"].as_i64().is_none()
        || numeric_id(&value["chat"]["id"]) != owner
    {
        return Err(Failure {
            status: "uncertain",
            reason: "Telegram receipt invalid".into(),
            delay: 0.0,
        }
        .into());
    }
    Ok(())
}

pub async fn deliver(store: &Store, telegram: &Telegram, bot: &Bot, event: &Value) -> Result<f64> {
    let (_sender, mut stop) = watch::channel(false);
    deliver_with_stop(store, telegram, bot, event, &mut stop).await
}

#[derive(Debug)]
struct Deferred;
impl std::fmt::Display for Deferred {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("delivery completion deferred")
    }
}
impl std::error::Error for Deferred {}

fn interrupted() -> anyhow::Error {
    Failure {
        status: "uncertain",
        reason: "delivery interrupted before acknowledgement".into(),
        delay: 0.0,
    }
    .into()
}

async fn write_with_stop(
    request: impl std::future::Future<Output = Result<Value>>,
    stop: &mut watch::Receiver<bool>,
) -> Result<Value> {
    tokio::pin!(request);
    tokio::select! {
        biased;
        result = &mut request => result,
        _ = stop.changed() => {
            // A response already arriving at shutdown can still establish a
            // receipt. Bound that drain; never issue a second network request.
            tokio::time::timeout(Duration::from_secs(1), &mut request)
                .await.unwrap_or_else(|_| Err(interrupted()))
        },
    }
}

async fn checkpoint(
    store: &Store,
    event: &Value,
    status: &str,
    receipt: Option<Value>,
    reason: &str,
    delay: f64,
    stop: &mut watch::Receiver<bool>,
) -> Result<()> {
    let record = crate::completion::Completion::delivery(event, status, receipt, reason, delay)?;
    match crate::completion::persist(store, record, stop).await {
        Ok(true) => Ok(()),
        Ok(false) => Err(Deferred.into()),
        Err(error) => {
            // Never reinterpret a completion-write failure as a failed remote send.
            crate::errors::log("delivery-checkpoint", &error);
            Err(crate::errors::ReconcileRequired.into())
        }
    }
}

pub(crate) async fn deliver_with_stop(
    store: &Store,
    telegram: &Telegram,
    bot: &Bot,
    event: &Value,
    stop: &mut watch::Receiver<bool>,
) -> Result<f64> {
    let _hold = crate::completion::Hold::delivery(store, event, stop)
        .await
        .context(crate::errors::ReconcileRequired)?;
    crate::completion::stage(
        store,
        crate::completion::Completion::delivery(
            event,
            "uncertain",
            event.get("receipt").filter(|v| !v.is_null()).cloned(),
            "delivery interrupted before acknowledgement",
            0.0,
        )?,
    )
    .await
    .context(crate::errors::ReconcileRequired)?;
    let payload = &event["payload"];
    let result: Result<Value> = async {
        let check_expiry = || -> Result<()> {
            if payload["expires_at"].as_f64().is_some_and(|v| v <= crate::now()) {
                return Err(Failure { status: "suppressed", reason: "notification expired".into(), delay: 0.0 }.into());
            }
            Ok(())
        };
        if *stop.borrow() {
            return Err(Failure { status: "pending", reason: "stopped before send".into(), delay: 0.0 }.into());
        }
        check_expiry()?;
        match event["kind"].as_str().unwrap_or("") {
            "text" => {
                let mut receipt = event["receipt"].as_array().cloned().unwrap_or_default();
                let parts = chunks(payload["text"].as_str().unwrap_or(""));
                if receipt.len() == parts.len() {
                    checkpoint(store, event, "delivered", Some(json!(receipt)), "", 0.0, stop).await?;
                    return Ok(json!(receipt));
                }
                anyhow::ensure!(receipt.len() < parts.len(), "receipt exceeds message parts");
                for (i,part) in parts.iter().enumerate().skip(receipt.len()) {
                    check_expiry()?;
                    let mut data=json!({"chat_id":bot.owner,"text":part,"parse_mode":"HTML","link_preview_options":{"is_disabled":true}});
                    if i+1==parts.len() && !payload["reply_markup"].is_null(){data["reply_markup"]=payload["reply_markup"].clone()}
                    if i==0 && payload["reply_to_message_id"].is_number(){data["reply_parameters"]=json!({"message_id":payload["reply_to_message_id"]})}
                    if *stop.borrow() { return Err(interrupted()); }
                    let sent = write_with_stop(async { Ok(telegram.call("sendMessage", &data, false).await?) }, stop).await?;
                    valid_receipt(&sent, &bot.owner)?;
                    receipt.push(json!({"message_id":sent["message_id"],"chat_id":sent["chat"]["id"]}));
                    let status = if i + 1 == parts.len() { "delivered" } else { "sending" };
                    checkpoint(store, event, status, Some(json!(receipt)), "", 0.0, stop).await?;
                    if i + 1 < parts.len() {
                        tokio::select! {
                            _ = stop.changed() => return Err(interrupted()),
                            _ = tokio::time::sleep(Duration::from_millis(1100)) => {},
                        }
                    }
                }
                Ok(json!(receipt))
            },
            "document" => {
                let sent = write_with_stop(telegram.document(&bot.owner, payload), stop).await?;
                valid_receipt(&sent, &bot.owner)?;
                Ok(json!({"message_id":sent["message_id"],"chat_id":sent["chat"]["id"],"document":sent["document"]}))
            },
            "bark" => write_with_stop(async {
                match tokio::time::timeout(Duration::from_secs(60), crate::bark::send(payload)).await {
                    Ok(result) => result,
                    Err(_) => Err(Failure { status:"uncertain", reason:"Bark total deadline exceeded; reconcile before retry".into(), delay:0.0 }.into()),
                }
            }, stop).await,
            _=>anyhow::bail!("unsupported delivery kind")
        }
    }.await;
    match result {
        Ok(receipt) => {
            if event["kind"] != "text" {
                let result =
                    checkpoint(store, event, "delivered", Some(receipt), "", 0.0, stop).await;
                if let Err(error) = result
                    && !error.is::<Deferred>()
                {
                    return Err(error);
                }
            }
            Ok(1.1)
        }
        Err(error) => {
            if error.is::<Deferred>() {
                return Ok(1.1);
            }
            if error.is::<crate::errors::ReconcileRequired>() {
                return Err(error);
            }
            let failure = error.downcast_ref::<Failure>();
            let status = failure.map(|f| f.status).unwrap_or("failed");
            let reason = failure
                .map(|f| f.reason.as_str())
                .unwrap_or("delivery validation failed");
            let mut delay = failure.map(|f| f.delay).unwrap_or(0.0);
            if status == "pending" {
                let attempts = event["attempts"]
                    .as_u64()
                    .unwrap_or(1)
                    .saturating_sub(1)
                    .min(4);
                delay = delay.max((30.0 * 2_f64.powi(attempts as i32)).min(300.0));
            }
            let result = checkpoint(store, event, status, None, reason, delay, stop).await;
            if let Err(error) = result
                && !error.is::<Deferred>()
            {
                return Err(error);
            }
            Ok(if status == "pending" {
                delay.max(1.1)
            } else {
                1.1
            })
        }
    }
}

#[cfg(test)]
mod stability_tests {
    use super::*;

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn receipt_is_retained_while_database_is_locked() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let temporary = tempfile::tempdir().unwrap();
        let store = Store::new(&temporary.path().join("state.sqlite3")).unwrap();
        store
            .submit(
                "fixture",
                &json!({"id":"receipt-fixture","kind":"text","payload":{"text":"fixture"}}),
                "fixture",
            )
            .unwrap();
        let event = store.claim("fixture").unwrap().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let blocked = store.connect().unwrap();
        blocked.execute_batch("BEGIN IMMEDIATE").unwrap();
        let server = tokio::spawn(async move {
            let (mut connection, _) = listener.accept().await.unwrap();
            let mut request = [0; 8192];
            assert!(connection.read(&mut request).await.unwrap() > 0);
            let body = r#"{"ok":true,"result":{"message_id":7,"chat":{"id":101}}}"#;
            connection
                .write_all(
                    format!(
                        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        body.len(),
                        body
                    )
                    .as_bytes(),
                )
                .await
                .unwrap();
            // A repeated HTTP send would be accepted here and fail the regression.
            assert!(
                tokio::time::timeout(Duration::from_millis(1300), listener.accept())
                    .await
                    .is_err()
            );
        });
        let client = Telegram {
            http: reqwest::Client::builder().no_proxy().build().unwrap(),
            token: "fixture".into(),
            base: format!("http://{address}"),
        };
        let bot = Bot {
            require_start: false,
            owner: "101".into(),
            credential: "fixture".into(),
            username: String::new(),
            enabled: true,
            menus: vec![],
        };
        let sending_store = store.clone();
        let send =
            tokio::spawn(async move { deliver(&sending_store, &client, &bot, &event).await });
        tokio::time::sleep(Duration::from_millis(900)).await;
        assert!(!send.is_finished());
        blocked.execute_batch("ROLLBACK").unwrap();
        send.await.unwrap().unwrap();
        server.await.unwrap();
        let result = store.event("receipt-fixture").unwrap();
        assert_eq!(result["status"], "delivered");
        assert_eq!(result["receipt"][0]["message_id"], 7);
        assert_eq!(result["attempts"], 1);
    }
}
