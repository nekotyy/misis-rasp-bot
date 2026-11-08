use rusqlite::{params, Connection, TransactionBehavior};
use std::collections::HashMap;
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GroupInfo {
    pub department_id: i64,
    pub department_code: String,
    pub department_name: String,
    pub group_name: String,
    pub schedule_id: Option<i64>,
    pub url: String,
}
#[derive(Default)]
pub struct GroupCatalog {
    groups: Vec<GroupInfo>,
    names: HashMap<String, usize>,
    compact: HashMap<String, usize>,
    ids: HashMap<i64, usize>,
}
fn compact(value: &str) -> String {
    crate::subscription::normalize_group(value)
        .chars()
        .filter(|c| c.is_alphanumeric() || *c == '_')
        .collect()
}
impl GroupCatalog {
    pub fn new(groups: Vec<GroupInfo>) -> Self {
        let mut result = Self {
            groups,
            ..Default::default()
        };
        for (i, group) in result.groups.iter().enumerate() {
            result
                .names
                .insert(crate::subscription::normalize_group(&group.group_name), i);
            result.compact.insert(compact(&group.group_name), i);
            if let Some(id) = group.schedule_id {
                result.ids.insert(id, i);
            }
        }
        result
    }
    pub fn find(&self, name: &str) -> Option<&GroupInfo> {
        self.names
            .get(&crate::subscription::normalize_group(name))
            .or_else(|| self.compact.get(&compact(name)))
            .map(|i| &self.groups[*i])
    }
    pub fn by_id(&self, id: i64) -> Option<&GroupInfo> {
        self.ids.get(&id).map(|i| &self.groups[*i])
    }
    pub fn list(&self) -> Vec<&GroupInfo> {
        let mut groups: Vec<_> = self.names.values().map(|i| &self.groups[*i]).collect();
        groups.sort_by(|a, b| {
            (&a.department_code, &a.group_name).cmp(&(&b.department_code, &b.group_name))
        });
        groups
    }
}
pub fn load_groups(db: &Connection) -> rusqlite::Result<Vec<GroupInfo>> {
    let mut stmt=db.prepare("SELECT department_id,department_code,department_name,group_name,schedule_id,url FROM groups")?;
    let rows = stmt.query_map([], |r| {
        Ok(GroupInfo {
            department_id: r.get(0)?,
            department_code: r.get(1)?,
            department_name: r.get(2)?,
            group_name: r.get(3)?,
            schedule_id: r.get(4)?,
            url: r.get(5)?,
        })
    })?;
    rows.collect()
}
pub fn save_groups(db: &mut Connection, groups: &[GroupInfo], now: &str) -> rusqlite::Result<()> {
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    tx.execute("DELETE FROM groups", [])?;
    for group in groups {
        tx.execute("INSERT INTO groups(schedule_id,group_name,department_id,department_code,department_name,url,updated_at) VALUES (?,?,?,?,?,?,?)",params![group.schedule_id,group.group_name,group.department_id,group.department_code,group.department_name,group.url,now])?;
    }
    tx.commit()
}

fn selector(css: &str) -> scraper::Selector {
    scraper::Selector::parse(css).expect("static selector")
}
fn text(element: scraper::ElementRef<'_>) -> String {
    element
        .text()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}
fn numeric_suffix(href: &str) -> Option<i64> {
    let value = href.rsplit('/').next()?;
    if value.is_empty() || !value.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    value.parse().ok()
}
pub fn parse_departments(html: &str) -> Vec<(i64, String)> {
    let doc = scraper::Html::parse_document(html);
    let mut departments = Vec::new();
    for link in doc.select(&selector("a[href^='/group/']")) {
        let Some(href) = link.value().attr("href") else {
            continue;
        };
        if let Some(id) = numeric_suffix(href) {
            departments.push((id, text(link)));
        }
    }
    departments.sort();
    departments.dedup();
    departments
}
pub fn parse_department(
    html: &str,
    origin: &str,
    department_id: i64,
    code: &str,
) -> Vec<GroupInfo> {
    let doc = scraper::Html::parse_document(html);
    let department_name = doc
        .select(&selector("#titleS"))
        .next()
        .map(text)
        .unwrap_or_default();
    let mut groups = Vec::new();
    for link in doc.select(&selector("a[href^='/rasp/']")) {
        let Some(href) = link.value().attr("href") else {
            continue;
        };
        let Some(id) = numeric_suffix(href) else {
            continue;
        };
        let name = text(link);
        if name.is_empty() {
            continue;
        }
        groups.push(GroupInfo {
            department_id,
            department_code: code.into(),
            department_name: department_name.clone(),
            group_name: name,
            schedule_id: Some(id),
            url: format!("{}/rasp/{id}", origin.trim_end_matches('/')),
        });
    }
    groups
}
pub fn parse_university_groups(payload: &str, origin: &str) -> Result<Vec<GroupInfo>, String> {
    crate::sources::parse_university_directory(payload, "groups")?
        .into_iter()
        .map(|row| {
            let id = row["value"].as_i64().ok_or("Некорректный идентификатор")?;
            let faculty = row
                .get("faculty_id")
                .and_then(|v| {
                    v.as_i64()
                        .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
                })
                .unwrap_or(0);
            Ok(GroupInfo {
                department_id: -faculty,
                department_code: "Университет".into(),
                department_name: row
                    .get("faculty_name")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("")
                    .into(),
                group_name: row["content"].as_str().unwrap_or("").trim().into(),
                schedule_id: Some(-id),
                url: format!("{}/group/{id}", origin.trim_end_matches('/')),
            })
        })
        .collect()
}

pub struct CatalogRefresh {
    pub groups: Vec<GroupInfo>,
    pub failed_departments: Vec<(i64, String)>,
    pub college_failed: bool,
    pub university_failed: bool,
}
pub async fn fetch_catalog(
    parser: &crate::parser::ScheduleParser,
    college_origin: &str,
    university_origin: &str,
    cached: &[GroupInfo],
) -> Result<CatalogRefresh, String> {
    let mut groups: HashMap<i64, GroupInfo> = HashMap::new();
    let mut failed = Vec::new();
    let college = parser
        .fetch_html(&format!("{}/", college_origin.trim_end_matches('/')))
        .await;
    let college_failed = college.is_err();
    if college_failed && university_origin.is_empty() {
        if cached.is_empty() {
            return Err("Каталоги расписания недоступны".into());
        }
        return Ok(CatalogRefresh {
            groups: cached.to_vec(),
            failed_departments: Vec::new(),
            college_failed: true,
            university_failed: false,
        });
    }
    match college {
        Ok(html) => {
            for (id, code) in parse_departments(&html) {
                match parser
                    .fetch_html(&format!(
                        "{}/group/{id}",
                        college_origin.trim_end_matches('/')
                    ))
                    .await
                {
                    Ok(page) => {
                        for group in parse_department(&page, college_origin, id, &code) {
                            if let Some(id) = group.schedule_id {
                                groups.insert(id, group);
                            }
                        }
                    }
                    Err(_) => failed.push((id, code)),
                }
            }
        }
        Err(_) => {
            for group in cached {
                if group
                    .schedule_id
                    .is_some_and(|id| id > 0 || university_origin.is_empty())
                {
                    groups.insert(group.schedule_id.unwrap(), group.clone());
                }
            }
        }
    }
    let mut university_failed = false;
    if !university_origin.is_empty() {
        let result = match parser
            .fetch_html(&format!(
                "{}/json/groups",
                university_origin.trim_end_matches('/')
            ))
            .await
        {
            Ok(payload) => parse_university_groups(&payload, university_origin),
            Err(_) => Err("Не удалось загрузить каталог университета".into()),
        };
        match result {
            Ok(items) => {
                for group in items {
                    if let Some(id) = group.schedule_id {
                        groups.insert(id, group);
                    }
                }
            }
            Err(_) => {
                university_failed = true;
                for group in cached {
                    if group.schedule_id.is_some_and(|id| id < 0) {
                        groups.insert(group.schedule_id.unwrap(), group.clone());
                    }
                }
            }
        }
    }
    if college_failed && (university_origin.is_empty() || university_failed) && groups.is_empty() {
        return Err("Каталоги расписания недоступны".into());
    }
    let mut groups: Vec<_> = groups.into_values().collect();
    groups.sort_by_key(|g| g.schedule_id);
    Ok(CatalogRefresh {
        groups,
        failed_departments: failed,
        college_failed,
        university_failed,
    })
}

pub struct RetryResult {
    pub groups: Vec<GroupInfo>,
    pub remaining: Vec<(i64, String)>,
    pub recovered: bool,
}
pub async fn retry_departments(
    parser: &crate::parser::ScheduleParser,
    origin: &str,
    failed: &[(i64, String)],
    cached: &[GroupInfo],
) -> RetryResult {
    let mut groups: HashMap<i64, GroupInfo> = cached
        .iter()
        .filter_map(|g| g.schedule_id.map(|id| (id, g.clone())))
        .collect();
    let mut remaining = Vec::new();
    let mut recovered = false;
    for (id, code) in failed {
        match parser
            .fetch_html(&format!("{}/group/{id}", origin.trim_end_matches('/')))
            .await
        {
            Ok(html) => {
                for group in parse_department(&html, origin, *id, code) {
                    if let Some(id) = group.schedule_id {
                        groups.insert(id, group);
                    }
                }
                recovered = true;
            }
            Err(_) => remaining.push((*id, code.clone())),
        }
    }
    let mut groups: Vec<_> = groups.into_values().collect();
    groups.sort_by_key(|g| g.schedule_id);
    RetryResult {
        groups,
        remaining,
        recovered,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn group(name: &str, id: i64) -> GroupInfo {
        GroupInfo {
            department_id: 1,
            department_code: "A".into(),
            department_name: "Тест".into(),
            group_name: name.into(),
            schedule_id: Some(id),
            url: String::new(),
        }
    }
    #[test]
    fn lookup_and_transactional_replacement() {
        let mut db = Connection::open_in_memory().unwrap();
        db.execute_batch("CREATE TABLE groups(schedule_id INTEGER,group_name TEXT,department_id INTEGER,department_code TEXT,department_name TEXT,url TEXT,updated_at TEXT); INSERT INTO groups VALUES (1,'old',1,'A','Dept','','now'); CREATE TRIGGER reject BEFORE INSERT ON groups WHEN NEW.group_name='bad' BEGIN SELECT RAISE(ABORT,'bad'); END;").unwrap();
        assert!(save_groups(&mut db, &[group("ИСП-25", 2), group("bad", 3)], "now").is_err());
        assert_eq!(load_groups(&db).unwrap()[0].group_name, "old");
        db.execute_batch("DROP TRIGGER reject").unwrap();
        save_groups(&mut db, &[group("ИСП-25", 2)], "now").unwrap();
        let catalog = GroupCatalog::new(load_groups(&db).unwrap());
        assert_eq!(catalog.find("ИCП 25").unwrap().schedule_id, Some(2));
        assert_eq!(catalog.by_id(2).unwrap().group_name, "ИСП-25");
    }
}

#[cfg(test)]
mod parsing_tests {
    use super::*;
    #[test]
    fn parses_college_and_university_catalogs() {
        let departments=parse_departments("<a href='/group/2'>Б</a><a href='/group/1'>А</a><a href='/group/1'>А</a><a href='/group/x'>X</a>");
        assert_eq!(departments, vec![(1, "А".into()), (2, "Б".into())]);
        let groups=parse_department("<div id='titleS'>Отделение</div><a href='/rasp/600'>ИСП-25</a><a href='/rasp/x'>bad</a>","http://college",1,"ИСП");
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0].url, "http://college/rasp/600");
        let university=parse_university_groups(r#"var database = {"groups":[{"value":12,"content":" ТЭ-26 ","faculty_id":4,"faculty_name":"Факультет"}]};"#,"http://university").unwrap();
        assert_eq!(university[0].schedule_id, Some(-12));
        assert_eq!(university[0].department_id, -4);
    }
}
