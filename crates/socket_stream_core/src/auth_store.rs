//! Local credential store for native ChatGPT/Codex sign-in.
//!
//! This store is owned entirely by SocketStream. It never reads, writes or
//! inspects the official Codex CLI's credential files, and it never logs a
//! token. The file is created with mode `0600` on Unix.
//!
//! See `docs/A11Y-TREE-SHORTCUTS.md` section 4.2 for the frozen interface.

use crate::openai_device_auth::{MAX_TOKEN_BYTES, OpenAiCredential};
use serde::{Deserialize, Serialize};
use std::fmt;
use std::path::{Path, PathBuf};

const STORE_VERSION: u32 = 1;
const MAX_STORE_BYTES: u64 = 256 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum AuthStoreError {
    #[error("credential file could not be read")]
    Read,
    #[error("credential file is malformed")]
    Malformed,
    #[error("credential file could not be written")]
    Write,
    #[error("credential file could not be removed")]
    Remove,
}

/// On-disk representation. Kept private so raw tokens never leak through a
/// public `Serialize` implementation.
#[derive(Serialize, Deserialize)]
struct StoredCredential {
    version: u32,
    access_token: String,
    refresh_token: String,
    expires_at_ms: u64,
    #[serde(default)]
    account_id: Option<String>,
}

impl StoredCredential {
    fn from_credential(credential: &OpenAiCredential) -> Self {
        Self {
            version: STORE_VERSION,
            access_token: credential.access_token.clone(),
            refresh_token: credential.refresh_token.clone(),
            expires_at_ms: credential.expires_at_ms,
            account_id: credential.account_id.clone(),
        }
    }

    fn into_credential(self) -> Result<OpenAiCredential, AuthStoreError> {
        if self.version != STORE_VERSION
            || self.access_token.is_empty()
            || self.refresh_token.is_empty()
            || self.access_token.len() > MAX_TOKEN_BYTES
            || self.refresh_token.len() > MAX_TOKEN_BYTES
            || self.account_id.as_ref().is_some_and(|id| id.len() > 256)
        {
            return Err(AuthStoreError::Malformed);
        }
        Ok(OpenAiCredential {
            access_token: self.access_token,
            refresh_token: self.refresh_token,
            expires_at_ms: self.expires_at_ms,
            account_id: self.account_id,
        })
    }
}

/// A credential store rooted at a single JSON file.
pub struct CredentialStore {
    path: PathBuf,
}

impl fmt::Debug for CredentialStore {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("CredentialStore")
            .field("path", &self.path)
            .finish_non_exhaustive()
    }
}

impl Default for CredentialStore {
    fn default() -> Self {
        Self::new()
    }
}

impl CredentialStore {
    /// Store at the platform default location.
    pub fn new() -> Self {
        Self {
            path: default_path(),
        }
    }

    /// Store at an explicit location (tests, or a user-selected profile).
    pub fn with_path(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Load the stored credential. A missing file is `Ok(None)`; a malformed
    /// file is an error and is never silently discarded.
    pub fn load(&self) -> Result<Option<OpenAiCredential>, AuthStoreError> {
        use std::io::Read;

        let file = match std::fs::File::open(&self.path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(_) => return Err(AuthStoreError::Read),
        };
        let metadata = file.metadata().map_err(|_| AuthStoreError::Read)?;
        if metadata.len() > MAX_STORE_BYTES {
            return Err(AuthStoreError::Malformed);
        }
        let mut bytes = Vec::with_capacity(metadata.len() as usize);
        file.take(MAX_STORE_BYTES + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| AuthStoreError::Read)?;
        if bytes.len() as u64 > MAX_STORE_BYTES {
            return Err(AuthStoreError::Malformed);
        }
        let stored: StoredCredential =
            serde_json::from_slice(&bytes).map_err(|_| AuthStoreError::Malformed)?;
        stored.into_credential().map(Some)
    }

    /// Persist a credential, creating the parent directory and restricting the
    /// file to the owner on Unix.
    pub fn save(&self, credential: &OpenAiCredential) -> Result<(), AuthStoreError> {
        if credential.access_token.is_empty()
            || credential.refresh_token.is_empty()
            || credential.access_token.len() > MAX_TOKEN_BYTES
            || credential.refresh_token.len() > MAX_TOKEN_BYTES
            || credential
                .account_id
                .as_ref()
                .is_some_and(|id| id.len() > 256)
        {
            return Err(AuthStoreError::Malformed);
        }
        if let Some(parent) = self.path.parent()
            && !parent.as_os_str().is_empty()
        {
            std::fs::create_dir_all(parent).map_err(|_| AuthStoreError::Write)?;
        }
        let stored = StoredCredential::from_credential(credential);
        let bytes = serde_json::to_vec_pretty(&stored).map_err(|_| AuthStoreError::Write)?;
        if bytes.len() as u64 > MAX_STORE_BYTES {
            return Err(AuthStoreError::Malformed);
        }
        write_private(&self.path, &bytes)
    }

    /// Remove the stored credential. A missing file is success.
    pub fn clear(&self) -> Result<(), AuthStoreError> {
        match std::fs::remove_file(&self.path) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err(AuthStoreError::Remove),
        }
    }
}

#[cfg(unix)]
fn write_private(path: &Path, bytes: &[u8]) -> Result<(), AuthStoreError> {
    use std::io::Write;
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(path)
        .map_err(|_| AuthStoreError::Write)?;
    file.set_permissions(std::fs::Permissions::from_mode(0o600))
        .map_err(|_| AuthStoreError::Write)?;
    file.set_len(0).map_err(|_| AuthStoreError::Write)?;
    file.write_all(bytes).map_err(|_| AuthStoreError::Write)?;
    file.sync_all().map_err(|_| AuthStoreError::Write)
}

#[cfg(not(unix))]
fn write_private(path: &Path, bytes: &[u8]) -> Result<(), AuthStoreError> {
    std::fs::write(path, bytes).map_err(|_| AuthStoreError::Write)
}

/// Resolve the default credential path.
pub fn default_path() -> PathBuf {
    if let Some(explicit) = std::env::var_os("SOCKET_STREAM_CREDENTIALS") {
        return PathBuf::from(explicit);
    }
    if cfg!(target_os = "macos")
        && let Some(home) = std::env::var_os("HOME")
    {
        return PathBuf::from(home)
            .join("Library/Application Support/socket_stream/credentials.json");
    }
    if let Some(config) = std::env::var_os("XDG_CONFIG_HOME") {
        return PathBuf::from(config).join("socket_stream/credentials.json");
    }
    if let Some(home) = std::env::var_os("HOME") {
        return PathBuf::from(home).join(".config/socket_stream/credentials.json");
    }
    PathBuf::from(".socket_stream_credentials.json")
}

#[cfg(test)]
mod tests {
    use super::*;

    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    fn credential() -> OpenAiCredential {
        OpenAiCredential {
            access_token: "access-token".into(),
            refresh_token: "refresh-token".into(),
            expires_at_ms: 1_700_000_000_000,
            account_id: Some("acct_123".into()),
        }
    }

    #[test]
    fn round_trips_through_a_tempdir() {
        let directory = tempfile::tempdir().expect("tempdir");
        let store = CredentialStore::with_path(directory.path().join("credentials.json"));
        assert!(store.load().expect("load empty").is_none());
        store.save(&credential()).expect("save");
        let loaded = store.load().expect("load").expect("present");
        assert_eq!(loaded, credential());
    }

    #[test]
    fn default_store_honours_credentials_path_override() {
        let _guard = ENV_LOCK.lock().expect("env lock");
        let directory = tempfile::tempdir().expect("tempdir");
        let expected = directory.path().join("private/credentials.json");
        let previous = std::env::var_os("SOCKET_STREAM_CREDENTIALS");
        // SAFETY: this test serializes access to the one environment variable it mutates.
        unsafe { std::env::set_var("SOCKET_STREAM_CREDENTIALS", &expected) };
        let actual = CredentialStore::new().path().to_path_buf();
        match previous {
            Some(value) => unsafe { std::env::set_var("SOCKET_STREAM_CREDENTIALS", value) },
            None => unsafe { std::env::remove_var("SOCKET_STREAM_CREDENTIALS") },
        }
        assert_eq!(actual, expected);
    }

    #[cfg(unix)]
    #[test]
    fn file_is_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let directory = tempfile::tempdir().expect("tempdir");
        let store = CredentialStore::with_path(directory.path().join("credentials.json"));
        store.save(&credential()).expect("save");
        let mode = std::fs::metadata(store.path())
            .expect("metadata")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600);
    }

    #[cfg(unix)]
    #[test]
    fn overwriting_existing_permissive_file_tightens_permissions() {
        use std::os::unix::fs::PermissionsExt;

        let directory = tempfile::tempdir().expect("tempdir");
        let store = CredentialStore::with_path(directory.path().join("credentials.json"));
        store.save(&credential()).expect("initial save");
        std::fs::set_permissions(store.path(), std::fs::Permissions::from_mode(0o644))
            .expect("make permissive");

        store.save(&credential()).expect("overwrite");
        let mode = std::fs::metadata(store.path())
            .expect("metadata")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600);
    }

    #[test]
    fn clear_removes_the_file_and_is_idempotent() {
        let directory = tempfile::tempdir().expect("tempdir");
        let store = CredentialStore::with_path(directory.path().join("credentials.json"));
        store.save(&credential()).expect("save");
        store.clear().expect("clear");
        assert!(!store.path().exists());
        store.clear().expect("clear again");
    }

    #[test]
    fn malformed_file_is_reported_not_cleared() {
        let directory = tempfile::tempdir().expect("tempdir");
        let path = directory.path().join("credentials.json");
        std::fs::write(&path, b"{ not json").expect("write");
        let store = CredentialStore::with_path(&path);
        assert_eq!(store.load().unwrap_err(), AuthStoreError::Malformed);
        assert!(path.exists(), "malformed file must not be discarded");
    }

    #[test]
    fn wrong_version_is_malformed() {
        let directory = tempfile::tempdir().expect("tempdir");
        let path = directory.path().join("credentials.json");
        std::fs::write(
            &path,
            br#"{"version":99,"access_token":"a","refresh_token":"r","expires_at_ms":1}"#,
        )
        .expect("write");
        let store = CredentialStore::with_path(&path);
        assert_eq!(store.load().unwrap_err(), AuthStoreError::Malformed);
    }

    #[test]
    fn oversized_file_is_malformed_and_preserved() {
        let directory = tempfile::tempdir().expect("tempdir");
        let path = directory.path().join("credentials.json");
        let contents = vec![b'x'; MAX_STORE_BYTES as usize + 1];
        std::fs::write(&path, &contents).expect("write");
        let store = CredentialStore::with_path(&path);
        assert_eq!(store.load().unwrap_err(), AuthStoreError::Malformed);
        assert_eq!(std::fs::read(path).expect("preserved"), contents);
    }

    #[test]
    fn debug_does_not_reveal_tokens() {
        let store = CredentialStore::with_path("/tmp/example/credentials.json");
        let rendered = format!("{store:?}");
        assert!(rendered.contains("credentials.json"));
        assert!(!rendered.contains("access-token"));
    }
}
