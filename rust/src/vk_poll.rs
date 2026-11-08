use serde_json::Value;
use std::collections::{HashSet, VecDeque};

pub fn peer_id(update: &Value) -> Option<i64> {
    let object = update.get("object")?.as_object()?;
    let message = object
        .get("message")
        .filter(|v| v.is_object())
        .unwrap_or(update.get("object")?);
    message.get("peer_id")?.as_i64()
}
pub fn dedup_key(update: &Value) -> Option<String> {
    if let Some(event) = update
        .get("event_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return Some(format!("e:{event}"));
    }
    let message = update.pointer("/object/message")?.as_object()?;
    let peer = message.get("peer_id")?;
    let id = message.get("conversation_message_id")?;
    if peer.is_null() || id.is_null() {
        return None;
    }
    Some(format!("m:{peer}:{id}"))
}
pub struct Deduplicator {
    limit: usize,
    order: VecDeque<String>,
    seen: HashSet<String>,
}
impl Deduplicator {
    pub fn new(limit: usize) -> Self {
        Self {
            limit,
            order: VecDeque::new(),
            seen: HashSet::new(),
        }
    }
    pub fn is_duplicate(&mut self, update: &Value) -> bool {
        let Some(key) = dedup_key(update) else {
            return false;
        };
        if self.seen.contains(&key) {
            return true;
        }
        self.seen.insert(key.clone());
        self.order.push_back(key);
        while self.order.len() > self.limit {
            if let Some(key) = self.order.pop_front() {
                self.seen.remove(&key);
            }
        }
        false
    }
}
struct Batch {
    ts: String,
    pending: HashSet<u64>,
    cancelled: bool,
}
#[derive(Default)]
pub struct CheckpointTracker {
    batches: VecDeque<Batch>,
}
impl CheckpointTracker {
    pub fn add(&mut self, ts: String, tasks: impl IntoIterator<Item = u64>) {
        self.batches.push_back(Batch {
            ts,
            pending: tasks.into_iter().collect(),
            cancelled: false,
        });
    }
    pub fn finish(&mut self, task: u64, cancelled: bool) -> Option<String> {
        for batch in &mut self.batches {
            if batch.pending.remove(&task) && cancelled {
                batch.cancelled = true;
            }
        }
        self.advance()
    }
    pub fn advance(&mut self) -> Option<String> {
        let mut latest = None;
        while self
            .batches
            .front()
            .is_some_and(|b| b.pending.is_empty() && !b.cancelled)
        {
            latest = self.batches.pop_front().map(|b| b.ts);
        }
        latest
    }
}

#[derive(serde::Serialize, serde::Deserialize)]
struct Checkpoint {
    group_id: i64,
    ts: Value,
}
pub fn load_checkpoint(path: &std::path::Path, group_id: i64) -> std::io::Result<Option<String>> {
    let data = match std::fs::read(path) {
        Ok(data) => data,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let checkpoint: Checkpoint = match serde_json::from_slice(&data) {
        Ok(value) => value,
        Err(_) => return Ok(None),
    };
    if checkpoint.group_id != group_id {
        return Ok(None);
    }
    let ts = match checkpoint.ts {
        Value::String(s) => s,
        Value::Number(n) if n.is_i64() || n.is_u64() => n.to_string(),
        _ => return Ok(None),
    };
    if ts.is_empty() || !ts.bytes().all(|c| c.is_ascii_digit()) {
        return Ok(None);
    }
    Ok(Some(ts))
}
pub fn save_checkpoint(path: &std::path::Path, group_id: i64, ts: &str) -> std::io::Result<()> {
    if ts.is_empty() || !ts.bytes().all(|c| c.is_ascii_digit()) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "Некорректная позиция VK",
        ));
    }
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| std::path::Path::new("."));
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    serde_json::to_writer(
        &mut file,
        &Checkpoint {
            group_id,
            ts: Value::String(ts.into()),
        },
    )?;
    file.persist(path).map_err(|e| e.error)?;
    Ok(())
}

pub enum PollResult {
    Batch { ts: String, updates: Vec<Value> },
    Refreshed,
    HistoryLost,
}
pub struct VkLongPoll {
    pub group_id: i64,
    pub last_ts: Option<String>,
    server: Option<String>,
    key: Option<String>,
    client: reqwest::Client,
    wait: u64,
    checkpoint_path: Option<std::path::PathBuf>,
}
impl VkLongPoll {
    pub fn new(
        group_id: i64,
        wait: u64,
        checkpoint: Option<String>,
    ) -> Result<Self, reqwest::Error> {
        Ok(Self {
            group_id,
            last_ts: checkpoint,
            server: None,
            key: None,
            client: reqwest::Client::builder()
                .connect_timeout(std::time::Duration::from_secs(15))
                .timeout(std::time::Duration::from_secs(wait.saturating_add(20)))
                .build()?,
            wait,
            checkpoint_path: None,
        })
    }
    pub fn with_checkpoint(
        group_id: i64,
        wait: u64,
        path: std::path::PathBuf,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let checkpoint = load_checkpoint(&path, group_id)?;
        let mut result = Self::new(group_id, wait, checkpoint)?;
        result.checkpoint_path = Some(path);
        Ok(result)
    }
    pub fn acknowledge(&self, ts: &str) -> std::io::Result<()> {
        if let Some(path) = &self.checkpoint_path {
            save_checkpoint(path, self.group_id, ts)?;
        }
        Ok(())
    }
    async fn refresh(
        &mut self,
        api: &crate::notification::ApiTransport,
        keep_ts: bool,
    ) -> Result<(), crate::notification::DeliveryFailure> {
        let result = api
            .send(
                "vk",
                "groups.getLongPollServer",
                &serde_json::json!({"group_id":self.group_id}),
            )
            .await?;
        let server = result
            .get("server")
            .and_then(Value::as_str)
            .ok_or(crate::notification::DeliveryFailure::InvalidResponse)?
            .to_string();
        let key = result
            .get("key")
            .and_then(Value::as_str)
            .ok_or(crate::notification::DeliveryFailure::InvalidResponse)?
            .to_string();
        let ts = result
            .get("ts")
            .and_then(value_text)
            .ok_or(crate::notification::DeliveryFailure::InvalidResponse)?;
        self.server = Some(server);
        self.key = Some(key);
        let first = self.last_ts.is_none();
        if !keep_ts || first {
            self.last_ts = Some(ts);
        }
        if first {
            if let Some(ts) = &self.last_ts {
                let _ = self.acknowledge(ts);
            }
        }
        Ok(())
    }
    pub async fn poll_once(
        &mut self,
        api: &crate::notification::ApiTransport,
    ) -> Result<PollResult, crate::notification::DeliveryFailure> {
        if self.server.is_none() {
            self.refresh(api, true).await?;
        }
        let server = self
            .server
            .as_deref()
            .ok_or(crate::notification::DeliveryFailure::InvalidResponse)?;
        let key = self
            .key
            .as_deref()
            .ok_or(crate::notification::DeliveryFailure::InvalidResponse)?;
        let ts = self
            .last_ts
            .as_deref()
            .ok_or(crate::notification::DeliveryFailure::InvalidResponse)?;
        let fetched = async {
            let response = self
                .client
                .post(server)
                .query(&[
                    ("act", "a_check"),
                    ("key", key),
                    ("ts", ts),
                    ("wait", &self.wait.to_string()),
                ])
                .send()
                .await
                .map_err(|_| crate::notification::DeliveryFailure::Network)?;
            response
                .json::<Value>()
                .await
                .map_err(|_| crate::notification::DeliveryFailure::InvalidResponse)
        }
        .await;
        let event = match fetched {
            Ok(event) => event,
            Err(error) => {
                self.server = None;
                self.key = None;
                return Err(error);
            }
        };
        if let Some(failed) = event.get("failed").and_then(Value::as_i64) {
            return match failed {
                1 => {
                    self.last_ts = event
                        .get("ts")
                        .and_then(value_text)
                        .or_else(|| self.last_ts.clone());
                    Ok(PollResult::HistoryLost)
                }
                2 => {
                    self.refresh(api, true).await?;
                    Ok(PollResult::Refreshed)
                }
                3 => {
                    self.refresh(api, false).await?;
                    Ok(PollResult::HistoryLost)
                }
                _ => {
                    self.server = None;
                    self.key = None;
                    Err(crate::notification::DeliveryFailure::InvalidResponse)
                }
            };
        }
        let Some(ts) = event.get("ts").and_then(value_text) else {
            self.server = None;
            self.key = None;
            return Err(crate::notification::DeliveryFailure::InvalidResponse);
        };
        let updates = event
            .get("updates")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        self.last_ts = Some(ts.clone());
        Ok(PollResult::Batch { ts, updates })
    }
}
fn value_text(value: &Value) -> Option<String> {
    match value {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn identifies_and_bounds_duplicates() {
        let update = json!({"object":{"message":{"peer_id":123,"conversation_message_id":45}}});
        assert_eq!(peer_id(&update), Some(123));
        assert_eq!(dedup_key(&update).as_deref(), Some("m:123:45"));
        let mut seen = Deduplicator::new(1);
        assert!(!seen.is_duplicate(&update));
        assert!(seen.is_duplicate(&update));
        assert!(!seen.is_duplicate(&json!({"event_id":"next"})));
        assert!(!seen.is_duplicate(&update));
    }
    #[test]
    fn checkpoints_wait_for_prior_batches_and_cancelled_work() {
        let mut tracker = CheckpointTracker::default();
        tracker.add("10".into(), [1, 2]);
        tracker.add("11".into(), [3]);
        assert_eq!(tracker.finish(3, false), None);
        assert_eq!(tracker.finish(1, false), None);
        assert_eq!(tracker.finish(2, false).as_deref(), Some("11"));
        tracker.add("12".into(), [4]);
        tracker.add("13".into(), []);
        assert_eq!(tracker.finish(4, true), None);
        assert_eq!(tracker.advance(), None);
    }
}

#[cfg(test)]
mod checkpoint_tests {
    use super::*;
    #[test]
    fn checkpoint_is_scoped_to_community_and_replaces_previous_value() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("checkpoint.json");
        assert_eq!(load_checkpoint(&path, 1).unwrap(), None);
        save_checkpoint(&path, 1, "123").unwrap();
        assert_eq!(load_checkpoint(&path, 1).unwrap().as_deref(), Some("123"));
        assert_eq!(load_checkpoint(&path, 2).unwrap(), None);
        save_checkpoint(&path, 1, "124").unwrap();
        assert_eq!(load_checkpoint(&path, 1).unwrap().as_deref(), Some("124"));
        assert!(save_checkpoint(&path, 1, "invalid").is_err());
        assert_eq!(load_checkpoint(&path, 1).unwrap().as_deref(), Some("124"));
    }
}
