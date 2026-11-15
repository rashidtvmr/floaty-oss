//! Authentication-only Codex app-server client. Credentials stay in Codex.
use crate::auth_store::CredentialStore;
use crate::openai_device_auth::OpenAiCredential;
use serde::Serialize;
use serde_json::{Value, json};
use std::collections::VecDeque;
use std::process::Stdio;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};

const MAX_MESSAGE: usize = 256 * 1024;
const RPC_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, thiserror::Error)]
pub enum AuthError {
    #[error("Codex CLI unavailable; install the official CLI and ensure it is in PATH")]
    Unavailable,
    #[error("Codex authentication transport closed or returned invalid data")]
    Transport,
    #[error("Codex authentication request timed out")]
    Timeout,
    #[error("Codex rejected this authentication operation; check account policy and CLI version")]
    Rejected,
    #[error("Codex sign-in was declined, expired or unsuccessful")]
    LoginFailed,
    #[error("Codex returned an untrusted sign-in URL; no browser was opened")]
    UntrustedUrl,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LoginMethod {
    Browser,
    Device,
}
impl LoginMethod {
    fn wire_name(self) -> &'static str {
        match self {
            Self::Browser => "chatgpt",
            Self::Device => "chatgptDeviceCode",
        }
    }
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct AccountSummary {
    pub signed_in: bool,
    pub auth_type: String,
    pub plan: Option<String>,
}
impl AccountSummary {
    fn from_result(value: &Value) -> Result<Self, AuthError> {
        if !value.is_object() {
            return Err(AuthError::Transport);
        }
        let account = &value["account"];
        if account.is_null() {
            return Ok(Self {
                signed_in: false,
                auth_type: "none".into(),
                plan: None,
            });
        }
        let kind = account
            .get("type")
            .and_then(Value::as_str)
            .ok_or(AuthError::Transport)?;
        let auth_type = match kind {
            "chatgpt" => "chatgpt",
            "apiKey" => "apiKey",
            "amazonBedrock" => "amazonBedrock",
            _ => "other",
        }
        .to_owned();
        let plan = account
            .get("planType")
            .and_then(Value::as_str)
            .filter(|p| p.len() <= 80 && p.chars().all(|c| c.is_ascii_alphanumeric() || c == '_'))
            .map(str::to_owned);
        Ok(Self {
            signed_in: true,
            auth_type,
            plan,
        })
    }
}

/// Summarise a native device-flow credential without exposing tokens.
pub fn credential_to_summary(credential: &OpenAiCredential) -> AccountSummary {
    let expired = credential.is_expired(now_ms());
    AccountSummary {
        signed_in: !expired,
        auth_type: if expired {
            "chatgpt (expired)".into()
        } else {
            "chatgpt".into()
        },
        plan: None,
    }
}

/// Report sign-in state from the local native credential store, without the
/// Codex CLI. Never reads the official CLI's credential files.
pub fn load_native_status() -> Result<AccountSummary, AuthError> {
    let credential = CredentialStore::new()
        .load()
        .map_err(|_| AuthError::Transport)?;
    Ok(match credential {
        Some(credential) => credential_to_summary(&credential),
        None => AccountSummary {
            signed_in: false,
            auth_type: "none".into(),
            plan: None,
        },
    })
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

/// No Debug: authorization URLs and device codes must not enter diagnostics.
pub struct LoginChallenge {
    pub login_id: String,
    pub method: LoginMethod,
    pub url: String,
    pub user_code: Option<String>,
}
impl LoginChallenge {
    fn from_result(result: &Value, method: LoginMethod) -> Result<Self, AuthError> {
        if result.get("type").and_then(Value::as_str) != Some(method.wire_name()) {
            return Err(AuthError::Transport);
        }
        let id = result
            .get("loginId")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty() && s.len() <= 128 && !s.chars().any(char::is_control))
            .ok_or(AuthError::Transport)?;
        let field = if method == LoginMethod::Browser {
            "authUrl"
        } else {
            "verificationUrl"
        };
        let url = result
            .get(field)
            .and_then(Value::as_str)
            .ok_or(AuthError::Transport)?;
        if !trusted_sign_in_url(url) {
            return Err(AuthError::UntrustedUrl);
        }
        let user_code = if method == LoginMethod::Device {
            Some(
                result
                    .get("userCode")
                    .and_then(Value::as_str)
                    .filter(|s| {
                        !s.is_empty()
                            && s.len() <= 64
                            && s.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
                    })
                    .ok_or(AuthError::Transport)?
                    .to_owned(),
            )
        } else {
            None
        };
        Ok(Self {
            login_id: id.into(),
            method,
            url: url.into(),
            user_code,
        })
    }
}

pub fn trusted_sign_in_url(value: &str) -> bool {
    if value.len() > 16384 || value.chars().any(char::is_control) {
        return false;
    }
    let Ok(url) = reqwest::Url::parse(value) else {
        return false;
    };
    url.scheme() == "https"
        && url.username().is_empty()
        && url.password().is_none()
        && url.fragment().is_none()
        && url.port_or_known_default() == Some(443)
        && matches!(url.host_str(), Some("auth.openai.com" | "chatgpt.com"))
}

/// Only following an explicit user Sign in action. Not a generic URL launcher.
pub async fn open_login_page(url: &str) -> Result<(), AuthError> {
    if !trusted_sign_in_url(url) {
        return Err(AuthError::UntrustedUrl);
    }
    let executable = if cfg!(target_os = "macos") {
        "open"
    } else {
        "xdg-open"
    };
    let status = tokio::time::timeout(
        Duration::from_secs(10),
        Command::new(executable)
            .arg(url)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .status(),
    )
    .await
    .map_err(|_| AuthError::Timeout)?
    .map_err(|_| AuthError::Unavailable)?;
    if status.success() {
        Ok(())
    } else {
        Err(AuthError::Unavailable)
    }
}

pub struct CodexAuthSession {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    partial: Vec<u8>,
    next_id: u64,
    completions: VecDeque<Value>,
}
impl CodexAuthSession {
    pub async fn connect() -> Result<Self, AuthError> {
        let mut command = Command::new("codex");
        command.args(["app-server", "--listen", "stdio://"]);
        command.env_remove("SOCKET_STREAM_API_KEY");
        Self::from_command(command).await
    }

    async fn from_command(mut command: Command) -> Result<Self, AuthError> {
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .map_err(|_| AuthError::Unavailable)?;
        let stdin = child.stdin.take().ok_or(AuthError::Transport)?;
        let stdout = BufReader::new(child.stdout.take().ok_or(AuthError::Transport)?);
        let mut client = Self {
            child,
            stdin,
            stdout,
            partial: Vec::new(),
            next_id: 1,
            completions: VecDeque::new(),
        };
        client.request("initialize", json!({"clientInfo":{
            "name":"socket_stream_local", "title":"SocketStream", "version":env!("CARGO_PKG_VERSION")
        }})).await?;
        client.send(&json!({"method":"initialized"})).await?;
        Ok(client)
    }

    async fn send(&mut self, message: &Value) -> Result<(), AuthError> {
        let mut bytes = serde_json::to_vec(message).map_err(|_| AuthError::Transport)?;
        bytes.push(b'\n');
        self.stdin
            .write_all(&bytes)
            .await
            .map_err(|_| AuthError::Transport)?;
        self.stdin.flush().await.map_err(|_| AuthError::Transport)
    }

    async fn read_message(&mut self) -> Result<Value, AuthError> {
        loop {
            let available = self
                .stdout
                .fill_buf()
                .await
                .map_err(|_| AuthError::Transport)?;
            if available.is_empty() {
                return Err(AuthError::Transport);
            }
            let newline = available.iter().position(|b| *b == b'\n');
            let count = newline.map_or(available.len(), |i| i + 1);
            if self.partial.len() + count > MAX_MESSAGE {
                return Err(AuthError::Transport);
            }
            self.partial.extend_from_slice(&available[..count]);
            self.stdout.consume(count);
            if newline.is_some() {
                let bytes = std::mem::take(&mut self.partial);
                if bytes.iter().all(u8::is_ascii_whitespace) {
                    continue;
                }
                return serde_json::from_slice(&bytes).map_err(|_| AuthError::Transport);
            }
        }
    }

    async fn handle_unsolicited(&mut self, message: Value) -> Result<(), AuthError> {
        if message.get("id").is_some() && message.get("method").is_some() {
            // An authentication client must not approve shell/MCP/tool operations.
            self.send(&json!({"id":message["id"],"error":{"code":-32601,
                "message":"Unsupported by SocketStream authentication client"}}))
                .await?;
        } else if message.get("method").and_then(Value::as_str) == Some("account/login/completed") {
            if self.completions.len() == 16 {
                self.completions.pop_front();
            }
            self.completions.push_back(message);
        }
        Ok(())
    }

    async fn request(&mut self, method: &str, params: Value) -> Result<Value, AuthError> {
        let id = self.next_id;
        self.next_id += 1;
        tokio::time::timeout(RPC_TIMEOUT, async {
            self.send(&json!({"id":id,"method":method,"params":params}))
                .await?;
            loop {
                let message = self.read_message().await?;
                if message.get("id").and_then(Value::as_u64) == Some(id)
                    && message.get("method").is_none()
                {
                    if message.get("error").is_some() {
                        return Err(AuthError::Rejected);
                    }
                    return message.get("result").cloned().ok_or(AuthError::Transport);
                }
                self.handle_unsolicited(message).await?;
            }
        })
        .await
        .map_err(|_| AuthError::Timeout)?
    }

    pub async fn status(&mut self) -> Result<AccountSummary, AuthError> {
        let value = self
            .request("account/read", json!({"refreshToken":false}))
            .await?;
        AccountSummary::from_result(&value)
    }

    pub async fn start_login(&mut self, method: LoginMethod) -> Result<LoginChallenge, AuthError> {
        let value = self
            .request("account/login/start", json!({"type":method.wire_name()}))
            .await?;
        LoginChallenge::from_result(&value, method)
    }

    pub async fn wait_for_login(&mut self, login_id: &str) -> Result<(), AuthError> {
        loop {
            let message = match self.completions.pop_front() {
                Some(message) => message,
                None => self.read_message().await?,
            };
            if message.get("method").and_then(Value::as_str) == Some("account/login/completed") {
                let params = &message["params"];
                if params.get("loginId").and_then(Value::as_str) != Some(login_id) {
                    continue;
                }
                return match params.get("success").and_then(Value::as_bool) {
                    Some(true) => Ok(()),
                    _ => Err(AuthError::LoginFailed),
                };
            }
            self.handle_unsolicited(message).await?;
        }
    }

    pub async fn cancel_login(&mut self, login_id: &str) -> Result<(), AuthError> {
        self.request("account/login/cancel", json!({"loginId":login_id}))
            .await?;
        Ok(())
    }

    /// Call only following explicit confirmation; it affects the shared CLI login.
    pub async fn logout(&mut self) -> Result<(), AuthError> {
        self.request("account/logout", json!({})).await?;
        Ok(())
    }

    pub async fn close(&mut self) {
        let _ = self.child.kill().await;
        let _ = self.child.wait().await;
    }
}
