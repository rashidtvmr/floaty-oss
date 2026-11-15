//! Bounded native OpenAI device-authorization transport.
//!
//! This lets SocketStream sign in to a ChatGPT/Codex account without the official
//! Codex CLI being installed. The endpoint and payload contract follows the
//! reference implementation at
//! `opencode-rk-recovery-20261005/crates/providers/src/openai_device_auth.rs`.
//!
//! The browser is never opened here: the caller receives the verification URL
//! and user code and makes the explicit UI choice. Every request is bounded by a
//! hard timeout, redirects are disabled, and response bodies are capped.
//!
//! See `docs/A11Y-TREE-SHORTCUTS.md` section 4.1 for the frozen interface.

use std::fmt;
use std::time::Duration;

use reqwest::{Client, Response, StatusCode};
use serde::{Deserialize, Serialize, Serializer};

#[cfg(test)]
use reqwest::Url;

/// Public client id used by the documented ChatGPT device flow.
pub const CLIENT_ID: &str = "app_EMoamEEZ73f0CkXaXp7hrann";
/// Issuer for every endpoint below.
pub const ISSUER: &str = "https://auth.openai.com";
/// Page the user opens to enter the device code.
pub const DEVICE_LOGIN_URL: &str = "https://auth.openai.com/codex/device";
const DEVICE_AUTH_URL: &str = "https://auth.openai.com/api/accounts/deviceauth/usercode";
const DEVICE_TOKEN_URL: &str = "https://auth.openai.com/api/accounts/deviceauth/token";
const OAUTH_TOKEN_URL: &str = "https://auth.openai.com/oauth/token";
const DEVICE_CALLBACK_URL: &str = "https://auth.openai.com/deviceauth/callback";

pub const MAX_USER_CODE_BYTES: usize = 256;
pub const MAX_TOKEN_BYTES: usize = 64 * 1024;
pub const MAX_RESPONSE_BYTES: usize = 256 * 1024;
pub const MAX_POLLS: usize = 240;
const MAX_DEVICE_FIELD_BYTES: usize = 4 * 1024;
const MAX_EXPIRES_IN_SECONDS: u64 = 30 * 24 * 60 * 60;
const DEFAULT_INTERVAL_SECONDS: u64 = 5;
const DEFAULT_EXPIRES_IN_SECONDS: u64 = 900;

/// Hard per-request timeout.
pub const REQUEST_TIMEOUT: Duration = Duration::from_secs(20);
/// Added to the server-provided polling interval.
pub const POLLING_SAFETY_MARGIN: Duration = Duration::from_millis(3_000);

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum DeviceAuthError {
    #[error("device authorization request failed")]
    Request,
    #[error("device authorization was denied or expired")]
    Denied,
    #[error("device authorization timed out")]
    Timeout,
    #[error("token response was malformed or too large")]
    Malformed,
    #[error("authentication is unavailable in this build")]
    Unavailable,
}

/// Redirect-disabled, timeout-bounded client for the device flow.
///
/// Callers must use this rather than an arbitrary `reqwest::Client`; the public
/// functions accept `&Client` so tests can inject one, but production callers
/// should always build it here.
pub fn device_auth_client() -> Result<Client, DeviceAuthError> {
    Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(REQUEST_TIMEOUT)
        .timeout(REQUEST_TIMEOUT)
        .build()
        .map_err(|_| DeviceAuthError::Request)
}

/// Internal endpoint set. Release builds can only construct the fixed official
/// endpoints; the loopback constructor exists exclusively in unit tests.
#[derive(Clone, Debug, Eq, PartialEq)]
struct EndpointSet {
    usercode: String,
    token: String,
    oauth_token: String,
    login_url: String,
    callback_url: String,
    #[cfg(test)]
    local_test: bool,
}

impl Default for EndpointSet {
    fn default() -> Self {
        Self {
            usercode: DEVICE_AUTH_URL.into(),
            token: DEVICE_TOKEN_URL.into(),
            oauth_token: OAUTH_TOKEN_URL.into(),
            login_url: DEVICE_LOGIN_URL.into(),
            callback_url: DEVICE_CALLBACK_URL.into(),
            #[cfg(test)]
            local_test: false,
        }
    }
}

impl EndpointSet {
    fn validate(&self) -> Result<(), DeviceAuthError> {
        #[cfg(test)]
        if !self.local_test {
            return if self == &Self::default() {
                Ok(())
            } else {
                Err(DeviceAuthError::Unavailable)
            };
        }
        #[cfg(test)]
        if self.local_test {
            let expected = [
                (&self.usercode, "/api/accounts/deviceauth/usercode"),
                (&self.token, "/api/accounts/deviceauth/token"),
                (&self.oauth_token, "/oauth/token"),
                (&self.login_url, "/codex/device"),
                (&self.callback_url, "/deviceauth/callback"),
            ];
            let bases: Vec<String> = expected
                .iter()
                .map(|(value, path)| parse_local_endpoint(value, path))
                .collect::<Result<_, _>>()?;
            if bases.iter().any(|base| base != &bases[0]) {
                return Err(DeviceAuthError::Unavailable);
            }
            return Ok(());
        }
        if self == &Self::default() {
            Ok(())
        } else {
            Err(DeviceAuthError::Unavailable)
        }
    }
}

/// Test endpoint wrapper, not present in release builds.
#[cfg(test)]
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct DeviceEndpoints(EndpointSet);

#[cfg(test)]
impl DeviceEndpoints {
    /// Only `http://127.0.0.1:<port>/` is accepted for mock servers.
    pub fn for_local_test(base: &str) -> Result<Self, DeviceAuthError> {
        let base = parse_local_base(base)?;
        Ok(Self(EndpointSet {
            usercode: format!("{base}/api/accounts/deviceauth/usercode"),
            token: format!("{base}/api/accounts/deviceauth/token"),
            oauth_token: format!("{base}/oauth/token"),
            login_url: format!("{base}/codex/device"),
            callback_url: format!("{base}/deviceauth/callback"),
            local_test: true,
        }))
    }
}

#[cfg(test)]
fn parse_local_base(value: &str) -> Result<String, DeviceAuthError> {
    let url = Url::parse(value).map_err(|_| DeviceAuthError::Unavailable)?;
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/"
    {
        return Err(DeviceAuthError::Unavailable);
    }
    Ok(format!("http://{}", url.authority()))
}

#[cfg(test)]
fn parse_local_endpoint(value: &str, expected_path: &str) -> Result<String, DeviceAuthError> {
    let url = Url::parse(value).map_err(|_| DeviceAuthError::Unavailable)?;
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != expected_path
    {
        return Err(DeviceAuthError::Unavailable);
    }
    Ok(format!("http://{}", url.authority()))
}

/// A pending device authorization. `Debug` redacts the user code and device code.
#[derive(Clone, Eq, PartialEq)]
pub struct DeviceAuthorization {
    pub user_code: String,
    pub verification_url: String,
    pub device_code: String,
    pub interval_seconds: u64,
    pub expires_in_seconds: u64,
}

impl fmt::Debug for DeviceAuthorization {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("DeviceAuthorization")
            .field("user_code", &"[REDACTED]")
            .field("verification_url", &self.verification_url)
            .field("device_code", &"[REDACTED]")
            .field("interval_seconds", &self.interval_seconds)
            .field("expires_in_seconds", &self.expires_in_seconds)
            .finish()
    }
}

/// A stored OpenAI credential. `Debug` and `Serialize` redact the tokens.
#[derive(Clone, Eq, PartialEq)]
pub struct OpenAiCredential {
    pub access_token: String,
    pub refresh_token: String,
    pub expires_at_ms: u64,
    pub account_id: Option<String>,
}

impl OpenAiCredential {
    /// True when the credential is at or past its expiry.
    pub fn is_expired(&self, now_ms: u64) -> bool {
        self.expires_at_ms <= now_ms
    }
}

impl fmt::Debug for OpenAiCredential {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("OpenAiCredential")
            .field("access_token", &"[REDACTED]")
            .field("refresh_token", &"[REDACTED]")
            .field("expires_at_ms", &self.expires_at_ms)
            .field(
                "account_id",
                &self.account_id.as_ref().map(|_| "[REDACTED]"),
            )
            .finish()
    }
}

impl Serialize for OpenAiCredential {
    /// Deliberately redacted: serializing a credential must never emit tokens.
    /// Persistence is handled by `crate::auth_store`, which owns its own
    /// representation.
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        #[derive(Serialize)]
        struct Redacted {
            expires_at_ms: u64,
        }
        Redacted {
            expires_at_ms: self.expires_at_ms,
        }
        .serialize(serializer)
    }
}

#[derive(Deserialize)]
struct UserCodeResponse {
    device_auth_id: Option<String>,
    user_code: Option<String>,
    interval: Option<serde_json::Value>,
    expires_in: Option<serde_json::Value>,
}

#[derive(Deserialize)]
struct DeviceTokenResponse {
    authorization_code: Option<String>,
    code_verifier: Option<String>,
}

#[derive(Deserialize)]
struct TokenResponse {
    access_token: Option<String>,
    refresh_token: Option<String>,
    id_token: Option<String>,
    expires_in: Option<serde_json::Value>,
}

#[derive(Serialize)]
struct UserCodeRequest<'a> {
    client_id: &'a str,
}

#[derive(Serialize)]
struct DeviceTokenRequest<'a> {
    device_auth_id: &'a str,
    user_code: &'a str,
}

/// Begin a device authorization against the production endpoints.
pub async fn begin_device_authorization(
    client: &Client,
) -> Result<DeviceAuthorization, DeviceAuthError> {
    begin_device_authorization_with_endpoints(client, &EndpointSet::default()).await
}

/// Begin a device authorization against an explicit endpoint set (tests).
#[cfg(test)]
pub async fn begin_device_authorization_at(
    client: &Client,
    endpoints: &DeviceEndpoints,
) -> Result<DeviceAuthorization, DeviceAuthError> {
    begin_device_authorization_with_endpoints(client, &endpoints.0).await
}

async fn begin_device_authorization_with_endpoints(
    client: &Client,
    endpoints: &EndpointSet,
) -> Result<DeviceAuthorization, DeviceAuthError> {
    endpoints.validate()?;
    let response = client
        .post(&endpoints.usercode)
        .header("content-type", "application/json")
        .header("user-agent", "socket_stream")
        .json(&UserCodeRequest {
            client_id: CLIENT_ID,
        })
        .timeout(REQUEST_TIMEOUT)
        .send()
        .await
        .map_err(|_| DeviceAuthError::Request)?;
    if !response.status().is_success() {
        return Err(DeviceAuthError::Request);
    }
    let data: UserCodeResponse = read_json(response).await?;
    let device_code = data
        .device_auth_id
        .filter(|value| !value.is_empty() && value.len() <= MAX_DEVICE_FIELD_BYTES)
        .ok_or(DeviceAuthError::Malformed)?;
    let user_code = data
        .user_code
        .filter(|value| {
            !value.is_empty()
                && value.len() <= MAX_USER_CODE_BYTES
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        })
        .ok_or(DeviceAuthError::Malformed)?;
    let interval_seconds = data
        .interval
        .as_ref()
        .and_then(flexible_u64)
        .filter(|value| *value > 0 && *value <= 3_600)
        .unwrap_or(DEFAULT_INTERVAL_SECONDS);
    let expires_in_seconds = data
        .expires_in
        .as_ref()
        .and_then(flexible_u64)
        .filter(|value| *value > 0 && *value <= MAX_EXPIRES_IN_SECONDS)
        .unwrap_or(DEFAULT_EXPIRES_IN_SECONDS);
    Ok(DeviceAuthorization {
        user_code,
        verification_url: endpoints.login_url.clone(),
        device_code,
        interval_seconds,
        expires_in_seconds,
    })
}

/// Poll until the user approves, then exchange the authorization code.
pub async fn poll_device_authorization(
    client: &Client,
    auth: &DeviceAuthorization,
) -> Result<OpenAiCredential, DeviceAuthError> {
    poll_device_authorization_with_endpoints(client, &EndpointSet::default(), auth, None).await
}

/// Poll with an explicit endpoint set and delay (tests).
#[cfg(test)]
pub async fn poll_device_authorization_at(
    client: &Client,
    endpoints: &DeviceEndpoints,
    auth: &DeviceAuthorization,
    delay: Duration,
) -> Result<OpenAiCredential, DeviceAuthError> {
    poll_device_authorization_with_endpoints(client, &endpoints.0, auth, Some(delay)).await
}

async fn poll_device_authorization_with_endpoints(
    client: &Client,
    endpoints: &EndpointSet,
    auth: &DeviceAuthorization,
    delay_override: Option<Duration>,
) -> Result<OpenAiCredential, DeviceAuthError> {
    endpoints.validate()?;
    if auth.device_code.is_empty()
        || auth.device_code.len() > MAX_DEVICE_FIELD_BYTES
        || auth.user_code.is_empty()
        || auth.user_code.len() > MAX_USER_CODE_BYTES
        || !auth
            .user_code
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        || auth.interval_seconds == 0
        || auth.interval_seconds > 3_600
        || auth.expires_in_seconds == 0
        || auth.expires_in_seconds > MAX_EXPIRES_IN_SECONDS
    {
        return Err(DeviceAuthError::Malformed);
    }
    let delay = match delay_override {
        Some(delay) => delay,
        None => Duration::from_secs(auth.interval_seconds)
            .checked_add(POLLING_SAFETY_MARGIN)
            .ok_or(DeviceAuthError::Malformed)?,
    };
    let deadline = tokio::time::Instant::now() + Duration::from_secs(auth.expires_in_seconds);
    for _ in 0..MAX_POLLS {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err(DeviceAuthError::Timeout);
        }
        if delay >= deadline - now {
            tokio::time::sleep_until(deadline).await;
            return Err(DeviceAuthError::Timeout);
        }
        tokio::time::sleep(delay).await;

        let attempt = tokio::time::timeout_at(deadline, async {
            let response = client
                .post(&endpoints.token)
                .header("content-type", "application/json")
                .header("user-agent", "socket_stream")
                .json(&DeviceTokenRequest {
                    device_auth_id: &auth.device_code,
                    user_code: &auth.user_code,
                })
                .timeout(REQUEST_TIMEOUT)
                .send()
                .await
                .map_err(|_| DeviceAuthError::Request)?;
            if response.status().is_success() {
                let token: DeviceTokenResponse = read_json(response).await?;
                let code = token
                    .authorization_code
                    .filter(|value| !value.is_empty() && value.len() <= MAX_DEVICE_FIELD_BYTES)
                    .ok_or(DeviceAuthError::Malformed)?;
                let verifier = token
                    .code_verifier
                    .filter(|value| !value.is_empty() && value.len() <= MAX_DEVICE_FIELD_BYTES)
                    .ok_or(DeviceAuthError::Malformed)?;
                return exchange_authorization_code_at(client, endpoints, &code, &verifier)
                    .await
                    .map(Some);
            }
            if response.status() == StatusCode::FORBIDDEN
                || response.status() == StatusCode::NOT_FOUND
            {
                Ok(None)
            } else {
                Err(DeviceAuthError::Denied)
            }
        })
        .await
        .map_err(|_| DeviceAuthError::Timeout)??;
        if let Some(credential) = attempt {
            return Ok(credential);
        }
    }
    Err(DeviceAuthError::Timeout)
}

/// Exchange an approved authorization code for a credential.
async fn exchange_authorization_code_at(
    client: &Client,
    endpoints: &EndpointSet,
    code: &str,
    verifier: &str,
) -> Result<OpenAiCredential, DeviceAuthError> {
    endpoints.validate()?;
    validate_nonempty_bounded(code, MAX_DEVICE_FIELD_BYTES)?;
    validate_nonempty_bounded(verifier, MAX_DEVICE_FIELD_BYTES)?;
    let response = client
        .post(&endpoints.oauth_token)
        .header("content-type", "application/x-www-form-urlencoded")
        .header("user-agent", "socket_stream")
        .form(&[
            ("grant_type", "authorization_code"),
            ("code", code),
            ("redirect_uri", endpoints.callback_url.as_str()),
            ("client_id", CLIENT_ID),
            ("code_verifier", verifier),
        ])
        .timeout(REQUEST_TIMEOUT)
        .send()
        .await
        .map_err(|_| DeviceAuthError::Request)?;
    parse_token_response(response).await
}

/// Refresh an existing credential.
pub async fn refresh_credential(
    client: &Client,
    refresh_token: &str,
) -> Result<OpenAiCredential, DeviceAuthError> {
    refresh_credential_with_endpoints(client, &EndpointSet::default(), refresh_token).await
}

/// Refresh against an explicit endpoint set (tests).
#[cfg(test)]
pub async fn refresh_credential_at(
    client: &Client,
    endpoints: &DeviceEndpoints,
    refresh_token: &str,
) -> Result<OpenAiCredential, DeviceAuthError> {
    refresh_credential_with_endpoints(client, &endpoints.0, refresh_token).await
}

async fn refresh_credential_with_endpoints(
    client: &Client,
    endpoints: &EndpointSet,
    refresh_token: &str,
) -> Result<OpenAiCredential, DeviceAuthError> {
    endpoints.validate()?;
    validate_nonempty_bounded(refresh_token, MAX_TOKEN_BYTES)?;
    let response = client
        .post(&endpoints.oauth_token)
        .header("content-type", "application/x-www-form-urlencoded")
        .header("user-agent", "socket_stream")
        .form(&[
            ("grant_type", "refresh_token"),
            ("refresh_token", refresh_token),
            ("client_id", CLIENT_ID),
        ])
        .timeout(REQUEST_TIMEOUT)
        .send()
        .await
        .map_err(|_| DeviceAuthError::Request)?;
    parse_token_response(response).await
}

async fn read_json<T: for<'de> Deserialize<'de>>(response: Response) -> Result<T, DeviceAuthError> {
    let body = read_body_bounded(response).await?;
    serde_json::from_slice(&body).map_err(|_| DeviceAuthError::Malformed)
}

async fn read_body_bounded(mut response: Response) -> Result<Vec<u8>, DeviceAuthError> {
    if response
        .content_length()
        .is_some_and(|length| length > MAX_RESPONSE_BYTES as u64)
    {
        return Err(DeviceAuthError::Malformed);
    }
    let mut body = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| DeviceAuthError::Request)?
    {
        if body.len().saturating_add(chunk.len()) > MAX_RESPONSE_BYTES {
            return Err(DeviceAuthError::Malformed);
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

async fn parse_token_response(response: Response) -> Result<OpenAiCredential, DeviceAuthError> {
    if !response.status().is_success() {
        return Err(DeviceAuthError::Request);
    }
    let token: TokenResponse = read_json(response).await?;
    let access_token = token
        .access_token
        .filter(|value| !value.is_empty() && value.len() <= MAX_TOKEN_BYTES)
        .ok_or(DeviceAuthError::Malformed)?;
    let refresh_token = token
        .refresh_token
        .filter(|value| !value.is_empty() && value.len() <= MAX_TOKEN_BYTES)
        .ok_or(DeviceAuthError::Malformed)?;
    let id_token = token.id_token.unwrap_or_default();
    if id_token.len() > MAX_TOKEN_BYTES {
        return Err(DeviceAuthError::Malformed);
    }
    let expires_in = token
        .expires_in
        .as_ref()
        .and_then(flexible_u64)
        .unwrap_or(3_600);
    if expires_in == 0 || expires_in > MAX_EXPIRES_IN_SECONDS {
        return Err(DeviceAuthError::Malformed);
    }
    let expires_at_ms = now_ms()
        .checked_add(
            expires_in
                .checked_mul(1_000)
                .ok_or(DeviceAuthError::Malformed)?,
        )
        .ok_or(DeviceAuthError::Malformed)?;
    let account_id = extract_account_id(&id_token).or_else(|| extract_account_id(&access_token));
    Ok(OpenAiCredential {
        access_token,
        refresh_token,
        expires_at_ms,
        account_id,
    })
}

fn flexible_u64(value: &serde_json::Value) -> Option<u64> {
    value
        .as_u64()
        .or_else(|| value.as_str().and_then(|text| text.parse().ok()))
}

fn now_ms() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}

/// Unverified display/routing hint only. Never used to grant authorization.
fn extract_account_id(token: &str) -> Option<String> {
    let mut parts = token.split('.');
    let _header = parts.next()?;
    let payload = parts.next()?;
    let _signature = parts.next()?;
    if parts.next().is_some() || payload.len() > MAX_TOKEN_BYTES {
        return None;
    }
    let bytes = decode_base64url(payload)?;
    let claims: serde_json::Value = serde_json::from_slice(&bytes).ok()?;
    let id = claims
        .get("chatgpt_account_id")
        .and_then(|value| value.as_str())
        .or_else(|| {
            claims
                .get("https://api.openai.com/auth")?
                .get("chatgpt_account_id")?
                .as_str()
        })
        .or_else(|| {
            claims
                .get("organizations")?
                .as_array()?
                .first()?
                .get("id")?
                .as_str()
        })?;
    valid_account_id(id)
}

fn valid_account_id(id: &str) -> Option<String> {
    ((1..=256).contains(&id.len())
        && id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(&byte)))
    .then(|| id.to_owned())
}

fn validate_nonempty_bounded(value: &str, limit: usize) -> Result<(), DeviceAuthError> {
    if value.is_empty() || value.len() > limit {
        Err(DeviceAuthError::Malformed)
    } else {
        Ok(())
    }
}

fn decode_base64url(input: &str) -> Option<Vec<u8>> {
    let mut bits = 0u32;
    let mut count = 0u8;
    let mut out = Vec::new();
    for byte in input.bytes() {
        let value = match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'-' => 62,
            b'_' => 63,
            _ => return None,
        } as u32;
        bits = (bits << 6) | value;
        count += 6;
        if count >= 8 {
            count -= 8;
            out.push((bits >> count) as u8);
            bits &= (1 << count) - 1;
        }
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    /// A scripted HTTP/1.1 mock server. Each element is (status, body).
    struct MockServer {
        base: String,
        handle: tokio::task::JoinHandle<()>,
    }

    impl MockServer {
        async fn start(script: Vec<(u16, String)>) -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
            let addr = listener.local_addr().expect("addr");
            let handle = tokio::spawn(async move {
                let mut script = script.into_iter();
                loop {
                    let Ok((mut socket, _)) = listener.accept().await else {
                        break;
                    };
                    let Some((status, body)) = script.next() else {
                        break;
                    };
                    let mut buffer = Vec::new();
                    let mut chunk = [0u8; 1024];
                    let mut header_end = None;
                    while header_end.is_none() {
                        let read = match socket.read(&mut chunk).await {
                            Ok(0) | Err(_) => break,
                            Ok(read) => read,
                        };
                        buffer.extend_from_slice(&chunk[..read]);
                        header_end = find_header_end(&buffer);
                    }
                    if let Some(end) = header_end {
                        let length = content_length(&buffer[..end]);
                        while buffer.len() < end + length {
                            let read = match socket.read(&mut chunk).await {
                                Ok(0) | Err(_) => break,
                                Ok(read) => read,
                            };
                            buffer.extend_from_slice(&chunk[..read]);
                        }
                    }
                    let response = format!(
                        "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    let _ = socket.write_all(response.as_bytes()).await;
                    let _ = socket.flush().await;
                    let _ = socket.shutdown().await;
                }
            });
            Self {
                base: format!("http://{addr}"),
                handle,
            }
        }

        fn endpoints(&self) -> DeviceEndpoints {
            DeviceEndpoints::for_local_test(&self.base).expect("local endpoints")
        }
    }

    impl Drop for MockServer {
        fn drop(&mut self) {
            self.handle.abort();
        }
    }

    fn find_header_end(buffer: &[u8]) -> Option<usize> {
        buffer
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .map(|i| i + 4)
    }

    fn content_length(headers: &[u8]) -> usize {
        let text = String::from_utf8_lossy(headers).to_ascii_lowercase();
        text.lines()
            .find_map(|line| line.strip_prefix("content-length:"))
            .and_then(|value| value.trim().parse().ok())
            .unwrap_or(0)
    }

    fn user_code_body() -> String {
        r#"{"device_auth_id":"dev-123","user_code":"ABCD-EFGH","interval":"1","expires_in":900}"#
            .to_string()
    }

    fn token_body() -> String {
        r#"{"authorization_code":"auth-code","code_verifier":"verifier"}"#.to_string()
    }

    fn credential_body() -> String {
        r#"{"access_token":"access-token","refresh_token":"refresh-token","id_token":"","expires_in":3600}"#
            .to_string()
    }

    fn test_client() -> Client {
        device_auth_client().expect("client")
    }

    #[tokio::test]
    async fn happy_path_authorizes_and_exchanges() {
        let server = MockServer::start(vec![
            (200, user_code_body()),
            (403, "{}".into()),
            (200, token_body()),
            (200, credential_body()),
        ])
        .await;
        let endpoints = server.endpoints();
        let client = test_client();

        let auth = begin_device_authorization_at(&client, &endpoints)
            .await
            .expect("begin");
        assert_eq!(auth.user_code, "ABCD-EFGH");
        assert_eq!(auth.device_code, "dev-123");
        assert_eq!(auth.interval_seconds, 1);
        assert_eq!(auth.expires_in_seconds, 900);

        let credential =
            poll_device_authorization_at(&client, &endpoints, &auth, Duration::from_millis(1))
                .await
                .expect("poll");
        assert_eq!(credential.access_token, "access-token");
        assert_eq!(credential.refresh_token, "refresh-token");
        assert!(!credential.is_expired(0));
        assert!(credential.is_expired(u64::MAX));
    }

    #[tokio::test]
    async fn denial_is_reported() {
        let server = MockServer::start(vec![(200, user_code_body()), (400, "{}".into())]).await;
        let endpoints = server.endpoints();
        let client = test_client();
        let auth = begin_device_authorization_at(&client, &endpoints)
            .await
            .expect("begin");
        let error =
            poll_device_authorization_at(&client, &endpoints, &auth, Duration::from_millis(1))
                .await
                .expect_err("denied");
        assert_eq!(error, DeviceAuthError::Denied);
    }

    #[tokio::test]
    async fn malformed_body_is_rejected() {
        let server = MockServer::start(vec![(200, "not json".into())]).await;
        let endpoints = server.endpoints();
        let error = begin_device_authorization_at(&test_client(), &endpoints)
            .await
            .expect_err("malformed");
        assert_eq!(error, DeviceAuthError::Malformed);
    }

    #[tokio::test]
    async fn oversized_body_is_rejected() {
        let huge = "x".repeat(MAX_RESPONSE_BYTES + 1);
        let server = MockServer::start(vec![(200, huge)]).await;
        let endpoints = server.endpoints();
        let error = begin_device_authorization_at(&test_client(), &endpoints)
            .await
            .expect_err("oversized");
        assert_eq!(error, DeviceAuthError::Malformed);
    }

    #[tokio::test]
    async fn refresh_uses_refresh_grant() {
        let server = MockServer::start(vec![(200, credential_body())]).await;
        let endpoints = server.endpoints();
        let credential = refresh_credential_at(&test_client(), &endpoints, "refresh-token")
            .await
            .expect("refresh");
        assert_eq!(credential.access_token, "access-token");
    }

    #[test]
    fn debug_redacts_codes_and_tokens() {
        let auth = DeviceAuthorization {
            user_code: "SECRET-CODE".into(),
            verification_url: DEVICE_LOGIN_URL.into(),
            device_code: "SECRET-DEVICE".into(),
            interval_seconds: 5,
            expires_in_seconds: 900,
        };
        let rendered = format!("{auth:?}");
        assert!(!rendered.contains("SECRET-CODE"));
        assert!(!rendered.contains("SECRET-DEVICE"));
        assert!(rendered.contains("REDACTED"));

        let credential = OpenAiCredential {
            access_token: "SECRET-ACCESS".into(),
            refresh_token: "SECRET-REFRESH".into(),
            expires_at_ms: 42,
            account_id: Some("SECRET-ACCOUNT".into()),
        };
        let rendered = format!("{credential:?}");
        assert!(!rendered.contains("SECRET-ACCESS"));
        assert!(!rendered.contains("SECRET-REFRESH"));
        assert!(!rendered.contains("SECRET-ACCOUNT"));

        let json = serde_json::to_string(&credential).expect("serialize");
        assert!(!json.contains("SECRET-ACCESS"));
        assert!(!json.contains("SECRET-REFRESH"));
        assert!(json.contains("\"expires_at_ms\":42"));
    }

    #[test]
    fn local_endpoint_policy_rejects_non_loopback() {
        assert!(DeviceEndpoints::for_local_test("http://127.0.0.1:8080/").is_ok());
        assert!(DeviceEndpoints::for_local_test("http://example.com/").is_err());
        assert!(DeviceEndpoints::for_local_test("https://127.0.0.1:8080/").is_err());
        assert!(DeviceEndpoints::for_local_test("http://127.0.0.1:8080/path").is_err());
    }

    #[test]
    fn account_id_is_extracted_from_unsigned_claims() {
        // {"chatgpt_account_id":"acct_123"} base64url-encoded.
        let payload = "eyJjaGF0Z3B0X2FjY291bnRfaWQiOiJhY2N0XzEyMyJ9";
        let token = format!("header.{payload}.signature");
        assert_eq!(extract_account_id(&token).as_deref(), Some("acct_123"));
        assert_eq!(extract_account_id("not-a-jwt"), None);
    }
}
