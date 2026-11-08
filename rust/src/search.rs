use std::collections::{HashMap, HashSet};
use unicode_normalization::UnicodeNormalization;
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SearchTarget {
    pub kind: String,
    pub title: String,
    pub url: String,
}
pub fn normalize(value: &str) -> String {
    let normalized: String = value.nfkc().collect();
    let normalized = crate::subscription::normalize_group(&normalized);
    let chars: Vec<_> = normalized.chars().collect();
    let mut result = String::new();
    let word = |c: char| c.is_alphanumeric() || c == '_';
    for (i, &c) in chars.iter().enumerate() {
        if c == '.' && i > 0 && i + 1 < chars.len() && word(chars[i - 1]) && word(chars[i + 1]) {
            result.push('.');
            result.push(' ');
        } else if word(c) || c.is_whitespace() || c == '.' || c == '-' {
            result.push(c);
        } else {
            result.push(' ');
        }
    }
    result.split_whitespace().collect::<Vec<_>>().join(" ")
}
fn compact(value: &str) -> String {
    value
        .chars()
        .filter(|c| c.is_alphanumeric() || *c == '_')
        .collect()
}
fn teacher_keys(title: &str) -> HashSet<String> {
    let normalized = normalize(title);
    let mut keys = HashSet::from([normalized.clone(), compact(&normalized)]);
    let parts: Vec<_> = normalized.split_whitespace().collect();
    if let Some(first) = parts.first() {
        keys.insert((*first).into());
    }
    if parts.len() >= 2 {
        let initials = format!(
            "{} {}",
            parts[0],
            parts[1..]
                .iter()
                .filter_map(|p| p.chars().next())
                .map(|c| format!("{c}."))
                .collect::<Vec<_>>()
                .join(" ")
        );
        keys.insert(compact(&initials));
        keys.insert(initials);
    }
    keys.remove("");
    keys
}
#[derive(Default)]
pub struct SearchCatalog {
    teachers: Vec<(String, SearchTarget)>,
    audiences: Vec<(String, SearchTarget)>,
    exact_teachers: HashMap<String, SearchTarget>,
    exact_audiences: HashMap<String, SearchTarget>,
}
fn insert_exact(
    map: &mut HashMap<String, SearchTarget>,
    ambiguous: &mut HashSet<String>,
    key: &str,
    target: &SearchTarget,
) {
    if map
        .get(key)
        .is_some_and(|existing| existing.url != target.url)
    {
        ambiguous.insert(key.into());
        map.remove(key);
    } else if !ambiguous.contains(key) {
        map.insert(key.into(), target.clone());
    }
}
impl SearchCatalog {
    pub fn new(teachers: &[(String, String)], audiences: &[(String, String)]) -> Self {
        let mut result = Self::default();
        let mut ambiguous = HashSet::new();
        for (title, url) in teachers {
            let target = SearchTarget {
                kind: "teacher".into(),
                title: title.clone(),
                url: url.clone(),
            };
            for key in teacher_keys(title) {
                insert_exact(&mut result.exact_teachers, &mut ambiguous, &key, &target);
                result.teachers.push((key, target.clone()));
            }
        }
        let mut ambiguous = HashSet::new();
        for (title, url) in audiences {
            let target = SearchTarget {
                kind: "audience".into(),
                title: title.clone(),
                url: url.clone(),
            };
            let key = normalize(title);
            insert_exact(&mut result.exact_audiences, &mut ambiguous, &key, &target);
            result.audiences.push((key, target));
        }
        result
    }
    pub fn find_all(
        &self,
        query: &str,
        groups: &crate::group_catalog::GroupCatalog,
    ) -> Vec<SearchTarget> {
        self.find_all_with_snapshots(query, groups, &[])
    }
    pub fn find_all_with_snapshots(
        &self,
        query: &str,
        groups: &crate::group_catalog::GroupCatalog,
        snapshots: &[crate::ScheduleSnapshot],
    ) -> Vec<SearchTarget> {
        let key = normalize(query);
        if key.is_empty() {
            return vec![];
        }
        if let Some(group) = groups.find(query) {
            return vec![SearchTarget {
                kind: "group".into(),
                title: group.group_name.clone(),
                url: group.url.clone(),
            }];
        }
        if let Some(target) = self.exact_teachers.get(&key) {
            return vec![target.clone()];
        }
        let teachers = partial(&key, &self.teachers);
        if !teachers.is_empty() {
            return teachers;
        }
        let fallback = teachers_from_snapshots(query, snapshots);
        if !fallback.is_empty() {
            return fallback;
        }
        if let Some(target) = self.exact_audiences.get(&key) {
            return vec![target.clone()];
        }
        partial(&key, &self.audiences)
    }
}
pub fn normalize_lesson(value: &str) -> String {
    use unicode_casefold::UnicodeCaseFold;
    let normalized: String = value.nfkc().collect();
    let folded: String = normalized.as_str().case_fold().collect();
    folded
        .replace('ё', "е")
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || c == '_' || c == '-' || c.is_whitespace() {
                c
            } else {
                ' '
            }
        })
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}
pub fn teacher_matches(query: &str, teacher: &str) -> bool {
    let normalized = normalize_lesson(teacher);
    if query.is_empty() || normalized.is_empty() {
        return false;
    }
    if query == normalized {
        return true;
    }
    let left = query.split_whitespace().collect::<Vec<_>>();
    let right = normalized.split_whitespace().collect::<Vec<_>>();
    !left.is_empty()
        && !right.is_empty()
        && left[0] == right[0]
        && (left.len() == 1 || normalized.contains(query))
}
pub fn teachers_from_snapshots(
    query: &str,
    snapshots: &[crate::ScheduleSnapshot],
) -> Vec<SearchTarget> {
    let query = normalize_lesson(query);
    if query.is_empty() {
        return vec![];
    }
    let mut variants: std::collections::BTreeMap<String, Vec<(String, usize)>> = Default::default();
    for snapshot in snapshots {
        for day in &snapshot.days {
            for lesson in &day.lessons {
                let raw = lesson.teacher.trim();
                let normalized = normalize_lesson(raw);
                if normalized.is_empty() {
                    continue;
                }
                let choices = variants.entry(normalized).or_default();
                if let Some((_, count)) = choices.iter_mut().find(|(name, _)| name == raw) {
                    *count += 1;
                } else {
                    choices.push((raw.into(), 1));
                }
            }
        }
    }
    variants
        .into_values()
        .filter_map(|choices| {
            let best = choices
                .iter()
                .enumerate()
                .max_by_key(|(index, (_, count))| (*count, std::cmp::Reverse(*index)))
                .map(|(_, entry)| &entry.0)?;
            if teacher_matches(&query, best) {
                Some(SearchTarget {
                    kind: "teacher".into(),
                    title: best.clone(),
                    url: String::new(),
                })
            } else {
                None
            }
        })
        .collect()
}
fn partial(query: &str, items: &[(String, SearchTarget)]) -> Vec<SearchTarget> {
    let query_compact = compact(query);
    let mut levels: [HashMap<String, SearchTarget>; 3] = Default::default();
    for (candidate, target) in items {
        let c = compact(candidate);
        let parts: Vec<_> = candidate.split_whitespace().collect();
        let level = if parts.contains(&query) || query_compact == c {
            Some(0)
        } else if parts.iter().any(|p| p.starts_with(query))
            || candidate.starts_with(query)
            || c.starts_with(&query_compact)
        {
            Some(1)
        } else if candidate.contains(query) || c.contains(&query_compact) {
            Some(2)
        } else {
            None
        };
        if let Some(i) = level {
            levels[i].insert(target.url.clone(), target.clone());
        }
    }
    for matches in levels {
        if !matches.is_empty() {
            let mut result: Vec<_> = matches.into_values().collect();
            result.sort_by_key(|t| (normalize(&t.title), t.url.clone()));
            return result;
        }
    }
    if query_compact.chars().count() < 5 {
        return vec![];
    }
    let mut ranked: HashMap<String, (f32, SearchTarget)> = HashMap::new();
    let a: Vec<char> = query_compact.chars().collect();
    for (candidate, target) in items {
        let b: Vec<char> = compact(candidate).chars().collect();
        if b.is_empty() {
            continue;
        }
        let ratio = difflib::sequencematcher::SequenceMatcher::new(&a[..], &b[..]).ratio();
        if ratio < 0.82 {
            continue;
        }
        let item = ranked
            .entry(target.url.clone())
            .or_insert((ratio, target.clone()));
        if ratio > item.0 {
            item.0 = ratio;
        }
    }
    let mut ordered: Vec<_> = ranked.into_values().collect();
    ordered.sort_by(|a, b| b.0.total_cmp(&a.0));
    match ordered.as_slice() {
        [(ratio, target)] if *ratio >= 0.86 => vec![target.clone()],
        [(ratio, target), (second, ..), ..] if *ratio >= 0.9 && ratio - second >= 0.03 => {
            vec![target.clone()]
        }
        _ => vec![],
    }
}
pub fn choice_label(target: &SearchTarget, targets: &[SearchTarget]) -> String {
    let source = crate::sources::university_kind(&target.url).is_some();
    let mixed = targets
        .iter()
        .any(|item| crate::sources::university_kind(&item.url).is_some() != source);
    if !mixed
        && targets
            .iter()
            .filter(|item| item.title == target.title)
            .count()
            < 2
    {
        return target.title.clone();
    }
    format!(
        "{} ({})",
        target.title,
        if source {
            "университет"
        } else {
            "колледж"
        }
    )
}

pub fn load_targets(
    db: &rusqlite::Connection,
    kind: &str,
) -> rusqlite::Result<Vec<(String, String)>> {
    let mut stmt = db.prepare("SELECT title,url FROM search_targets WHERE kind=?")?;
    let rows = stmt.query_map([kind], |r| Ok((r.get(0)?, r.get(1)?)))?;
    rows.collect()
}
pub fn save_targets(
    db: &mut rusqlite::Connection,
    kind: &str,
    targets: &[(String, String)],
    now: &str,
) -> rusqlite::Result<()> {
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    tx.execute("DELETE FROM search_targets WHERE kind=?", [kind])?;
    for (title, url) in targets {
        tx.execute(
            "INSERT INTO search_targets(kind,title,url,updated_at) VALUES (?,?,?,?)",
            rusqlite::params![kind, title, url, now],
        )?;
    }
    tx.commit()
}
pub fn parse_college_pairs(html: &str, origin: &str, kind: &str) -> Vec<(String, String)> {
    let doc = scraper::Html::parse_document(html);
    let css = if kind == "teacher" {
        "a[href^='/raspprep/']"
    } else {
        "a[href^='/raspAud/']"
    };
    let selector = scraper::Selector::parse(css).unwrap();
    doc.select(&selector)
        .filter_map(|link| {
            let title = link
                .text()
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .collect::<Vec<_>>()
                .join(" ");
            let href = link.value().attr("href")?;
            if title.is_empty() || href.is_empty() {
                None
            } else {
                Some((title, format!("{}{href}", origin.trim_end_matches('/'))))
            }
        })
        .collect()
}
pub async fn fetch_pairs(
    parser: &crate::parser::ScheduleParser,
    college: &str,
    university: &str,
    kind: &str,
    cached: &[(String, String)],
) -> Result<Vec<(String, String)>, String> {
    if !matches!(kind, "teacher" | "audience") {
        return Err("Неизвестный вид справочника".into());
    }
    let path = if kind == "teacher" { "prep" } else { "aud" };
    let mut pairs = Vec::new();
    let mut failures = 0;
    match parser
        .fetch_html(&format!("{}/{path}", college.trim_end_matches('/')))
        .await
    {
        Ok(html) => pairs.extend(parse_college_pairs(&html, college, kind)),
        Err(_) => {
            failures += 1;
            pairs.extend(
                cached
                    .iter()
                    .filter(|(_, url)| {
                        university.is_empty() || crate::sources::university_kind(url).is_none()
                    })
                    .cloned(),
            );
        }
    }
    if !university.is_empty() {
        let (directory, route) = if kind == "teacher" {
            ("teachers", "teacher")
        } else {
            ("rooms", "room")
        };
        let result = match parser
            .fetch_html(&format!(
                "{}/json/{directory}",
                university.trim_end_matches('/')
            ))
            .await
        {
            Ok(text) => crate::sources::parse_university_directory(&text, directory),
            Err(_) => Err("Сайт университета недоступен".into()),
        };
        match result {
            Ok(rows) => {
                for row in rows {
                    if let (Some(title), Some(id)) =
                        (row["content"].as_str(), row["value"].as_i64())
                    {
                        pairs.push((
                            title.trim().into(),
                            format!("{}/{route}/{id}", university.trim_end_matches('/')),
                        ));
                    }
                }
            }
            Err(_) => {
                failures += 1;
                pairs.extend(
                    cached
                        .iter()
                        .filter(|(_, url)| crate::sources::university_kind(url).is_some())
                        .cloned(),
                );
            }
        }
    }
    if failures == (if university.is_empty() { 1 } else { 2 }) && pairs.is_empty() {
        return Err("Справочники расписания недоступны".into());
    }
    let mut seen = HashSet::new();
    pairs.retain(|pair| seen.insert(pair.clone()));
    Ok(pairs)
}

pub fn teacher_snapshot(
    teacher: &str,
    group_snapshots: &[crate::database::StoredSnapshot],
    college_only: bool,
    fetched_at: &str,
) -> crate::ScheduleSnapshot {
    let normalized = normalize_lesson(teacher);
    let mut days: std::collections::BTreeMap<String, (String, Vec<crate::Lesson>)> =
        Default::default();
    for group in group_snapshots {
        if college_only && group.schedule_id.is_some_and(|id| id < 0) {
            continue;
        }
        let title = group
            .group_name
            .as_deref()
            .filter(|s| !s.is_empty())
            .or(group.source_title.as_deref())
            .unwrap_or("");
        for day in &group.content.days {
            if day.date_iso.is_empty() {
                continue;
            }
            for lesson in &day.lessons {
                if !teacher_matches(&normalized, &lesson.teacher) {
                    continue;
                }
                let entry = days.entry(day.date_iso.clone()).or_insert_with(|| {
                    (
                        if day.date_label.is_empty() {
                            day.date_iso.clone()
                        } else {
                            day.date_label.clone()
                        },
                        Vec::new(),
                    )
                });
                entry.1.push(crate::Lesson {
                    number: lesson.number,
                    subject: lesson.subject.clone(),
                    teacher: if title.is_empty() {
                        lesson.teacher.clone()
                    } else {
                        title.into()
                    },
                    classroom: lesson.classroom.clone(),
                });
            }
        }
    }
    let days = days
        .into_iter()
        .map(|(date, (label, mut lessons))| {
            lessons.sort_by(|a, b| {
                (a.number, &a.teacher, &a.subject, &a.classroom).cmp(&(
                    b.number,
                    &b.teacher,
                    &b.subject,
                    &b.classroom,
                ))
            });
            crate::DaySchedule {
                date_label: label,
                date_iso: date,
                lessons,
                week_variant: None,
                current_week_variant: None,
            }
        })
        .collect();
    crate::ScheduleSnapshot {
        group_name: teacher.into(),
        fetched_at: fetched_at.into(),
        days,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ambiguous_names_are_not_silently_selected() {
        let groups = crate::group_catalog::GroupCatalog::default();
        let catalog = SearchCatalog::new(
            &[
                ("Иванов Иван Иванович".into(), "/raspprep/1".into()),
                ("Иванов Игорь Иванович".into(), "/raspprep/2".into()),
            ],
            &[("101".into(), "/raspAud/3".into())],
        );
        assert_eq!(catalog.find_all("Иванов", &groups).len(), 2);
        assert_eq!(catalog.find_all("101", &groups)[0].kind, "audience");
        assert_eq!(normalize("ИВАНОВ И.И."), "иванов и. и.");
    }
}

#[cfg(test)]
mod cache_tests {
    use super::*;
    #[test]
    fn parsing_and_replacement_are_transactional() {
        let pairs = parse_college_pairs(
            "<a href='/raspprep/1'>Иванов</a><a href='/other'>x</a>",
            "http://site",
            "teacher",
        );
        assert_eq!(
            pairs,
            vec![("Иванов".into(), "http://site/raspprep/1".into())]
        );
        let mut db = rusqlite::Connection::open_in_memory().unwrap();
        db.execute_batch("CREATE TABLE search_targets(kind TEXT,title TEXT,url TEXT,updated_at TEXT); INSERT INTO search_targets VALUES('teacher','old','url','now'); CREATE TRIGGER fail BEFORE INSERT ON search_targets WHEN NEW.title='bad' BEGIN SELECT RAISE(ABORT,'bad'); END;").unwrap();
        assert!(save_targets(&mut db, "teacher", &[("bad".into(), "url".into())], "now").is_err());
        assert_eq!(load_targets(&db, "teacher").unwrap()[0].0, "old");
    }
}

#[cfg(test)]
mod fallback_tests {
    use super::*;
    #[test]
    fn finds_teacher_in_latest_group_snapshots_when_catalog_is_empty() {
        let day = crate::DaySchedule {
            date_label: String::new(),
            date_iso: "2026-11-09".into(),
            week_variant: None,
            current_week_variant: None,
            lessons: vec![
                crate::Lesson {
                    number: 1,
                    subject: "A".into(),
                    teacher: "Иванов И.И.".into(),
                    classroom: String::new(),
                },
                crate::Lesson {
                    number: 2,
                    subject: "B".into(),
                    teacher: "Иванов И. И.".into(),
                    classroom: String::new(),
                },
            ],
        };
        let snapshots = vec![crate::ScheduleSnapshot {
            group_name: "G".into(),
            fetched_at: "2026-11-08T12:00:00".into(),
            days: vec![day],
        }];
        let groups = crate::group_catalog::GroupCatalog::default();
        let catalog = SearchCatalog::default();
        let matches = catalog.find_all_with_snapshots("иванов", &groups, &snapshots);
        assert_eq!(matches.len(), 1);
        assert_eq!(matches[0].title, "Иванов И.И.");
        assert!(matches[0].url.is_empty());
    }
}

#[cfg(test)]
mod teacher_schedule_tests {
    use super::*;
    #[test]
    fn builds_group_labeled_schedule_and_respects_source() {
        let group = |title: &str, id: i64| {
            let snapshot = crate::ScheduleSnapshot {
                group_name: title.into(),
                fetched_at: "now".into(),
                days: vec![crate::DaySchedule {
                    date_label: "9 ноября".into(),
                    date_iso: "2026-11-09".into(),
                    week_variant: None,
                    current_week_variant: None,
                    lessons: vec![crate::Lesson {
                        number: 1,
                        subject: "Физика".into(),
                        teacher: "Иванов И.И.".into(),
                        classroom: "101".into(),
                    }],
                }],
            };
            crate::database::StoredSnapshot {
                source_type: Some("group".into()),
                source_key: None,
                source_title: None,
                source_url: None,
                group_name: Some(title.into()),
                schedule_id: Some(id),
                hash: String::new(),
                content: snapshot,
                fetched_at: "now".into(),
                created_at: String::new(),
            }
        };
        let groups = vec![group("Группа Б", -2), group("Группа А", 1)];
        let all = teacher_snapshot("Иванов", &groups, false, "now");
        assert_eq!(
            all.days[0]
                .lessons
                .iter()
                .map(|l| l.teacher.as_str())
                .collect::<Vec<_>>(),
            vec!["Группа А", "Группа Б"]
        );
        assert_eq!(
            teacher_snapshot("Иванов", &groups, true, "now").days[0]
                .lessons
                .len(),
            1
        );
    }
}
