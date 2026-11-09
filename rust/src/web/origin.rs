//! Проверка Origin/Referer для изменяющих состояние запросов админки.
//! Ожидаемый адрес должен быть получен только из доверенной конфигурации
//! или заголовков доверенного обратного прокси.

use url::Url;

fn origin(value: &str) -> Option<(String, String, u16)> {
    let parsed = Url::parse(value).ok()?;
    if !matches!(parsed.scheme(), "http" | "https")
        || !parsed.username().is_empty()
        || parsed.password().is_some()
    {
        return None;
    }
    Some((
        parsed.scheme().to_owned(),
        parsed.host_str()?.to_owned(),
        parsed.port_or_known_default()?,
    ))
}

/// Если Origin присутствует, Referer не может переопределить его значение.
pub fn allows_mutation(
    expected: &str,
    request_origin: Option<&str>,
    referer: Option<&str>,
) -> bool {
    let Some(expected) = origin(expected) else {
        return false;
    };
    let Some(candidate) = request_origin.or(referer).and_then(origin) else {
        return false;
    };
    expected == candidate
}
