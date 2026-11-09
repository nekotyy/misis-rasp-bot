use chrono::{DateTime, Datelike, Duration, NaiveDate, Utc, Weekday};
use chrono_tz::Tz;

pub fn today_in_timezone(timezone: Tz, now: DateTime<Utc>) -> NaiveDate {
    now.with_timezone(&timezone).date_naive()
}
pub fn subtract_calendar_months(day: NaiveDate, months: u32) -> Option<NaiveDate> {
    day.checked_sub_months(chrono::Months::new(months))
}
pub fn schedule_dates(today: NaiveDate) -> [NaiveDate; 3] {
    [today, today + Duration::days(1), today + Duration::days(2)]
}
pub fn history_bounds(today: NaiveDate) -> Option<(NaiveDate, NaiveDate)> {
    Some((subtract_calendar_months(today, 3)?, today.pred_opt()?))
}
pub fn week_start(day: NaiveDate) -> NaiveDate {
    day - Duration::days(i64::from(day.weekday().num_days_from_monday()))
}
pub fn history_week_days(
    today: NaiveDate,
    monday: NaiveDate,
) -> Result<Vec<NaiveDate>, &'static str> {
    if monday.weekday() != Weekday::Mon {
        return Err("Начало недели должно приходиться на понедельник");
    }
    let first = history_bounds(today)
        .ok_or("Дата вне допустимого диапазона")?
        .0;
    if monday < week_start(first) {
        return Ok(vec![]);
    }
    Ok((0..6)
        .map(|offset| monday + Duration::days(offset))
        .collect())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn month_end_and_history_boundaries() {
        let day = NaiveDate::from_ymd_opt(2024, 3, 31).unwrap();
        assert_eq!(
            subtract_calendar_months(day, 1),
            NaiveDate::from_ymd_opt(2024, 2, 29)
        );
        assert_eq!(
            history_bounds(day).unwrap().1,
            NaiveDate::from_ymd_opt(2024, 3, 30).unwrap()
        );
        assert_eq!(
            week_start(day),
            NaiveDate::from_ymd_opt(2024, 3, 25).unwrap()
        );
        assert_eq!(history_week_days(day, week_start(day)).unwrap().len(), 6);
        assert!(history_week_days(day, day).is_err());
    }
    #[test]
    fn timezone_crosses_day_boundary() {
        let instant = DateTime::parse_from_rfc3339("2024-03-31T22:00:00Z")
            .unwrap()
            .to_utc();
        assert_eq!(
            today_in_timezone(chrono_tz::Europe::Moscow, instant),
            NaiveDate::from_ymd_opt(2024, 4, 1).unwrap()
        );
    }
}
