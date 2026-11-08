use crate::ScheduleSnapshot;
use sha2::{Digest, Sha256};

pub fn compute_snapshot_hash(snapshot: &ScheduleSnapshot) -> String {
    let mut parts = vec![snapshot.group_name.clone()];
    for day in &snapshot.days {
        parts.push(day.date_iso.clone());
        if let Some(week @ (1 | 2)) = day.week_variant {
            parts.push(format!("week_variant:{week}"));
        }
        let mut lessons: Vec<_> = day.lessons.iter().collect();
        lessons.sort_by(|a, b| {
            (a.number, &a.subject, &a.teacher, &a.classroom).cmp(&(
                b.number,
                &b.subject,
                &b.teacher,
                &b.classroom,
            ))
        });
        for lesson in lessons {
            parts.push(format!(
                "{}|{}|{}|{}",
                lesson.number, lesson.subject, lesson.teacher, lesson.classroom
            ));
        }
    }
    format!("{:x}", Sha256::digest(parts.join("\n").as_bytes()))
}

fn text_of(element: scraper::ElementRef<'_>) -> String {
    element
        .text()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}
fn selector(value: &str) -> scraper::Selector {
    scraper::Selector::parse(value).expect("static selector")
}

pub fn date_label_to_iso(label: &str, now: chrono::NaiveDate) -> String {
    use chrono::Datelike;
    let lower = label.to_lowercase().replace(',', " ");
    let parts: Vec<_> = lower.split_whitespace().collect();
    if parts.len() < 2 {
        return label.to_owned();
    }
    let month = match parts[1] {
        "января" => 1,
        "февраля" => 2,
        "марта" => 3,
        "апреля" => 4,
        "мая" => 5,
        "июня" => 6,
        "июля" => 7,
        "августа" => 8,
        "сентября" => 9,
        "октября" => 10,
        "ноября" => 11,
        "декабря" => 12,
        _ => 1,
    };
    let year = parts[2..]
        .iter()
        .find(|s| s.len() == 4 && s.bytes().all(|b| b.is_ascii_digit()))
        .and_then(|s| s.parse::<i32>().ok())
        .unwrap_or_else(|| {
            if now.month() == 12 && month <= 2 {
                now.year() + 1
            } else if now.month() == 1 && month >= 11 {
                now.year() - 1
            } else {
                now.year()
            }
        });
    format!("{year:04}-{month:02}-{:0>2}", parts[0])
}
pub fn parse_college_html(html: &str, now: chrono::NaiveDateTime) -> ScheduleSnapshot {
    use crate::{DaySchedule, Lesson};
    let doc = scraper::Html::parse_document(html);
    let group_name = doc
        .select(&selector("div#titleF"))
        .next()
        .map(|e| e.text().collect::<String>().trim().to_string())
        .unwrap_or_else(|| "Неизвестная группа".into());
    let mut days = Vec::new();
    for node in doc.select(&selector("div.titleDate")) {
        let label = text_of(node);
        let schedule = node
            .next_siblings()
            .filter_map(scraper::ElementRef::wrap)
            .find(|e| {
                e.value().name() == "div"
                    && e.value()
                        .has_class("rasp", scraper::CaseSensitivity::CaseSensitive)
            });
        let Some(schedule) = schedule else {
            continue;
        };
        let mut lessons = Vec::new();
        for row in schedule.select(&selector("table tr")).skip(1) {
            let cells: Vec<_> = row.select(&selector("td")).map(text_of).collect();
            if cells.len() < 4 {
                continue;
            }
            if let Ok(number) = cells[0].parse() {
                lessons.push(Lesson {
                    number,
                    subject: cells[1].clone(),
                    teacher: cells[2].clone(),
                    classroom: cells[3].clone(),
                });
            }
        }
        days.push(DaySchedule {
            date_iso: date_label_to_iso(&label, now.date()),
            date_label: label,
            lessons,
            week_variant: None,
            current_week_variant: None,
        });
    }
    ScheduleSnapshot {
        group_name,
        fetched_at: now.format("%Y-%m-%dT%H:%M:%S").to_string(),
        days,
    }
}

pub fn parse_university_html(
    html: &str,
    reference: chrono::NaiveDateTime,
    align_current_week: bool,
) -> Result<ScheduleSnapshot, String> {
    use crate::{DaySchedule, Lesson};
    use chrono::Duration;
    use std::collections::HashSet;
    let doc = scraper::Html::parse_document(html);
    let table = doc
        .select(&selector("#schedule-table"))
        .next()
        .ok_or("Неполная страница расписания университета")?;
    let title = doc
        .select(&selector("#title-value"))
        .next()
        .ok_or("Неполная страница расписания университета")?;
    let info = doc
        .select(&selector("#schedule-title .misc-schedule > span"))
        .next()
        .ok_or("Неполная страница расписания университета")?;
    let info_text = text_of(info)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    let parity_re = regex::Regex::new(
        r"На этой неделе учимся по (числителю|знаменателю).*показано по (числителю|знаменателю)",
    )
    .unwrap();
    let parity = parity_re
        .captures(&info_text)
        .ok_or("Неизвестная неделя расписания университета")?;
    let variant = |value: &str| if value == "числителю" { 1 } else { 2 };
    let current = variant(&parity[1]);
    let shown = variant(&parity[2]);
    let mut monday = crate::calendar::week_start(reference.date());
    if !align_current_week && current != shown {
        monday += Duration::days(7);
    }
    let weekdays = [
        "понедельник",
        "вторник",
        "среда",
        "четверг",
        "пятница",
        "суббота",
        "воскресенье",
    ];
    let numbers: Vec<i64> = table
        .select(&selector(".table-lesson-num"))
        .map(|e| text_of(e).parse::<i64>())
        .collect::<Result<_, _>>()
        .map_err(|_| "Не удалось прочитать номера пар университета")?;
    if numbers.is_empty()
        || numbers.iter().any(|n| *n <= 0)
        || numbers.iter().collect::<HashSet<_>>().len() != numbers.len()
    {
        return Err("Не удалось прочитать номера пар университета".into());
    }
    let mut days = Vec::new();
    for row in table.select(&selector("tr")) {
        let Some(weekday) = row.select(&selector(".table-weekdays")).next() else {
            continue;
        };
        let name = text_of(weekday).to_lowercase();
        let index = weekdays
            .iter()
            .position(|w| *w == name)
            .ok_or_else(|| format!("Неизвестный день недели: {name}"))?;
        let cells: Vec<_> = row
            .children()
            .filter_map(scraper::ElementRef::wrap)
            .filter(|e| e.value().name() == "td")
            .collect();
        if cells.len() != numbers.len() {
            return Err("Изменилась структура таблицы расписания университета".into());
        }
        let mut lessons = Vec::new();
        for (number, cell) in numbers.iter().zip(cells) {
            let subgroups: Vec<_> = cell.select(&selector(".table-subgroup-item")).collect();
            let blocks = if subgroups.is_empty() {
                vec![cell]
            } else {
                subgroups
            };
            for block in blocks {
                let value = |css| {
                    block
                        .select(&selector(css))
                        .next()
                        .map(|e| text_of(e).split_whitespace().collect::<Vec<_>>().join(" "))
                        .unwrap_or_default()
                };
                let subject = value(".table-subject");
                if subject.is_empty() {
                    if !cell
                        .value()
                        .has_class("table-empty", scraper::CaseSensitivity::CaseSensitive)
                        && block
                            .select(&selector(".schedule-subgroup-empty"))
                            .next()
                            .is_none()
                    {
                        return Err("Не удалось прочитать занятие университета".into());
                    }
                    continue;
                }
                let details: Vec<String> = [value(".table-lessontype"), value(".table-sg-name")]
                    .into_iter()
                    .filter(|s| !s.is_empty())
                    .collect();
                lessons.push(Lesson {
                    number: *number,
                    subject: if details.is_empty() {
                        subject
                    } else {
                        format!("{subject} ({})", details.join(", "))
                    },
                    teacher: value(".table-teacher"),
                    classroom: value(".table-room"),
                });
            }
        }
        let date = (monday + Duration::days(index as i64)).to_string();
        days.push(DaySchedule {
            date_label: date.clone(),
            date_iso: date,
            lessons,
            week_variant: Some(shown),
            current_week_variant: Some(current),
        });
    }
    if days.len() != 7
        || days
            .iter()
            .map(|d| &d.date_iso)
            .collect::<HashSet<_>>()
            .len()
            != 7
    {
        return Err("Неполная неделя расписания университета".into());
    }
    let title = text_of(title);
    Ok(ScheduleSnapshot {
        group_name: title
            .strip_prefix("Расписание для ")
            .unwrap_or(&title)
            .into(),
        fetched_at: reference.format("%Y-%m-%dT%H:%M:%S").to_string(),
        days,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{DaySchedule, Lesson};
    #[test]
    fn hash_matches_python_and_is_independent_of_lesson_order() {
        let mut snapshot = ScheduleSnapshot {
            group_name: "ИСП-25".into(),
            fetched_at: "2026-11-08T12:00:00".into(),
            days: vec![DaySchedule {
                date_label: "".into(),
                date_iso: "2026-11-09".into(),
                week_variant: Some(1),
                current_week_variant: None,
                lessons: vec![
                    Lesson {
                        number: 1,
                        subject: "Физика".into(),
                        teacher: "Иванов".into(),
                        classroom: "101".into(),
                    },
                    Lesson {
                        number: 1,
                        subject: "Алгебра".into(),
                        teacher: "Петров".into(),
                        classroom: "202".into(),
                    },
                ],
            }],
        };
        let original = compute_snapshot_hash(&snapshot);
        assert_eq!(
            original,
            "30ba8d004400f6d8b31ca21b5167ba638dcd97d455bed0de92f33b9e29eb6050"
        );
        snapshot.days[0].lessons.reverse();
        assert_eq!(compute_snapshot_hash(&snapshot), original);
    }
}

#[cfg(test)]
mod html_tests {
    use super::*;
    #[test]
    fn college_html_and_year_transition() {
        let now = chrono::NaiveDate::from_ymd_opt(2026, 12, 30)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        let html = r#"<div id="titleF">ИСП-25</div><div class="titleDate">1 января</div><div class="rasp"><table><tr><th>Номер</th></tr><tr><td>1</td><td>Физика</td><td>Иванов</td><td>101</td></tr><tr><td>не номер</td><td>x</td><td>x</td><td>x</td></tr></table></div>"#;
        let snapshot = parse_college_html(html, now);
        assert_eq!(snapshot.group_name, "ИСП-25");
        assert_eq!(snapshot.days[0].date_iso, "2027-01-01");
        assert_eq!(snapshot.days[0].lessons.len(), 1);
        assert_eq!(snapshot.days[0].lessons[0].subject, "Физика");
        assert_eq!(
            date_label_to_iso(
                "31 декабря",
                chrono::NaiveDate::from_ymd_opt(2027, 1, 1).unwrap()
            ),
            "2026-12-31"
        );
    }
}

#[cfg(test)]
mod university_tests {
    use super::*;
    #[test]
    fn parses_subgroups_and_rejects_incomplete_week() {
        let weekdays = [
            "Понедельник",
            "Вторник",
            "Среда",
            "Четверг",
            "Пятница",
            "Суббота",
            "Воскресенье",
        ];
        let rows = weekdays.iter().map(|day| format!(r#"<tr><th class="table-weekdays">{day}</th><td><div class="table-subgroup-item"><span class="table-subject">Физика</span><span class="table-sg-name">1 подгруппа</span><span class="table-teacher">Иванов</span></div></td><td class="table-empty">—</td></tr>"#)).collect::<String>();
        let html = format!(
            r#"<a id="title-value">Расписание для Группа</a><div id="schedule-title"><div class="misc-schedule"><span>На этой неделе учимся по знаменателю (показано по числителю)</span></div></div><table id="schedule-table"><tr><th><span class="table-lesson-num">1</span></th><th><span class="table-lesson-num">2</span></th></tr>{rows}</table>"#
        );
        let reference = chrono::NaiveDate::from_ymd_opt(2026, 12, 31)
            .unwrap()
            .and_hms_opt(12, 0, 0)
            .unwrap();
        let snapshot = parse_university_html(&html, reference, false).unwrap();
        assert_eq!(snapshot.group_name, "Группа");
        assert_eq!(snapshot.days.len(), 7);
        assert_eq!(snapshot.days[0].date_iso, "2027-01-04");
        assert_eq!(snapshot.days[0].lessons[0].subject, "Физика (1 подгруппа)");
        assert_eq!(snapshot.days[0].current_week_variant, Some(2));
        assert!(
            parse_university_html("<table id='schedule-table'></table>", reference, false).is_err()
        );
    }
}

pub struct ScheduleParser {
    college_base: String,
    university_base: String,
    client: reqwest::Client,
    retries: u32,
    backoff: std::time::Duration,
}
impl ScheduleParser {
    pub fn new(
        college_url: &str,
        university_url: &str,
        timeout: std::time::Duration,
        retries: u32,
        backoff: std::time::Duration,
    ) -> Result<Self, reqwest::Error> {
        let mut base = college_url.trim_end_matches('/').to_owned();
        if base
            .rsplit('/')
            .next()
            .is_some_and(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()))
        {
            if let Some(index) = base.rfind('/') {
                base.truncate(index);
            }
        }
        Ok(Self {
            college_base: base,
            university_base: university_url.trim_end_matches('/').into(),
            client: reqwest::Client::builder().timeout(timeout).build()?,
            retries: retries.max(1),
            backoff,
        })
    }
    pub fn build_schedule_url(&self, id: i64) -> Result<String, &'static str> {
        if id < 0 {
            if self.university_base.is_empty() {
                return Err("Источник расписания университета отключён");
            }
            Ok(format!(
                "{}/group/{}",
                self.university_base,
                id.unsigned_abs()
            ))
        } else {
            Ok(format!("{}/{id}", self.college_base))
        }
    }
    pub async fn fetch_html(&self, url: &str) -> Result<String, reqwest::Error> {
        for attempt in 1..=self.retries {
            let result = match self.client.get(url).send().await {
                Ok(response) => response.error_for_status(),
                Err(err) => Err(err),
            };
            match result {
                Ok(response) => return response.text().await,
                Err(err) => {
                    let retryable = err
                        .status()
                        .is_none_or(|status| !status.is_client_error() || status.as_u16() == 429);
                    if attempt == self.retries || !retryable {
                        return Err(err);
                    }
                    tokio::time::sleep(self.backoff.saturating_mul(attempt)).await;
                }
            }
        }
        unreachable!()
    }
}
#[cfg(test)]
mod client_tests {
    use super::*;
    #[test]
    fn urls_distinguish_college_and_university() {
        let parser = ScheduleParser::new(
            "http://college/rasp/600",
            "http://university/",
            std::time::Duration::from_secs(20),
            3,
            std::time::Duration::from_secs(1),
        )
        .unwrap();
        assert_eq!(
            parser.build_schedule_url(3917).unwrap(),
            "http://college/rasp/3917"
        );
        assert_eq!(
            parser.build_schedule_url(-3917).unwrap(),
            "http://university/group/3917"
        );
    }
}
impl ScheduleParser {
    pub async fn parse(
        &self,
        schedule_id: i64,
        reference: chrono::NaiveDateTime,
    ) -> Result<(ScheduleSnapshot, String), Box<dyn std::error::Error>> {
        self.parse_from_url(&self.build_schedule_url(schedule_id)?, reference)
            .await
    }
    pub async fn parse_from_url(
        &self,
        url: &str,
        reference: chrono::NaiveDateTime,
    ) -> Result<(ScheduleSnapshot, String), Box<dyn std::error::Error>> {
        let html = self.fetch_html(url).await?;
        let (is_university, onclick) = {
            let doc = scraper::Html::parse_document(&html);
            let is_university = doc.select(&selector("#schedule-table")).next().is_some();
            let onclick = doc
                .select(&selector("#schedule-title .search-button-active[onclick]"))
                .next()
                .and_then(|e| e.value().attr("onclick"))
                .map(str::to_owned);
            (is_university, onclick)
        };
        if crate::sources::university_kind(url).is_some() && !is_university {
            return Err("Страница университета не содержит таблицу расписания".into());
        }
        let mut snapshot = if is_university {
            parse_university_html(&html, reference, false)?
        } else {
            parse_college_html(&html, reference)
        };
        if is_university {
            let onclick = onclick
                .ok_or("Не удалось определить числитель/знаменатель расписания университета")?;
            let regex = regex::Regex::new(r#"/([12])['\"]"#)?;
            let active: i64 = regex
                .captures(&onclick)
                .and_then(|m| m.get(1))
                .ok_or("Не удалось определить числитель/знаменатель расписания университета")?
                .as_str()
                .parse()?;
            let base = url.trim_end_matches('/');
            let base = if base.ends_with("/1") || base.ends_with("/2") {
                &base[..base.len() - 2]
            } else {
                base
            };
            let other_html = self.fetch_html(&format!("{base}/{}", 3 - active)).await?;
            let other = parse_university_html(&other_html, reference, false)?;
            if snapshot.days.iter().any(|day| {
                other
                    .days
                    .iter()
                    .any(|other_day| day.date_iso == other_day.date_iso)
            }) {
                return Err("Сайт вернул одинаковую неделю для числителя и знаменателя".into());
            }
            snapshot.days.extend(other.days);
            snapshot.days.sort_by(|a, b| a.date_iso.cmp(&b.date_iso));
        }
        let digest = compute_snapshot_hash(&snapshot);
        Ok((snapshot, digest))
    }
}
