//! L2.MOV.010: a deferred quant status, held pending until a quant_check.

use ktpu_l2::{compile_l1, Program, Search};
use ktpu_machine::Machine;

const ASSERT: &str = "assert_zero r14 code=1";

fn machine() -> Machine {
    Machine::named("multimesh_v9t6").unwrap()
}

/// Halves of `records` records of every quant core.
fn halves(records: i64) -> i64 {
    records * machine().quant_cores() * 64
}

/// Two quants of `x`'s halves into `q0`, `q1`, then `body`.
fn program(status: i64, body: &str) -> String {
    format!(
        "target ktpu.v9\n\nfn q.l2(x: f16[128, 64] @dram, q0: mx7a[64, 64] @dram(gm=16,nk=2), q1: mx7a[64, 64] @dram(gm=16,nk=2))\n    s = buffer : f16[{status}] @dram\n{body}"
    )
}

fn lower(text: &str) -> Result<String, ktpu_l2::Error> {
    let p = Program::parse(text, "<status>", None)?;
    compile_l1(&p, &machine(), &[], false, &Search::default()).map(|c| c.l1_text)
}

fn deferred() -> String {
    let one = halves(1);
    program(
        2 * one,
        &format!(
            "    quant q0 <- x[0 : +64, 0 : +64] status=s[0 : +{one}]\n    quant q1 <- x[64 : +64, 0 : +64] status=s[{one} : +{one}]\n    quant_check s\n"
        ),
    )
}

/// The lines from the last quant's drain on.
fn after_drains(text: &str) -> &str {
    let last = text.rfind("run q.mx7_status_").unwrap();
    &text[last..]
}

#[test]
fn a_deferred_status_is_checked_once_after_both_quants() {
    let cores = machine().quant_cores() as usize;
    let text = lower(&deferred()).unwrap();
    assert_eq!(text.matches(ASSERT).count(), 2 * cores);
    assert_eq!(after_drains(&text).matches(ASSERT).count(), 2 * cores);
    assert!(text.contains("(s + 0)") && text.contains(&format!("(s + {} + 0)", 128 * cores)));
    let inline = lower(&program(
        64,
        "    quant q0 <- x[0 : +64, 0 : +64]\n    quant q1 <- x[64 : +64, 0 : +64]\n",
    ))
    .unwrap();
    assert_eq!(inline.matches(ASSERT).count(), 2 * cores);
    assert_eq!(after_drains(&inline).matches(ASSERT).count(), cores);
}

#[test]
fn a_status_is_refused_where_its_records_could_go_unchecked() {
    let one = halves(1);
    let refused = |body: String, status: i64, why: &str| {
        let e = lower(&program(status, &body)).unwrap_err();
        assert!(e.message.contains(why), "{why}: {}", e.message);
    };
    let q0 = format!("    quant q0 <- x[0 : +64, 0 : +64] status=s[0 : +{one}]\n");
    refused(q0.clone(), one, "unchecked at the function's end");
    refused("    quant_check s\n".into(), one, "no pending quant status");
    refused(
        format!(
            "{q0}    quant q1 <- x[64 : +64, 0 : +64] status=s[0 : +{one}]\n    quant_check s\n"
        ),
        one,
        "pending its quant_check",
    );
    refused(
        format!("{q0}    fill s value=0\n    quant_check s\n"),
        one,
        "a write over a quant status",
    );
    refused(
        "    quant q0 <- x[0 : +64, 0 : +64] status=s[0 : +64]\n    quant_check s\n".to_string(),
        one,
        "pieces' records take",
    );
    refused(
        format!("    quant q0 <- x[0 : +64, 0 : +64] finite=1 status=s[0 : +{one}]\n    quant_check s\n"),
        one,
        "no finite=1",
    );
    refused(
        "    quant q0 <- x[0 : +64, 0 : +64] status=x[64 : +2, 0 : +64]\n    quant_check x[64 : +2, 0 : +64]\n"
            .to_string(),
        one,
        "no operand of its quant",
    );
}

/// A padded quant's status holds a record a core of each piece holding
/// source entries (`ktpu_machine::packing::quant_status_bytes`).
#[test]
fn a_padded_quant_takes_a_record_a_live_piece() {
    let one = halves(1);
    let quant = |records: i64| {
        lower(&program(
            records * one,
            &format!(
                "    quant q0 <- x[0 : +40, 0 : +48] pad=1 status=s[0 : +{}]\n    quant_check s\n",
                records * one
            ),
        ))
    };
    let pieces = ktpu_machine::packing::pieces((0, 64, 40), (0, 64, 48), 16, 2);
    let live = ktpu_machine::packing::status_bytes(1, &pieces) / 128;
    assert!(live >= 2, "{pieces:?}");
    assert!(quant(live - 1).is_err());
    let text = quant(live).unwrap();
    let cores = machine().quant_cores();
    assert_eq!(text.matches(ASSERT).count() as i64, live * cores);
}

/// The mutant checks only the last pending record: one core's of the last
/// quant.
#[cfg(feature = "mutants")]
#[test]
fn deferred_last_only_checks_one_record() {
    let text = ktpu_l2::mutant::scoped(
        ktpu_l2::mutant::State {
            active: Some("l2.packing/deferred-last-only"),
            ..Default::default()
        },
        || lower(&deferred()).unwrap(),
    );
    assert_eq!(text.matches(ASSERT).count(), 1);
}

/// A mover-backend quant has no deferred status.
#[test]
fn a_mover_quant_takes_no_status() {
    let m = Machine::named("vlt_v9t6s_1n").unwrap();
    let text = "target ktpu.v9\n\nfn q.l2(x: f16[128, 64] @dram, q: mx7a[128, 64] @dram(gm=32,nk=1))\n    s = buffer : f16[1024] @dram\n    quant q <- x status=s\n    quant_check s\n";
    let p = Program::parse(text, "<status>", None).unwrap();
    let e = compile_l1(&p, &m, &[], false, &Search::default()).unwrap_err();
    assert!(e.message.contains("vector_mx7 quant's"), "{}", e.message);
}
