use misis_rasp_core::web::origin::allows_mutation;

#[test]
fn accepts_same_origin_or_referer_with_normalized_default_port() {
    assert!(allows_mutation(
        "https://example.test",
        Some("https://example.test:443"),
        None
    ));
    assert!(allows_mutation(
        "http://localhost:8000",
        None,
        Some("http://localhost:8000/login?x=1")
    ));
    assert!(allows_mutation(
        "http://[::1]:8000",
        Some("http://[::1]:8000"),
        None
    ));
}

#[test]
fn rejects_missing_cross_origin_or_untrusted_urls() {
    let expected = "https://example.test";
    assert!(!allows_mutation(expected, None, None));
    assert!(!allows_mutation(
        expected,
        Some("null"),
        Some("https://example.test/")
    ));
    assert!(!allows_mutation(
        expected,
        Some("https://attacker.test"),
        Some("https://example.test/")
    ));
    assert!(!allows_mutation(
        expected,
        Some("http://example.test"),
        None
    ));
    assert!(!allows_mutation(
        expected,
        Some("https://example.test.attacker.test"),
        None
    ));
    assert!(!allows_mutation(
        expected,
        Some("https://example.test@attacker.test"),
        None
    ));
    assert!(!allows_mutation(
        expected,
        Some("https://example.test:444"),
        None
    ));
    assert!(!allows_mutation("not-a-url", Some(expected), None));
}
