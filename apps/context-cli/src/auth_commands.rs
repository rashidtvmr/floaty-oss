//! User-initiated Codex login controls using the official app-server protocol.
//! ContextLens never reads or handles the user's persisted Codex credentials.
use context_core::codex_auth::{CodexAuthSession, LoginMethod, open_login_page};
use std::time::Duration;

fn has(args: &[String], flag: &str) -> bool {
    args.iter().any(|argument| argument == flag)
}

pub async fn run(action: &str, args: &[String]) -> Result<(), String> {
    if action == "codex-logout" && !has(args, "--confirm") {
        return Err("Signing out affects your shared Codex CLI session; add --confirm".into());
    }
    let method = match args.windows(2).find(|pair| pair[0] == "--method") {
        Some(pair) if pair[1] == "device" => LoginMethod::Device,
        Some(pair) if pair[1] == "browser" => LoginMethod::Browser,
        None => LoginMethod::Browser,
        _ => return Err("--method must be browser or device".into()),
    };

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
