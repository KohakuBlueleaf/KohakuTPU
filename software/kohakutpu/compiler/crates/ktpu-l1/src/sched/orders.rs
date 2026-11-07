//! Successful fresh-core run orders shared within one mutation scope.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use crate::error::Result;

type Key = ([u8; 32], u32, u32, i64, String);
type Cell = Arc<Mutex<Option<Vec<usize>>>>;

/// Exact run orders for a fixed mutation scope. Machine, register-file size,
/// read copies and configuration price are part of each key. Only successful
/// orders are retained; a refusal is evaluated with the caller's source reader.
#[derive(Debug, Default)]
pub struct Orders {
    runs: Mutex<HashMap<Key, Cell>>,
}

impl Orders {
    pub(super) fn get(
        &self,
        context: ([u8; 32], u32, u32),
        price: i64,
        text: String,
        make: impl FnOnce() -> Result<Vec<usize>>,
    ) -> Result<Vec<usize>> {
        let key = (context.0, context.1, context.2, price, text);
        let cell = self.runs.lock().unwrap().entry(key).or_default().clone();
        let mut order = cell.lock().unwrap();
        if let Some(order) = order.as_ref() {
            return Ok(order.clone());
        }
        let made = make()?;
        *order = Some(made.clone());
        Ok(made)
    }
}
