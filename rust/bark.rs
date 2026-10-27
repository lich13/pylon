//! Bark endpoints are untrusted module input. Validate DNS and pin the connection.
use crate::telegram::Failure;
use anyhow::{Context, Result, ensure};
use reqwest::{Client, Url};
use serde_json::{Value, json};
use std::{
    net::{IpAddr, SocketAddr},
    time::Duration,
};

const DNS_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Debug)]
pub struct DnsUnavailable;
impl std::fmt::Display for DnsUnavailable {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Bark name resolution unavailable")
    }
}
impl std::error::Error for DnsUnavailable {}

fn public_address(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => {
            let [a, b, c, _] = ip.octets();
            !(a == 0
                || a == 10
                || a == 127
                || a >= 224
                || (a == 100 && (64..=127).contains(&b))
                || (a == 169 && b == 254)
                || (a == 172 && (16..=31).contains(&b))
                || (a == 192
                    && (b == 168 || (b == 0 && (c == 0 || c == 2)) || (b == 88 && c == 99)))
                || (a == 198 && (b == 18 || b == 19 || (b == 51 && c == 100)))
                || (a == 203 && b == 0 && c == 113))
        }
        IpAddr::V6(ip) => {
            let s = ip.segments();
            // Only native global unicast. Exclude translation/tunnelling and special-use ranges.
            (s[0] & 0xe000) == 0x2000
                && !(s[0] == 0x2001 && (s[1] < 0x0200 || s[1] == 0x0db8))
                && s[0] != 0x2002
                && !(s[0] == 0x3fff && (s[1] & 0xf000) == 0)
        }
    }
}

fn endpoint(base: &str) -> Result<Url> {
    let mut url = Url::parse(base).context("invalid Bark endpoint")?;
    ensure!(
        url.scheme() == "https"
            && url.username().is_empty()
            && url.password().is_none()
            && url.host_str().is_some(),
        "invalid Bark endpoint"
    );
    let host = url
        .host_str()
        .unwrap()
        .trim_start_matches('[')
        .trim_end_matches(']');
    if let Ok(ip) = host.parse::<IpAddr>() {
        ensure!(public_address(ip), "Bark endpoint is not public");
    }
    url.set_path(&format!("{}/push", url.path().trim_end_matches('/')));
    Ok(url)
}

#[derive(Debug)]
struct Destination {
    url: Url,
    addresses: Vec<SocketAddr>,
}

impl Destination {
    fn checked(url: Url, addresses: Vec<SocketAddr>) -> Result<Self> {
        ensure!(!addresses.is_empty(), DnsUnavailable);
        ensure!(
            addresses.iter().all(|a| public_address(a.ip())),
            "Bark endpoint is not public"
        );
        Ok(Self { url, addresses })
    }

    fn client_builder(&self) -> Result<reqwest::ClientBuilder> {
        let host = self.url.host_str().context("Bark host missing")?;
        Ok(Client::builder()
            .no_proxy()
            .https_only(true)
            .redirect(reqwest::redirect::Policy::none())
            .resolve_to_addrs(host, &self.addresses)
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(45)))
    }

    fn client(&self) -> Result<Client> {
        Ok(self.client_builder()?.build()?)
    }
}

async fn prepare(base: &str) -> Result<Destination> {
    let url = endpoint(base)?;
    let host = url
        .host_str()
        .context("Bark host missing")?
        .trim_start_matches('[')
        .trim_end_matches(']');
    let port = url.port_or_known_default().context("Bark port missing")?;
    let addresses = if let Ok(ip) = host.parse::<IpAddr>() {
        vec![SocketAddr::new(ip, port)]
    } else {
        tokio::time::timeout(DNS_TIMEOUT, tokio::net::lookup_host((host, port)))
            .await
            .map_err(|_| DnsUnavailable)?
            .map_err(|_| DnsUnavailable)?
            .collect()
    };
    Destination::checked(url, addresses)
}

pub async fn validate(base: &str) -> Result<()> {
    prepare(base).await.map(|_| ())
}

fn network_failure(error: reqwest::Error) -> Failure {
    Failure {
        status: if error.is_connect() {
            "pending"
        } else {
            "uncertain"
        },
        reason: "Bark network error".into(),
        delay: 30.0,
    }
}

async fn response(response: reqwest::Response) -> Result<Value> {
    let status = response.status().as_u16();
    if status == 429 {
        return Err(Failure {
            status: "pending",
            reason: "Bark rate limit".into(),
            delay: 60.0,
        }
        .into());
    }
    let value: Value = response.json().await.map_err(|_| Failure {
        status: "uncertain",
        reason: "Bark response invalid".into(),
        delay: 0.0,
    })?;
    if (200..300).contains(&status)
        && (!value.is_object()
            || value["code"].is_null()
            || !(value["code"].is_i64() || value["code"].is_string()))
    {
        return Err(Failure {
            status: "uncertain",
            reason: "Bark response invalid".into(),
            delay: 0.0,
        }
        .into());
    }
    if !(200..300).contains(&status)
        || (!matches!(value["code"].as_i64(), Some(0 | 200)) && value["code"] != "200")
    {
        return Err(Failure {
            status: if status >= 500 { "uncertain" } else { "failed" },
            reason: format!("Bark HTTP {status}"),
            delay: 0.0,
        }
        .into());
    }
    Ok(json!({"http_status": status, "code": value["code"]}))
}

pub async fn send(payload: &Value) -> Result<Value> {
    // Compiled only into unit tests; the shipped gateway has no bypass or test setting.
    #[cfg(test)]
    if let Some(endpoint) = payload["fixture_transport"].as_str() {
        let url = Url::parse(endpoint)?;
        ensure!(
            url.scheme() == "http" && url.host_str() == Some("127.0.0.1"),
            "invalid fixture transport"
        );
        return response(
            Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(Duration::from_secs(4))
                .build()?
                .post(url)
                .json(&payload["body"])
                .send()
                .await
                .map_err(network_failure)?,
        )
        .await;
    }
    let base = payload["api_base"]
        .as_str()
        .context("Bark endpoint missing")?;
    let destination = prepare(base).await.map_err(|e| {
        if e.is::<DnsUnavailable>() {
            anyhow::Error::from(Failure {
                status: "pending",
                reason: "Bark name resolution unavailable".into(),
                delay: 30.0,
            })
        } else {
            e
        }
    })?;
    let result = destination
        .client()?
        .post(destination.url)
        .json(&payload["body"])
        .send()
        .await
        .map_err(network_failure)?;
    response(result).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn address_policy_and_path() {
        for addr in [
            "0.0.0.0",
            "0.1.2.3",
            "127.0.0.1",
            "10.2.3.4",
            "172.16.1.1",
            "192.168.1.1",
            "100.64.0.1",
            "169.254.169.254",
            "192.0.0.1",
            "192.0.2.1",
            "198.18.0.1",
            "198.51.100.1",
            "203.0.113.1",
            "224.0.0.1",
            "240.0.0.1",
            "::",
            "::1",
            "fc00::1",
            "fe80::1",
            "ff02::1",
            "::ffff:127.0.0.1",
            "64:ff9b::a00:1",
            "2002:7f00:1::",
            "2001::1",
            "2001:db8::1",
            "3fff::1",
        ] {
            assert!(!public_address(addr.parse().unwrap()), "{addr}");
        }
        for addr in ["93.184.216.34", "2606:4700::1111"] {
            assert!(public_address(addr.parse().unwrap()));
        }
        assert_eq!(
            endpoint("https://example.invalid/prefix/")
                .unwrap()
                .as_str(),
            "https://example.invalid/prefix/push"
        );
        for base in [
            "http://example.invalid",
            "https://user@example.invalid",
            "https://example.invalid@127.0.0.1",
            "https://2130706433",
            "https://0x7f000001",
            "https://[::ffff:127.0.0.1]",
        ] {
            assert!(endpoint(base).is_err(), "{base}");
        }
    }

    #[test]
    fn all_dns_results_are_checked_and_fixed() {
        let url = endpoint("https://example.invalid/prefix").unwrap();
        let public: SocketAddr = "93.184.216.34:443".parse().unwrap();
        let private: SocketAddr = "127.0.0.1:443".parse().unwrap();
        assert!(Destination::checked(url.clone(), vec![public, private]).is_err());
        assert!(
            Destination::checked(url.clone(), vec![])
                .unwrap_err()
                .is::<DnsUnavailable>()
        );
        let approved = Destination::checked(url.clone(), vec![public]).unwrap();
        assert_eq!(approved.addresses, [public]);
        // A later DNS answer never changes the approved request; the next send validates again.
        assert!(Destination::checked(url, vec![private]).is_err());
    }

    #[tokio::test]
    async fn pinned_transport_does_not_resolve_again_or_follow_redirects() {
        use std::sync::{
            Arc,
            atomic::{AtomicUsize, Ordering},
        };
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        struct RebindingDns(Arc<AtomicUsize>);
        impl reqwest::dns::Resolve for RebindingDns {
            fn resolve(&self, _: reqwest::dns::Name) -> reqwest::dns::Resolving {
                self.0.fetch_add(1, Ordering::SeqCst);
                Box::pin(async {
                    let addresses: reqwest::dns::Addrs =
                        Box::new(["127.0.0.1:1".parse::<SocketAddr>().unwrap()].into_iter());
                    Ok(addresses)
                })
            }
        }

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        // Only this in-process transport fixture bypasses address validation and TLS.
        // Production always calls checked() and keeps https_only enabled.
        let destination = Destination {
            url: Url::parse(&format!(
                "http://example.invalid:{}/prefix/push",
                address.port()
            ))
            .unwrap(),
            addresses: vec![address],
        };
        assert!(
            destination
                .client()
                .unwrap()
                .post(destination.url.clone())
                .send()
                .await
                .unwrap_err()
                .is_builder()
        );
        let calls = Arc::new(AtomicUsize::new(0));
        let client = destination
            .client_builder()
            .unwrap()
            .https_only(false)
            .dns_resolver(Arc::new(RebindingDns(calls.clone())))
            .build()
            .unwrap();
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut request = Vec::new();
            loop {
                request.push(stream.read_u8().await.unwrap());
                if request.ends_with(b"\r\n\r\n") {
                    break;
                }
            }
            assert!(request.starts_with(b"POST /prefix/push HTTP/1.1\r\n"));
            stream.write_all(b"HTTP/1.1 302 Found\r\nLocation: http://metadata.example.invalid/\r\nContent-Length: 12\r\nConnection: close\r\n\r\n{\"code\":200}").await.unwrap();
        });
        let received = client.post(destination.url).send().await.unwrap();
        assert_eq!(received.status().as_u16(), 302);
        assert_eq!(calls.load(Ordering::SeqCst), 0);
        assert_eq!(
            response(received)
                .await
                .unwrap_err()
                .downcast_ref::<Failure>()
                .unwrap()
                .status,
            "failed"
        );
        server.await.unwrap();
    }

    #[tokio::test]
    async fn preserves_delivery_response_classification_without_network() {
        for (status, body, expected) in [
            (200, r#"{"code":200}"#, "delivered"),
            (200, r#"{"code":0}"#, "delivered"),
            (200, r#"{"code":"200"}"#, "delivered"),
            (200, r#"{}"#, "uncertain"),
            (200, r#"null"#, "uncertain"),
            (200, r#"[]"#, "uncertain"),
            (200, r#"{"code":true}"#, "uncertain"),
            (429, "", "pending"),
            (500, r#"{}"#, "uncertain"),
            (400, r#"{}"#, "failed"),
            (200, "broken", "uncertain"),
            (302, r#"{}"#, "failed"),
            (200, r#"{"code":400}"#, "failed"),
        ] {
            let fixture = axum::http::Response::builder()
                .status(status)
                .body(body)
                .unwrap();
            match response(fixture.into()).await {
                Ok(_) => assert_eq!(expected, "delivered"),
                Err(error) => assert_eq!(error.downcast_ref::<Failure>().unwrap().status, expected),
            }
        }
    }
}
