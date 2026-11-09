use misis_rasp_core::web::session::SessionSigner;

#[test]
fn python_signed_session_is_accepted_and_tampering_expiration_are_rejected() {
    let signer = SessionSigner::new("test-secret-with-at-least-32-characters").unwrap();
    let token = "eyJsb2dpbiI6ImFkbWluIiwiaWF0IjoxMDAsIm5vbmNlIjoiMDEyMzQ1Njc4OWFiY2RlZiJ9.c7a1cd1a19214af67daeffae919009b5e6f24e8485d612bd418a5d0a903dc0e1";
    assert_eq!(signer.verify(token, 110, 20).as_deref(), Some("admin"));
    assert_eq!(signer.verify(token, 121, 20), None);
    assert_eq!(signer.verify(token, 99, 20), None);
    assert_eq!(signer.verify(&format!("{token}0"), 110, 20), None);
    assert_eq!(signer.verify(token, 110, 0), None);
}

#[test]
fn new_sessions_are_random_and_require_strong_secret() {
    assert!(SessionSigner::new("short").is_err());
    let signer = SessionSigner::new("test-secret-with-at-least-32-characters").unwrap();
    let a = signer.sign("админ", 100).unwrap();
    let b = signer.sign("админ", 100).unwrap();
    assert_ne!(a, b);
    assert_eq!(signer.verify(&a, 100, 60).as_deref(), Some("админ"));
    assert!(signer.sign("", 100).is_err());
}
