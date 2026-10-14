use crate::{
    config::{Bot, Config},
    db::Store,
};
use anyhow::{Context, Result};
use serde_json::{Value, json};
use std::{
    net::{IpAddr, Ipv4Addr},
    time::Duration,
};

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
    pub fn new(config: &Config, bot: &Bot) -> Result<Self> {
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
        let http = reqwest::Client::builder()
            .local_address(IpAddr::V4(Ipv4Addr::UNSPECIFIED))
            .no_proxy()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(45))
            .build()?;
        Ok(Self {
            http,
            token: config.token(bot)?,
            base,
        })
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
        let value: Value = response
            .json()
            .await
            .map_err(|_| uncertain(format!("Telegram HTTP {status}: invalid response")))?;
        if value["ok"] == true {
            return Ok(value["result"].clone());
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
                    .as_f64()
                    .unwrap_or(30.0)
                    .max(1.0)
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

pub async fn deliver(store: &Store, telegram: &Telegram, bot: &Bot, event: &Value) -> Result<f64> {
    let id = event["id"].as_str().context("event id missing")?;
    let payload = &event["payload"];
    let result:Result<Value>=async {
        let check_expiry=||->Result<()>{if payload["expires_at"].as_f64().is_some_and(|v|v<=crate::now()){return Err(Failure{status:"suppressed",reason:"notification expired".into(),delay:0.0}.into())}Ok(())};
        check_expiry()?;
        match event["kind"].as_str().unwrap_or("") {
            "text"=>{
                let mut receipt=event["receipt"].as_array().cloned().unwrap_or_default();
                let parts=chunks(payload["text"].as_str().unwrap_or(""));
                for (i,part) in parts.iter().enumerate().skip(receipt.len()) {
                    check_expiry()?;
                    let mut data=json!({"chat_id":bot.owner,"text":part,"parse_mode":"HTML","link_preview_options":{"is_disabled":true}});
                    if i+1==parts.len() && !payload["reply_markup"].is_null(){data["reply_markup"]=payload["reply_markup"].clone()}
                    if i==0 && payload["reply_to_message_id"].is_number(){data["reply_parameters"]=json!({"message_id":payload["reply_to_message_id"]})}
                    let sent=telegram.call("sendMessage",&data,false).await?;
                    receipt.push(json!({"message_id":sent["message_id"],"chat_id":sent["chat"]["id"]}));
                    store.finish(id,"sending",Some(&json!(receipt)),"",0.0)?;
                    if i+1<parts.len(){tokio::time::sleep(Duration::from_millis(1100)).await}
                }
                Ok(json!(receipt))
            },
            "document"=>{let sent=telegram.document(&bot.owner,payload).await?;Ok(json!({"message_id":sent["message_id"],"chat_id":sent["chat"]["id"],"document":sent["document"]}))},
            "bark"=>{
                let base=payload["api_base"].as_str().context("Bark endpoint missing")?;
                let response=telegram.http.post(format!("{}/push",base.trim_end_matches('/'))).json(&payload["body"]).send().await.map_err(|e|Failure{status:if e.is_connect(){"pending"}else{"uncertain"},reason:"Bark network error".into(),delay:30.0})?;
                let status=response.status().as_u16();
                if status==429{return Err(Failure{status:"pending",reason:"Bark rate limit".into(),delay:60.0}.into())}
                let value:Value=response.json().await.map_err(|_|Failure{status:"uncertain",reason:"Bark response invalid".into(),delay:0.0})?;
                if !(200..300).contains(&status)||(!value["code"].is_null()&&!matches!(value["code"].as_i64(),Some(0|200))&&value["code"]!="200"){return Err(Failure{status:if status>=500{"uncertain"}else{"failed"},reason:format!("Bark HTTP {status}"),delay:0.0}.into())}
                Ok(json!({"http_status":status,"code":value["code"]}))
            },
            _=>anyhow::bail!("unsupported delivery kind")
        }
    }.await;
    match result {
        Ok(receipt) => {
            store.finish(id, "delivered", Some(&receipt), "", 0.0)?;
            Ok(1.1)
        }
        Err(error) => {
            let failure = error.downcast_ref::<Failure>();
            let status = failure.map(|f| f.status).unwrap_or("failed");
            let reason = failure
                .map(|f| f.reason.as_str())
                .unwrap_or("delivery validation failed");
            let delay = failure.map(|f| f.delay).unwrap_or(0.0);
            store.finish(id, status, None, reason, delay)?;
            Ok(if status == "pending" {
                delay.max(1.1)
            } else {
                1.1
            })
        }
    }
}
