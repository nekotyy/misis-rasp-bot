use regex::Regex;
use serde_json::Value;
use std::sync::OnceLock;

fn path(url: &str) -> &str {
    let without_suffix = url.split(['?', '#']).next().unwrap_or("");
    if let Some((_, rest)) = without_suffix.split_once("://") {
        rest.find('/').map(|i| &rest[i..]).unwrap_or("")
    } else if let Some(rest) = without_suffix.strip_prefix("//") {
        rest.find('/').map(|i| &rest[i..]).unwrap_or("")
    } else {
        without_suffix
    }
}
pub fn university_kind(url: &str) -> Option<&str> {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^/(group|teacher|room)/[1-9][0-9]*(?:/[12])?/?$").unwrap())
        .captures(path(url))
        .and_then(|m| m.get(1))
        .map(|m| m.as_str())
}
pub fn schedule_numeric_id(url: &str) -> Option<i64> {
    let p = path(url).trim_matches('/');
    if university_kind(url).is_some() {
        p.split('/').nth(1)?.parse::<i64>().ok()?.checked_neg()
    } else {
        let last = p.rsplit('/').next()?;
        if last.is_empty() || !last.bytes().all(|c| c.is_ascii_digit()) {
            return None;
        }
        last.parse().ok()
    }
}
pub fn source_key_from_url(kind: &str, url: &str) -> String {
    match schedule_numeric_id(url) {
        Some(id) => format!("{kind}:{id}"),
        None => format!("{kind}:{url}"),
    }
}
pub fn parse_university_directory(text: &str, kind: &str) -> Result<Vec<Value>, String> {
    let mut payload = text.trim();
    if payload.starts_with("var database") {
        payload = payload
            .split_once('=')
            .ok_or("Некорректная обёртка каталога")?
            .1
            .trim();
        payload = payload.strip_suffix(';').unwrap_or(payload);
    }
    let json: Value = serde_json::from_str(payload).map_err(|e| e.to_string())?;
    let rows = json
        .get(kind)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("Некорректный каталог университета: {kind}"))?;
    if rows.iter().any(|row| {
        row.get("value")
            .and_then(Value::as_i64)
            .is_none_or(|v| v <= 0)
            || row
                .get("content")
                .and_then(Value::as_str)
                .is_none_or(|s| s.trim().is_empty())
    }) {
        return Err(format!("Некорректный каталог университета: {kind}"));
    }
    Ok(rows.clone())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ids_and_keys_keep_sources_separate() {
        assert_eq!(
            university_kind("http://r.sf-misis.ru/group/123/2?x=1"),
            Some("group")
        );
        assert_eq!(
            schedule_numeric_id("http://r.sf-misis.ru/group/123/2"),
            Some(-123)
        );
        assert_eq!(
            schedule_numeric_id("https://example.ru/rasp/123/"),
            Some(123)
        );
        assert_eq!(source_key_from_url("teacher", "abc"), "teacher:abc");
        assert_eq!(university_kind("/group/01"), None);
    }
    #[test]
    fn directory_does_not_execute_javascript() {
        let rows = parse_university_directory(
            r#"var database = {"group":[{"value": 1, "content": "Группа"}]};"#,
            "group",
        )
        .unwrap();
        assert_eq!(rows.len(), 1);
        assert!(
            parse_university_directory(r#"{"group":[{"value":0,"content":"x"}]}"#, "group")
                .is_err()
        );
        assert!(parse_university_directory("var database = alert(1);", "group").is_err());
    }
}
