//! `compiler/tests/test_schedule.py`: the image scheduler keeps an L1 word's
//! pack before its read, keeps an order the timing does not improve, and its
//! output still encodes.

use super::*;
use ktpu_l1::sched::schedule_module;
use ktpu_l1::text::Module;

pub const CHECKS: &[(&str, Check)] = &[
    (
        "test_schedule::test_an_l1_word_is_not_read_before_its_pack",
        an_l1_word_is_not_read_before_its_pack,
    ),
    (
        "test_schedule::test_a_reorder_the_timing_does_not_gain_keeps_the_written_order",
        a_reorder_with_no_gain_keeps_the_written_order,
    ),
    (
        "test_schedule::test_a_scheduled_image_still_encodes",
        a_scheduled_image_still_encodes,
    ),
];

const CHAIN: &str = "image k()\n    vunpk v0 <- l1[0] f16\n    vexp2 v1, v0\n    vexp2 v2, v1\n    vexp2 v3, v2\n    vunpk v8 <- l1[8] f16\n    vmul v9, v8, v8\n    vmul v10, v8, v8\n    vsync pack on=drain\n    vpack v9 -> l1[24] f16\n    vpack v10 -> l1[32] f16\n    vpack v3 -> l1[16] f16\n    halt\n";

fn module(text: &str) -> Result<Module, String> {
    read(&format!("target ktpu.v9\n{text}"), "<t>").map_err(|e| e.to_string())
}

/// Image `k`'s ops after the scheduler.
fn ops(text: &str) -> Result<Vec<String>, String> {
    let m = schedule_module(&module(text)?, &machine()).map_err(|e| e.to_string())?;
    Ok(m.images["k"].stmts.iter().map(|s| s.op.clone()).collect())
}

fn at(ops: &[String], op: &str) -> Result<usize, String> {
    ops.iter()
        .position(|o| o == op)
        .ok_or(format!("no {op} in {ops:?}"))
}

/// A pack's L1 word is not read before the pack, whatever latency a
/// reorder would hide.
fn an_l1_word_is_not_read_before_its_pack() -> Result<(), String> {
    // Python's text, then a read inside the pack's span (l1[4]) that a pack
    // at the end of one chain holds back from starting another: hoisting it
    // would be a gain.
    let texts = [
        "image k()\n    vpack v1 -> l1[0] f16\n    vexp2 v5, v4\n    vexp2 v6, v5\n    vunpk v2 <- l1[0] f16\n    halt\n",
        "image k()\n    vexp2 v1, v0\n    vexp2 v2, v1\n    vexp2 v3, v2\n    vpack v3 -> l1[0] f16\n    vunpk v4 <- l1[4] f16\n    vexp2 v5, v4\n    vexp2 v6, v5\n    vexp2 v7, v6\n    halt\n",
    ];
    for text in texts {
        let got = ops(text)?;
        if at(&got, "vpack")? > at(&got, "vunpk")? {
            return Err(format!("{got:?}"));
        }
    }
    Ok(())
}

/// When the chain's own pack ends the image either way, the written order
/// stays.
fn a_reorder_with_no_gain_keeps_the_written_order() -> Result<(), String> {
    let text = CHAIN.replace(
        "    vpack v9 -> l1[24] f16\n    vpack v10 -> l1[32] f16\n    vpack v3 -> l1[16] f16\n",
        "    vpack v3 -> l1[16] f16\n    vpack v9 -> l1[24] f16\n",
    );
    let written: Vec<String> = module(&text)?.images["k"]
        .stmts
        .iter()
        .map(|s| s.op.clone())
        .collect();
    eq(ops(&text)?, written)
}

/// The scheduler's module encodes, to the words of the ruled schedule
/// (`golden/sched/l1/vec-l1-sched-L1.SCHED.005.image`, RUL.063).
fn a_scheduled_image_still_encodes() -> Result<(), String> {
    let m = machine();
    let s = schedule_module(&module(CHAIN)?, &m).map_err(|e| e.to_string())?;
    let (words, _) = img::image(&s, &m, "k", &[], UNKNOWN).map_err(|e| e.to_string())?;
    let path =
        common::repo().join("software/rust/golden/sched/l1/vec-l1-sched-L1.SCHED.005.image.json");
    let g: Value = serde_json::from_str(&std::fs::read_to_string(path).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    let ruled =
        read(g["scheduled_ruled"].as_str().unwrap_or(""), "<golden>").map_err(|e| e.to_string())?;
    let (want, _) = img::image(&ruled, &m, "k", &[], UNKNOWN).map_err(|e| e.to_string())?;
    match words.is_empty() {
        true => Err("no words".into()),
        false => eq(core_words(&words), core_words(&want)),
    }
}
