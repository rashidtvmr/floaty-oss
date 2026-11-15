//! User-initiated Codex login controls.
//!
//! Two paths exist:
//! - the default one drives the official Codex app-server protocol;
//! - `--native` uses SocketStream' own device-authorization flow and local
//!   credential store, so sign-in works without the Codex CLI installed.
//!
//! SocketStream never reads or handles the user's persisted Codex CLI credentials.
use socket_stream_core::auth_store::CredentialStore;
use socket_stream_core::codex_auth::{
    CodexAuthSession, LoginMethod, load_native_status, open_login_page,
};
use socket_stream_core::openai_device_auth::{
    begin_device_authorization, device_auth_client, poll_device_authorization,
};
use std::time::Duration;

fn has(args: &[String], flag: &str) -> bool {
    args.iter().any(|argument| argument == flag)
}

fn method(args: &[String]) -> Result<LoginMethod, String> {
    match args.windows(2).find(|pair| pair[0] == "--method") {
        Some(pair) if pair[1] == "device" => Ok(LoginMethod::Device),
        Some(pair) if pair[1] == "browser" => Ok(LoginMethod::Browser),
        None => Ok(LoginMethod::Browser),
        _ => Err("--method must be browser or device".into()),
    }
}

/// Native device flow: no Codex CLI required, credential stored locally.
async fn run_native(action: &str, args: &[String]) -> Result<(), String> {
    match action {
        "codex-status" => {
            let account = load_native_status().map_err(|error| error.to_string())?;
            println!(
                "Codex (native): {}; authentication: {}; plan: {}. Sign-in does not guarantee available credits.",
                if account.signed_in {
                    "signed in"
                } else {
                    "signed out"
                },
                account.auth_type,
                account.plan.as_deref().unwrap_or("not reported")
            );
            Ok(())
        }
        "codex-logout" => {
            CredentialStore::new()
                .clear()
                .map_err(|error| error.to_string())?;
            println!(
                "Cleared the SocketStream credential stored on this machine. Your Codex CLI session is untouched."
            );
            Ok(())
        }
        "codex-login" => {
            if method(args)? != LoginMethod::Device {
                return Err("native sign-in supports --method device only".into());
            }
            let client = device_auth_client().map_err(|error| error.to_string())?;
            let authorization = begin_device_authorization(&client)
                .await
                .map_err(|error| error.to_string())?;
            println!("Open {} in your browser.", authorization.verification_url);
            println!(
                "Enter this one-time device code: {}",
                authorization.user_code
            );
            println!("Keep the code private. Ctrl+C or Stop cancels.");

            let outcome = tokio::select! {
                result = poll_device_authorization(&client, &authorization) => Some(result),
                _ = tokio::signal::ctrl_c() => None,
            };
            match outcome {
                Some(Ok(credential)) => {
                    CredentialStore::new()
                        .save(&credential)
                        .map_err(|error| error.to_string())?;
                    println!(
                        "Codex sign-in completed and stored locally. Native inference is not yet connected; the existing Codex CLI provider remains separate."
                    );
                    Ok(())
                }
                Some(Err(error)) => Err(error.to_string()),
                None => Err("Codex sign-in stopped".into()),
            }
        }
        _ => Err("unknown authentication command".into()),
    }
}

pub async fn run(action: &str, args: &[String]) -> Result<(), String> {
    let native = has(args, "--native");
    if action == "codex-logout" && !has(args, "--confirm") {
        return Err(if native {
            "Clearing the native SocketStream credential requires --confirm".into()
        } else {
            "Signing out affects your shared Codex CLI session; add --confirm".into()
        });
    }
    if native {
        return run_native(action, args).await;
    }
    let method = method(args)?;

    let mut client = CodexAuthSession::connect()
        .await
        .map_err(|error| error.to_string())?;
    let result = match action {
        "codex-status" => {
            let account = client.status().await.map_err(|error| error.to_string())?;
            println!(
                "Codex: {}; authentication: {}; plan: {}. Sign-in does not guarantee available credits.",
                if account.signed_in {
                    "signed in"
                } else {
                    "signed out"
                },
                account.auth_type,
                account.plan.as_deref().unwrap_or("not reported")
            );
            Ok(())
        }
        "codex-logout" => {
            client.logout().await.map_err(|error| error.to_string())?;
            println!("Signed out from the shared Codex login.");
            Ok(())
        }
        "codex-login" => {
            let challenge = client
                .start_login(method)
                .await
                .map_err(|error| error.to_string())?;
            if method == LoginMethod::Browser {
                if let Err(error) = open_login_page(&challenge.url).await {
                    let _ = client.cancel_login(&challenge.login_id).await;
                    return Err(format!(
                        "Could not open the Codex sign-in page ({error}); try --method device"
                    ));
                }
                println!("Complete Codex sign-in in your browser. Ctrl+C or Stop cancels.");
            } else {
                println!("Open {} in your browser.", challenge.url);
                println!(
                    "Enter this one-time device code: {}",
                    challenge.user_code.as_deref().unwrap_or("unavailable")
                );
                println!("Keep the code private. Ctrl+C or Stop cancels.");
            }
            let login = tokio::select! {
                outcome = client.wait_for_login(&challenge.login_id) => Some(outcome),
                _ = tokio::signal::ctrl_c() => None,
                _ = tokio::time::sleep(Duration::from_secs(600)) => None,
            };
            match login {
                Some(Ok(())) => {
                    let summary = client.status().await.map_err(|error| error.to_string())?;
                    if !summary.signed_in {
                        Err("Codex did not report a signed-in account".into())
                    } else {
                        println!("Codex sign-in completed. Select the codex provider to use it.");
                        Ok(())
                    }
                }
                Some(Err(error)) => Err(error.to_string()),
                None => {
                    let _ = tokio::time::timeout(
                        Duration::from_secs(3),
                        client.cancel_login(&challenge.login_id),
                    )
                    .await;
                    Err("Codex sign-in stopped or timed out".into())
                }
            }
        }
        _ => Err("unknown authentication command".into()),
    };
    client.close().await;
    result
}
