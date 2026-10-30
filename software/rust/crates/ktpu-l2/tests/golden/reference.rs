//! The scheduled L1 an e2e golden case (`golden/e2e/<case>.json`) is held
//! to: the ruled lowering (`ruled.l1`, RUL.099) where the case carries one,
//! else the RUL.063 schedule (`golden/sched/e2e/<case>.json`) where the
//! ruling changes Python's, else Python's `l1`. Shared by ktpu-l2's
//! `e2e_l1` and ktpu's stage test.

use serde_json::Value;
use std::path::Path;

/// Where a case's reference L1 comes from.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Source {
    Lowered,
    Scheduled,
    Python,
}

/// The RUL.063 schedule of a case whose Python schedule the ruling changes.
pub fn ruled_schedule(case: &Value, golden: &Path) -> Option<String> {
    let path = golden
        .join("sched/e2e")
        .join(format!("{}.json", case["case"].as_str()?));
    let s: Value = serde_json::from_str(&std::fs::read_to_string(path).ok()?).ok()?;
    let ruled = s["scheduled_ruled"].as_str()?;
    let tagged = s["rulings"].as_array()?.iter().any(|r| r == "RUL.063");
    (tagged && Some(ruled) != s["scheduled"].as_str()).then(|| ruled.to_string())
}

/// The case's reference L1 and its source.
pub fn reference_l1(case: &Value, golden: &Path) -> Option<(String, Source)> {
    if let Some(l1) = case["ruled"]["l1"].as_str() {
        return Some((l1.to_string(), Source::Lowered));
    }
    if let Some(l1) = ruled_schedule(case, golden) {
        return Some((l1, Source::Scheduled));
    }
    case["l1"]
        .as_str()
        .map(|l1| (l1.to_string(), Source::Python))
}
