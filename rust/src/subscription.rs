pub fn subscription_caption(
    kind: Option<&str>,
    title: Option<&str>,
    audience: Option<&str>,
) -> Option<String> {
    let (kind, title) = (
        kind.filter(|x| !x.is_empty())?,
        title.filter(|x| !x.is_empty())?,
    );
    Some(match kind {
        "teacher" => {
            let audience_line = match audience.filter(|x| !x.is_empty()) {
                Some(name) => format!("\nВыбран кабинет: {name}"),
                None => "\nКабинет пока не выбран.".to_string(),
            };
            format!("Выбран преподаватель: {title}{audience_line}\n\nСайт может загружать изменения в расписании с задержкой. Если пары проходят в одном кабинете, дополнительно сверяй расписание по кабинету: там обновления обычно появляются быстрее.")
        }
        "audience" => format!("Выбран кабинет: {title}"),
        _ => format!("Твоя группа: {title}"),
    })
}

pub fn normalize_group(value: &str) -> String {
    use unicode_casefold::UnicodeCaseFold;
    let mapped = value
        .trim()
        .chars()
        .map(|c| match c {
            'A' => 'А',
            'a' => 'а',
            'B' => 'В',
            'E' => 'Е',
            'e' => 'е',
            'K' => 'К',
            'k' => 'к',
            'M' => 'М',
            'H' => 'Н',
            'O' => 'О',
            'o' => 'о',
            'P' => 'Р',
            'p' => 'р',
            'C' => 'С',
            'c' => 'с',
            'T' => 'Т',
            'Y' => 'У',
            'y' => 'у',
            'X' => 'Х',
            'x' => 'х',
            other => other,
        })
        .collect::<String>();
    let folded: String = mapped.as_str().case_fold().collect();
    let folded = crate::normalize_dashes(&folded.replace('ё', "е"));
    let dash = regex::Regex::new(r"\s*-\s*").unwrap();
    dash.replace_all(&folded, "-")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}
#[derive(Debug, PartialEq, Eq)]
pub struct Subscription {
    pub subscription_type: String,
    pub subscription_key: String,
    pub subscription_title: String,
    pub subscription_url: String,
    pub group_name: Option<String>,
    pub schedule_id: Option<i64>,
}
pub fn group_subscription(name: &str, schedule_id: Option<i64>) -> Subscription {
    Subscription {
        subscription_type: "group".into(),
        subscription_key: schedule_id
            .map(|id| format!("group:{id}"))
            .unwrap_or_else(|| format!("group-pending:{}", normalize_group(name))),
        subscription_title: name.into(),
        subscription_url: schedule_id
            .map(|id| format!("rasp:{id}"))
            .unwrap_or_default(),
        group_name: Some(name.into()),
        schedule_id,
    }
}
pub fn teacher_subscription(title: &str, url: &str) -> Subscription {
    let key = if let Some(id) = crate::sources::schedule_numeric_id(url) {
        format!("teacher:{id}")
    } else if !url.is_empty() {
        format!("teacher:{url}")
    } else {
        format!("teacher-pending:{}", normalize_group(title))
    };
    Subscription {
        subscription_type: "teacher".into(),
        subscription_key: key,
        subscription_title: title.into(),
        subscription_url: url.into(),
        group_name: None,
        schedule_id: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn captions_match_existing_user_interface() {
        assert_eq!(subscription_caption(None, Some("x"), None), None);
        assert_eq!(
            subscription_caption(Some("group"), Some("Г-1"), None).unwrap(),
            "Твоя группа: Г-1"
        );
        assert_eq!(
            subscription_caption(Some("audience"), Some("101"), None).unwrap(),
            "Выбран кабинет: 101"
        );
        let teacher = subscription_caption(Some("teacher"), Some("Иванов"), Some("101")).unwrap();
        assert!(teacher.starts_with("Выбран преподаватель: Иванов\nВыбран кабинет: 101\n\n"));
        assert!(subscription_caption(Some("teacher"), Some("Иванов"), None)
            .unwrap()
            .contains("Кабинет пока не выбран."));
    }
}

#[cfg(test)]
mod key_tests {
    use super::*;
    #[test]
    fn pending_and_site_subscriptions_do_not_collide() {
        assert_eq!(normalize_group(" ИCП — 25 Ё "), "исп-25 е");
        assert_eq!(normalize_group("Straße"), "strаssе");
        assert_eq!(
            group_subscription("ИСП-25", Some(10)).subscription_key,
            "group:10"
        );
        assert_eq!(
            group_subscription("ИCП - 25", None).subscription_key,
            "group-pending:исп-25"
        );
        assert_eq!(
            teacher_subscription("Иванов", "").subscription_key,
            "teacher-pending:иванов"
        );
        assert_eq!(
            teacher_subscription("Иванов", "http://r.sf-misis.ru/teacher/12").subscription_key,
            "teacher:-12"
        );
    }
}
