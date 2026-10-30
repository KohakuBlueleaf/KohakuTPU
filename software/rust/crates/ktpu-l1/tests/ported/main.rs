//! The Python compiler's L1 tests (`software/compiler/tests`, `language/tests`)
//! whose property no other Rust test witnessed, ported for the Python
//! compiler's deletion. Each module lists its checks
//! (`CHECKS`); every check names the Python test it ports, and with the
//! `mutants` feature every check is shown to fail under its mutant
//! (`KILLERS`).

#[path = "../common/mod.rs"]
mod common;

mod image;
mod layout;
mod node;
mod sched;
mod text;

use std::collections::BTreeMap;

use ktpu_l1::build;
use ktpu_l1::lower::image::{self as img, UNKNOWN};
use ktpu_l1::lower::node::NodeProgram;
use ktpu_l1::text::read;
use ktpu_l1::Error;
use ktpu_machine::{Machine, UnitKind};
use serde_json::{json, Value};

pub use common::{core_words, machine, mover_writes, prog_steps};

/// One ported property: `Ok` when it holds, else what differs.
pub type Check = fn() -> Result<(), String>;

/// `got == want`, or both shown.
pub fn eq<T: PartialEq + std::fmt::Debug>(got: T, want: T) -> Result<(), String> {
    match got == want {
        true => Ok(()),
        false => Err(format!("got {got:?}\nwant {want:?}")),
    }
}

/// The refusal's clause, or why there is none.
pub fn clause<T: std::fmt::Debug>(r: Result<T, Error>) -> Result<String, String> {
    match r {
        Ok(v) => Err(format!("expected a refusal, got {v:?}")),
        Err(e) => Ok(e.clause),
    }
}

/// v9t6 with a second unpack / pack queue (`vc.dualq` 1).
pub fn dualq_machine() -> Machine {
    machine().with_fact("vc.dualq", json!(1)).expect("vc.dualq")
}

/// Image `k` of `target ktpu.v9` + `text` on `m`, as the spec runner prints
/// its words.
pub fn img_on(m: &Machine, text: &str) -> Result<Vec<String>, Error> {
    let module = read(&format!("target ktpu.v9\n{text}"), "<t>")?;
    let (words, _) = img::image(&module, m, "k", &[], UNKNOWN)?;
    Ok(core_words(&words))
}

pub fn img(text: &str) -> Result<Vec<String>, Error> {
    img_on(&machine(), text)
}

/// `fn f.l1(a: f16[4096] @dram)` with `body` on `m`'s card of `nodes`.
pub fn progs_on(
    m: &Machine,
    text: &str,
    binds: &[(&str, i128)],
    nodes: Option<u32>,
) -> Result<Vec<NodeProgram>, Error> {
    let module = read(text, "<t>")?;
    let b: BTreeMap<String, i128> = binds.iter().map(|(k, v)| (k.to_string(), *v)).collect();
    build::programs(&module, "f", &b, m, nodes, None)
}

pub fn kind(m: &Machine) -> impl Fn((u32, u32)) -> String + '_ {
    move |c| {
        for k in [UnitKind::Vc, UnitKind::Mg, UnitKind::Sr] {
            if m.units(k).iter().any(|u| (u.0, u.1) == c) {
                return k.tag().to_string();
            }
        }
        "?".into()
    }
}

pub const BODY: &str = "target ktpu.v9\nfn f.l1(a: f16[4096] @dram)\n";

/// The node program's steps (the spec runner's JSON) of `body`.
pub fn steps(body: &str) -> Result<Value, Error> {
    let m = machine();
    let got = progs_on(&m, &format!("{BODY}{body}"), &[("a", 4096)], None)?;
    let out = prog_steps(&kind(&m), &got[0].program);
    Ok(out)
}

pub fn steps_err(body: &str) -> Result<String, String> {
    clause(progs_on(
        &machine(),
        &format!("{BODY}{body}"),
        &[("a", 4096)],
        None,
    ))
}

/// The words of the first step of `send mg[0]` + `body`.
pub fn mg(body: &str) -> Result<Value, Error> {
    Ok(steps(&format!("    send mg[0]\n{body}"))?[0][2].clone())
}

/// The first step of `move` + `body`.
pub fn mv(body: &str) -> Result<Value, Error> {
    Ok(steps(&format!("    move\n{body}"))?[0].clone())
}

/// Every ported check, by its Python test.
fn checks() -> Vec<(&'static str, Check)> {
    [
        image::CHECKS,
        node::CHECKS,
        layout::CHECKS,
        text::CHECKS,
        sched::CHECKS,
    ]
    .concat()
}

fn failing(list: &[(&'static str, Check)]) -> Vec<String> {
    list.iter()
        .filter_map(|(name, c)| {
            let got = std::panic::catch_unwind(c).unwrap_or_else(|_| Err("panicked".into()));
            got.err().map(|e| format!("{name}: {e}"))
        })
        .collect()
}

#[test]
fn every_ported_check_holds() {
    let bad = failing(&checks());
    assert!(
        bad.is_empty(),
        "{} failing:\n{}",
        bad.len(),
        bad.join("\n\n")
    );
}

/// Each check's mutant: one that breaks the property the check asserts, not
/// only the text it reads.
#[cfg(feature = "mutants")]
const KILLERS: &[(&str, &str)] = &[
    (
        "test_emit_image::test_unknown_entry_sets_the_loop_state_once",
        "l1.img/no-hoist",
    ),
    (
        "skipz::a_long_skip_runs_as_consecutive_skips",
        "l1.img/skipz-whole",
    ),
    (
        "test_emit_image::test_operand_fields_follow_the_op",
        "l1.img/reads",
    ),
    (
        "test_emit_image::test_config_words_only_on_change",
        "l1.img/no-tracking",
    ),
    (
        "test_emit_image::test_free_fields_keep_the_state",
        "l1.img/reads",
    ),
    (
        "test_emit_image::test_crossbar_on_c_reads_b",
        "l1.img/xmodes",
    ),
    ("test_emit_image::test_walks_dma_and_sync", "l1.img/waiters"),
    ("test_emit_image::test_errors_name_the_line", "l1.img/reads"),
    (
        "test_vregs::test_target_line_reads_and_writes_vregs",
        "l1.module/target-name",
    ),
    (
        "test_vregs::test_image_encodes_for_the_target_core",
        "l1.img/regs32",
    ),
    (
        "test_vregs::test_resident_imem_rebase_keeps_the_word_past_bit_32",
        "l1.img/regs32",
    ),
    (
        "test_flow::test_a_fused_drain_must_name_its_emits_sink",
        "l1.mg/sink-unchecked",
    ),
    ("test_flow::test_a_count_is_not_marked", "l1.img/vwait-any"),
    (
        "test_l1_coverage::test_an_image_drain_names_only_this_mesh",
        "l1.img/drain-any-mesh",
    ),
    (
        "test_l1_coverage::test_a_mover_count_from_a_register_is_patched",
        "l1.node/reg-off-by-one",
    ),
    (
        "test_emit_node::test_marks_waits_moves_fetch (plain mover forms)",
        "l1.text/size-decimal",
    ),
    (
        "test_l1_coverage::test_fill_peers_and_gemm_anchor (two peers)",
        "l1.mg/peer-bytes",
    ),
    (
        "test_emit_node::test_cluster_forms (drain ack tuple)",
        "l1.mg/ack-swapped",
    ),
    (
        "test_flow::test_cluster_recv_fill_credit_drain_post (recv bank, credit)",
        "l1.mg/fill-no-recv",
    ),
    (
        "test_l1_coverage::test_explicit_config_lines_and_what_the_encoder_then_skips",
        "l1.img/xmodes",
    ),
    (
        "test_l1_coverage::test_images_and_descriptors_name_units_on_the_target (named ack)",
        "l1.unit/unsorted",
    ),
    (
        "test_emit_node::test_errors / test_fused_drain (missing cases)",
        "l1.mov/plain-unchecked",
    ),
    (
        "test_l1_coverage::test_register_count_in_a_fill",
        "l1.node/reg-off-by-one",
    ),
    (
        "test_layout::test_pack_places_every_slot_and_scale",
        "l1.layout/slot-order",
    ),
    (
        "test_layout::test_pack_refuses_a_partial_tile",
        "l1.layout/partial-tile",
    ),
    (
        "test_layout::test_a_drained_tile_reads_sub_tile_by_sub_tile",
        "l1.layout/subtile-transposed",
    ),
    (
        "test_layout::test_a_tiled_buffer_reads_back_what_it_wrote",
        "l1.layout/subtile-transposed",
    ),
    (
        "test_layout::test_a_batch_buffer_is_its_slabs_one_after_another",
        "l1.body/nbytes",
    ),
    (
        "test_syntax::test_a_term_parses_to_its_node_and_prints_back",
        "l1.text/size-decimal",
    ),
    (
        "test_syntax::test_printed_arithmetic_keeps_its_value",
        "l1.text/no-right-parens",
    ),
    (
        "test_syntax::test_a_statement_carries_target_args_annotation_block_and_position",
        "l1.text/no-annotation",
    ),
    (
        "test_syntax::test_an_error_names_its_line_and_column",
        "l1.text/col-off",
    ),
    (
        "test_schedule::test_an_l1_word_is_not_read_before_its_pack",
        "l1.sched/l1-words",
    ),
    (
        "test_schedule::test_a_reorder_the_timing_does_not_gain_keeps_the_written_order",
        "l1.sched/sync-movable",
    ),
    (
        "test_schedule::test_a_scheduled_image_still_encodes",
        "l1.sched/timing-flat",
    ),
];

/// Each check fails under its mutant (`KILLERS`), so none of them passes on
/// a compiler broken where it looks.
#[cfg(feature = "mutants")]
#[test]
fn every_ported_check_fails_under_its_mutant() {
    let all = checks();
    let mut bad = Vec::new();
    for (name, c) in &all {
        let Some((_, m)) = KILLERS.iter().find(|(n, _)| n == name) else {
            bad.push(format!("{name}: no mutant named"));
            continue;
        };
        let m = ktpu_l1::mutant::NAMES.iter().find(|x| *x == m).expect(m);
        let holds = ktpu_l1::mutant::with(m, || {
            std::panic::catch_unwind(c)
                .map(|r| r.is_ok())
                .unwrap_or(false)
        });
        if holds {
            let others: Vec<&str> = ktpu_l1::mutant::NAMES
                .iter()
                .copied()
                .filter(|o| {
                    !ktpu_l1::mutant::with(o, || {
                        std::panic::catch_unwind(c)
                            .map(|r| r.is_ok())
                            .unwrap_or(false)
                    })
                })
                .collect();
            bad.push(format!("{name}: holds under {m}; killed by {others:?}"));
        }
    }
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}
