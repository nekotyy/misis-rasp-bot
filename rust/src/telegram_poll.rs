use serde_json::{json, Value};

pub struct TelegramLongPoll {
    next_offset: Option<i64>,
    timeout: u64,
}
impl TelegramLongPoll {
    pub fn new(timeout: u64) -> Self {
        Self {
            next_offset: None,
            timeout,
        }
    }
    pub async fn fetch(
        &self,
        api: &crate::notification::ApiTransport,
    ) -> Result<Vec<Value>, crate::notification::DeliveryFailure> {
        let mut args = json!({"timeout":self.timeout});
        if let Some(offset) = self.next_offset {
            args["offset"] = json!(offset);
        }
        let response = api.send("telegram", "getUpdates", &args).await?;
        response
            .as_array()
            .cloned()
            .ok_or(crate::notification::DeliveryFailure::InvalidResponse)
    }
    /// Вызывать последовательно после полной обработки события и сохранения ответов.
    pub fn acknowledge(&mut self, update: &Value) -> bool {
        let Some(id) = update.get("update_id").and_then(Value::as_i64) else {
            return false;
        };
        let Some(next) = id.checked_add(1) else {
            return false;
        };
        self.next_offset = Some(self.next_offset.map_or(next, |old| old.max(next)));
        true
    }
    pub fn next_offset(&self) -> Option<i64> {
        self.next_offset
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn offset_moves_only_after_acknowledgement() {
        let mut poll = TelegramLongPoll::new(25);
        assert_eq!(poll.next_offset(), None);
        assert!(!poll.acknowledge(&json!({"message":{}})));
        assert_eq!(poll.next_offset(), None);
        assert!(poll.acknowledge(&json!({"update_id":42})));
        assert_eq!(poll.next_offset(), Some(43));
        poll.acknowledge(&json!({"update_id":40}));
        assert_eq!(poll.next_offset(), Some(43));
    }
}
