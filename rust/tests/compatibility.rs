use misis_rasp_core::{parser, DaySchedule, Lesson, ScheduleSnapshot};

#[test]
fn snapshot_hash_matches_python_for_persisted_schedule() {
    let snapshot = ScheduleSnapshot {
        group_name: "ИСП-25".into(),
        fetched_at: "2026-11-08T12:00:00".into(),
        days: vec![DaySchedule {
            date_label: String::new(),
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
    assert_eq!(
        parser::compute_snapshot_hash(&snapshot),
        "30ba8d004400f6d8b31ca21b5167ba638dcd97d455bed0de92f33b9e29eb6050"
    );
}

#[tokio::test]
async fn http_retry_then_parses_college_page() {
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::time::Duration;
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let thread = std::thread::spawn(move || {
        for status in ["503 Service Unavailable", "200 OK"] {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 2048];
            let _ = stream.read(&mut request).unwrap();
            let body = if status.starts_with("200") {
                "<div id='titleF'>Group</div>"
            } else {
                "temporary"
            };
            write!(
                stream,
                "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
        }
    });
    let parser = parser::ScheduleParser::new(
        &format!("http://{address}/rasp"),
        "",
        Duration::from_secs(5),
        2,
        Duration::ZERO,
    )
    .unwrap();
    let reference = chrono::NaiveDate::from_ymd_opt(2026, 11, 8)
        .unwrap()
        .and_hms_opt(12, 0, 0)
        .unwrap();
    let (snapshot, _) = parser.parse(1, reference).await.unwrap();
    assert_eq!(snapshot.group_name, "Group");
    thread.join().unwrap();
}
