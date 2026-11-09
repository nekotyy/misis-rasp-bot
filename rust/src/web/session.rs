//! Подписанные сессии совместимы с Python-форматом; маршруты ещё закрыты.

use base64::{engine::general_purpose::URL_SAFE, Engine};
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;

#[derive(Serialize, Deserialize)]
struct SessionPayload {
    login: String,
    iat: i64,
    nonce: String,
}

pub struct SessionSigner {
    secret: Vec<u8>,
}

impl SessionSigner {
    pub fn new(secret: &str) -> Result<Self, &'static str> {
        let secret = secret.trim();
        if secret.chars().count() < 32
            || matches!(
                secret,
                "change-me-long-random-secret" | "dev-secret-change-me"
            )
        {
            return Err("WEB_CONFIG_SECRET must be unique and at least 32 characters");
        }
        Ok(Self {
            secret: secret.as_bytes().to_vec(),
        })
    }

    pub fn sign(&self, login: &str, now: i64) -> Result<String, &'static str> {
        if login.is_empty() {
            return Err("empty login");
        }
        let mut random = [0_u8; 8];
        getrandom::getrandom(&mut random).map_err(|_| "randomness unavailable")?;
        let nonce = random.iter().map(|b| format!("{b:02x}")).collect();
        let data = URL_SAFE.encode(
            serde_json::to_vec(&SessionPayload {
                login: login.into(),
                iat: now,
                nonce,
            })
            .map_err(|_| "invalid session")?,
        );
        let mut mac = Hmac::<Sha256>::new_from_slice(&self.secret).map_err(|_| "invalid secret")?;
        mac.update(data.as_bytes());
        let signature: String = mac
            .finalize()
            .into_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        Ok(format!("{data}.{signature}"))
    }

    pub fn verify(&self, token: &str, now: i64, max_age: i64) -> Option<String> {
        if max_age <= 0 || token.len() > 4096 {
            return None;
        }
        let (data, signature) = token.rsplit_once('.')?;
        if signature.len() != 64 || !signature.is_ascii() {
            return None;
        }
        let bytes = signature
            .as_bytes()
            .as_chunks::<2>()
            .0
            .iter()
            .map(|pair| {
                let text = std::str::from_utf8(pair).ok()?;
                u8::from_str_radix(text, 16).ok()
            })
            .collect::<Option<Vec<_>>>()?;
        let mut mac = Hmac::<Sha256>::new_from_slice(&self.secret).ok()?;
        mac.update(data.as_bytes());
        mac.verify_slice(&bytes).ok()?;
        let payload: SessionPayload = serde_json::from_slice(&URL_SAFE.decode(data).ok()?).ok()?;
        if payload.login.is_empty()
            || payload.iat > now
            || now.saturating_sub(payload.iat) > max_age
        {
            return None;
        }
        Some(payload.login)
    }
}
