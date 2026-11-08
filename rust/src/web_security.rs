use base64::{engine::general_purpose::STANDARD, Engine};
use subtle::ConstantTimeEq;

const ITERATIONS: u32 = 180_000;
pub fn hash_password(password: &str) -> Result<String, getrandom::Error> {
    let mut salt = [0_u8; 16];
    getrandom::getrandom(&mut salt)?;
    let mut digest = [0_u8; 32];
    pbkdf2::pbkdf2_hmac::<sha2::Sha256>(password.as_bytes(), &salt, ITERATIONS, &mut digest);
    Ok(format!(
        "pbkdf2_sha256${}${}",
        STANDARD.encode(salt),
        STANDARD.encode(digest)
    ))
}
pub fn verify_password(password: &str, hash: &str) -> bool {
    let mut fields = hash.split('$');
    let (Some("pbkdf2_sha256"), Some(salt), Some(expected), None) =
        (fields.next(), fields.next(), fields.next(), fields.next())
    else {
        return false;
    };
    let (Ok(salt), Ok(expected)) = (STANDARD.decode(salt), STANDARD.decode(expected)) else {
        return false;
    };
    if expected.len() != 32 {
        return false;
    }
    let mut actual = [0_u8; 32];
    pbkdf2::pbkdf2_hmac::<sha2::Sha256>(password.as_bytes(), &salt, ITERATIONS, &mut actual);
    bool::from(actual.as_slice().ct_eq(&expected))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn verifies_python_password_hash_and_rejects_invalid() {
        let hash =
            "pbkdf2_sha256$AAECAwQFBgcICQoLDA0ODw==$ZWy4QZ0H0lAp0T4Z1pV0M15m8eXfTTMGZ5QrOj+fab0=";
        assert!(verify_password("test-password", hash));
        assert!(!verify_password("wrong", hash));
        assert!(!verify_password("test-password", "pbkdf2_sha256$bad$bad"));
        let generated = hash_password("пароль").unwrap();
        assert!(verify_password("пароль", &generated));
        assert!(!verify_password("не пароль", &generated));
    }
}
