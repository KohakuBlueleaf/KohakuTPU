//! Complete local-stage prices, including image residency and memory service.

use super::*;
use crate::lower::sync::Stage;

pub(super) fn price(stage: &Stage, images: &[String], t: &Target) -> Result<i64> {
    let mut text = String::from("target ktpu.v9\nfn choice.l1()\n");
    for line in &stage.lines {
        text.push_str(&format!("    {line}\n"));
    }
    text.push_str(&images.join("\n\n"));
    let mut module = ktpu_l1::text::read(&text, "<local stage choice>")?;
    module.vregs = t.vregs;
    module.vcopies = t.vcopies;
    let scheduled = ktpu_l1::sched::schedule_module(&module, &t.machine)?;
    crate::cost::cycles(&scheduled, "choice", t)
}

/// Retain the first schedule on a price tie. Only its images enter the module.
pub(super) fn select(
    candidates: Vec<(Stage, Vec<String>)>,
    images: &mut Vec<String>,
    t: &Target,
) -> Result<Stage> {
    let mut best = None;
    for (stage, code) in candidates {
        let cost = price(&stage, &code, t)?;
        if best.as_ref().is_none_or(|(price, _, _)| cost < *price) {
            best = Some((cost, stage, code));
        }
    }
    let (_, stage, code) = best.expect("a stage choice has its original schedule");
    images.extend(code);
    Ok(stage)
}
