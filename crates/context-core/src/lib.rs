//! Consent-based screen context and model routing for developer practice.
pub mod a11y;
pub mod accessibility;
pub mod ai;
pub mod audio;
pub mod auth_store;
pub mod capture;
#[cfg(feature = "codex-auth-experimental")]
pub mod codex_auth;
pub mod config;
pub mod context;
pub mod ocr;
pub mod openai_device_auth;
pub mod paths;
pub mod platform;
pub mod session;
pub mod shortcuts;
