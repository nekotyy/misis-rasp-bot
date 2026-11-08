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
