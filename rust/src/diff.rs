use crate::{subgroups::lesson_subgroups, Lesson};

#[derive(Debug, PartialEq, Eq)]
pub enum Status {
    Unchanged,
    Changed,
    Added,
    Ambiguous,
}
#[derive(Debug, PartialEq, Eq)]
pub struct LessonChange {
    pub lesson: Lesson,
    pub status: Status,
    pub before: Option<Lesson>,
    pub changed_fields: Vec<&'static str>,
}
#[derive(Debug, PartialEq, Eq)]
pub struct DayChanges {
    pub lessons: Vec<LessonChange>,
    pub missing: Vec<Lesson>,
}
fn could_share_slot(a: &Lesson, b: &Lesson) -> bool {
    if a.number != b.number {
        return false;
    }
    let x = lesson_subgroups(&a.subject);
    let y = lesson_subgroups(&b.subject);
    x.is_empty() || y.is_empty() || !x.is_disjoint(&y)
}
pub fn compare_lessons(before: Option<&[Lesson]>, after: &[Lesson]) -> DayChanges {
    let Some(before) = before else {
        return DayChanges {
            lessons: after
                .iter()
                .map(|lesson| LessonChange {
                    lesson: lesson.clone(),
                    status: Status::Unchanged,
                    before: None,
                    changed_fields: vec![],
                })
                .collect(),
            missing: vec![],
        };
    };
    let mut old_used = vec![false; before.len()];
    let mut changes: Vec<Option<LessonChange>> = (0..after.len()).map(|_| None).collect();
    let mut pending = vec![];
    for (i, lesson) in after.iter().enumerate() {
        if let Some(j) = before
            .iter()
            .enumerate()
            .position(|(j, old)| !old_used[j] && old == lesson)
        {
            old_used[j] = true;
            changes[i] = Some(LessonChange {
                lesson: lesson.clone(),
                status: Status::Unchanged,
                before: None,
                changed_fields: vec![],
            });
        } else {
            pending.push(i);
        }
    }
    let unmatched: Vec<usize> = (0..before.len()).filter(|&j| !old_used[j]).collect();
    let mut matched = vec![false; before.len()];
    for &i in &pending {
        let lesson = &after[i];
        let candidates: Vec<usize> = unmatched
            .iter()
            .copied()
            .filter(|&j| could_share_slot(&before[j], lesson))
            .collect();
        let unique = candidates.len() == 1
            && lesson_subgroups(&before[candidates[0]].subject)
                == lesson_subgroups(&lesson.subject)
            && pending
                .iter()
                .filter(|&&other| could_share_slot(&before[candidates[0]], &after[other]))
                .count()
                == 1;
        let change = if unique {
            let j = candidates[0];
            matched[j] = true;
            let old = &before[j];
            let mut fields = vec![];
            if old.subject != lesson.subject {
                fields.push("subject");
            }
            if old.teacher != lesson.teacher {
                fields.push("teacher");
            }
            if old.classroom != lesson.classroom {
                fields.push("classroom");
            }
            LessonChange {
                lesson: lesson.clone(),
                status: Status::Changed,
                before: Some(old.clone()),
                changed_fields: fields,
            }
        } else {
            LessonChange {
                lesson: lesson.clone(),
                status: if candidates.is_empty() {
                    Status::Added
                } else {
                    Status::Ambiguous
                },
                before: None,
                changed_fields: vec![],
            }
        };
        changes[i] = Some(change);
    }
    DayChanges {
        lessons: changes.into_iter().flatten().collect(),
        missing: unmatched
            .into_iter()
            .filter(|&j| !matched[j])
            .map(|j| before[j].clone())
            .collect(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn lesson(subject: &str, classroom: &str) -> Lesson {
        Lesson {
            number: 1,
            subject: subject.into(),
            teacher: "Иванов".into(),
            classroom: classroom.into(),
        }
    }
    #[test]
    fn baseline_duplicates_and_changes() {
        let a = lesson("Физика (1 подгруппа)", "301");
        let b = lesson("Химия (2 подгруппа)", "301");
        let changed = lesson("Химия (2 подгруппа)", "302");
        assert_eq!(
            compare_lessons(None, std::slice::from_ref(&a)).lessons[0].status,
            Status::Unchanged
        );
        assert_eq!(
            compare_lessons(Some(&[]), std::slice::from_ref(&a)).lessons[0].status,
            Status::Added
        );
        let result = compare_lessons(Some(&[a.clone(), b.clone()]), &[changed, a.clone()]);
        assert_eq!(result.lessons[0].before, Some(b));
        assert_eq!(result.lessons[0].changed_fields, vec!["classroom"]);
        assert!(result.missing.is_empty());
        let result = compare_lessons(Some(&[a.clone(), a.clone()]), &[a.clone(), a.clone(), a]);
        assert_eq!(
            result.lessons.iter().map(|x| &x.status).collect::<Vec<_>>(),
            vec![&Status::Unchanged, &Status::Unchanged, &Status::Added]
        );
    }
    #[test]
    fn ambiguity_does_not_cancel_missing_lessons() {
        let old = vec![lesson("Физика", "301"), lesson("Химия", "301")];
        let result = compare_lessons(Some(&old), &[lesson("История", "301")]);
        assert_eq!(result.lessons[0].status, Status::Ambiguous);
        assert_eq!(result.missing, old);
        let old = lesson("Физика", "301");
        assert_eq!(
            compare_lessons(Some(&[old]), &[lesson("Физика (1 подгруппа)", "301")]).lessons[0]
                .status,
            Status::Ambiguous
        );
    }
}
