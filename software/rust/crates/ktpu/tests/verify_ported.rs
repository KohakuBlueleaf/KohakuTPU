//! The static checker on L1 programs (`compiler/tests/test_verify.py`): the
//! L1 examples check clean, and each hazard the checker claims to catch is
//! caught on a program built to hold exactly that hazard. Each program goes
//! through the whole stack (`compile`, `Input::L1`) and the verdict's
//! findings are read; under `--features mutants` each test fails under a
//! checker mutant.

#![cfg(feature = "stack")]

use ktpu::{compile, Cache, Input, Stack};
use ktpu_machine::Machine;
use std::path::{Path, PathBuf};

fn machine(nodes: u32) -> Machine {
    let m = Machine::named("multimesh_v9t8").unwrap();
    match nodes {
        1 => m,
        n => m.with_nodes(n).unwrap(),
    }
}

/// `(kind, text)` of every finding the stack's checker makes of L1 `text`,
/// or the stage's refusal as one `("refused", ..)` row.
fn kinds_on(text: &str, nodes: u32) -> Vec<(String, String)> {
    let name = text
        .lines()
        .find_map(|l| l.strip_prefix("fn "))
        .and_then(|l| l.split('.').next())
        .unwrap()
        .to_string();
    match compile(
        &Stack,
        &Input::L1(text.into()),
        Some(&name),
        &machine(nodes),
        &Cache::off(),
    ) {
        Ok(c) => c
            .verdict
            .findings
            .into_iter()
            .map(|f| (f.kind, f.text))
            .collect(),
        Err(e) => vec![("refused".into(), e.to_string())],
    }
}

fn kinds(text: &str) -> Vec<(String, String)> {
    kinds_on(text, 1)
}

fn examples() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../language/kohakutpu/language/examples/l1")
}

/// The L1 example `name` (one file of that stem under `examples/l1`).
fn example(name: &str) -> String {
    fn find(dir: &Path, file: &str, out: &mut Vec<PathBuf>) {
        for e in std::fs::read_dir(dir).unwrap().flatten() {
            let p = e.path();
            if p.is_dir() {
                find(&p, file, out);
            } else if p.file_name().is_some_and(|n| n == file) {
                out.push(p);
            }
        }
    }
    let mut hits = vec![];
    find(&examples(), &format!("{name}.ktpu"), &mut hits);
    assert_eq!(hits.len(), 1, "{name}: {hits:?}");
    std::fs::read_to_string(&hits[0]).unwrap()
}

const ONE_NODE: [&str; 10] = [
    "add",
    "softmax",
    "layernorm",
    "silu",
    "matmul",
    "longk",
    "splitk",
    "tonode",
    "push",
    "memops",
];
const TWO_NODES: [&str; 6] = ["silu2", "ring", "remote", "xsplitk", "matmul2", "xpeer"];

/// Each example's findings: the one-node examples on one node, the
/// multinode ones on two.
pub fn examples_facts() -> Vec<(String, Vec<(String, String)>)> {
    ONE_NODE
        .iter()
        .map(|n| (n.to_string(), kinds_on(&example(n), 1)))
        .chain(
            TWO_NODES
                .iter()
                .map(|n| (n.to_string(), kinds_on(&example(n), 2))),
        )
        .collect()
}

#[test]
fn examples_verify_clean() {
    for (name, found) in examples_facts() {
        assert_eq!(found, vec![], "{name}");
    }
}

fn splitk(old: &str, new: &str) -> Vec<(String, String)> {
    let text = example("splitk");
    assert!(text.contains(old), "splitk holds {old:?}");
    kinds(&text.replace(old, new))
}

/// Without the marker's take only time orders the drain of the sum after
/// the add: an accumulator race naming the DRAIN.
pub fn settle_facts() -> bool {
    splitk("        fill A0 recv n=1\n", "")
        .iter()
        .any(|(k, t)| k == "race" && t.starts_with("acc ") && t.contains("DRAIN"))
}

#[test]
fn an_add_ordered_by_a_settle_races() {
    assert!(settle_facts());
}

pub fn marker_facts() -> bool {
    splitk("        fill A0 recv n=1\n", "        fill A0 <- x n=1\n")
        .iter()
        .any(|(k, t)| k == "rest" && t.contains("entries landed on side A"))
}

#[test]
fn a_marker_never_taken_is_a_rest_finding() {
    assert!(marker_facts());
}

/// A cluster streams 16 sub-tiles into vc[0]'s L1 under credits: `open`
/// posts the credits, `sink` waits for the granules.
fn stream(credits: u32, wait: u32, sink: &str) -> Vec<(String, String)> {
    kinds(&format!(
        "target ktpu.v9

image open()
    vpost mg[0] n={credits}
    halt

image sink()
    {sink}vwait fe rx n={wait}
    halt

fn s.l1(x: mx7a[4, 64] @dram(gm=1, nk=2), w: mx7b[64, 64] @dram(gn=16, nk=2))
    send vc[0]
        run open()
    send mg[0]
        fill A0 <- x n=2
        fill B0 <- w n=32
        gemm 1x16x2 a=A0 b=B0 emit to=vc[0] l1=0 count credit
        drain n=16 to=vc[0] l1=0 fused count credit
    send vc[0]
        run sink()
"
    ))
}

/// The stream's findings at each variant `test_verify.py` builds: clean,
/// short of credits, credits left over, granules never waited for, a write
/// before the wait, waiting for more than lands. A deadlock the scheduler
/// meets first refuses the build (L1.VER.016) with the same findings.
pub fn stream_facts() -> [bool; 6] {
    let has = |got: &[(String, String)], kind: &str, text: &str| {
        let refused = format!("kind: {}{}", kind[..1].to_uppercase(), &kind[1..]);
        got.iter()
            .any(|(k, t)| (k == kind || k == "refused" && t.contains(&refused)) && t.contains(text))
    };
    let write = "desc a0 = 0 walk=(32, 16)\n    vfill a0 -> l1[0]\n    ";
    let over = stream(16, 24, "");
    [
        stream(16, 16, "").is_empty(),
        has(&stream(8, 16, ""), "deadlock", "credits"),
        stream(24, 16, "").contains(&("rest".into(), "16 credits received and never spent".into())),
        stream(16, 8, "").contains(&(
            "rest".into(),
            "16 granules landed and never waited for".into(),
        )),
        stream(16, 16, write)
            .iter()
            .any(|(k, t)| k == "race" && t.contains("VFILL") && t.contains("burst")),
        has(&over, "deadlock", "VWAIT") || has(&over, "deadlock", "vwait"),
    ]
}

#[test]
fn the_stream_variants_find_their_hazards() {
    assert_eq!(stream_facts(), [true; 6]);
}

/// Both cores peer-drain into mg[0]'s L1 A with nothing ordering the two.
const TWO: &str = "target ktpu.v9

image put(at)
    desc a0 = peer(at, count=1) walk=(32, 16)
    vdrain a0 <- l1[0] to=mg[0] buf=0
    halt

fn s.l1(x: mx7a[4, 64] @dram(gm=1, nk=2))
    send vc[0]
        run put(0)
    send vc[1]
        run put(16)
    send mg[0]
        fill A0 recv n=8
";

/// Whether the port finding is made, and every port finding's clause.
pub fn two_facts() -> (bool, Vec<String>) {
    let name = "s";
    let c = compile(
        &Stack,
        &Input::L1(TWO.into()),
        Some(name),
        &machine(1),
        &Cache::off(),
    )
    .unwrap();
    let port: Vec<_> = c
        .verdict
        .findings
        .iter()
        .filter(|f| f.kind == "port")
        .collect();
    (
        port.iter().any(|f| f.text.contains("interleave")) && c.verdict.refused,
        port.iter().map(|f| f.clause.clone()).collect(),
    )
}

#[test]
fn two_unordered_senders_into_one_unit_are_found_and_refused() {
    let (found, clauses) = two_facts();
    assert!(found);
    assert!(
        !clauses.is_empty() && clauses.iter().all(|c| c == "L1.VER.008"),
        "{clauses:?}"
    );
}

/// Each test above fails under a checker mutant.
#[cfg(feature = "mutants")]
mod mutants {
    use super::*;

    fn breaks<T: PartialEq + std::fmt::Debug + Send>(name: &'static str, facts: fn() -> T) {
        let want = facts();
        let got = std::panic::catch_unwind(|| ktpu::mutant::with(name, facts).unwrap());
        if let Ok(got) = got {
            assert_ne!(got, want, "mutant {name} does not break the facts");
        }
    }

    #[test]
    fn examples_break_under_model_no_sync() {
        breaks("isa.vector/model_no_sync", examples_facts);
    }

    #[test]
    fn settle_breaks_under_checker() {
        breaks("isa.cluster/checker", settle_facts);
    }

    #[test]
    fn marker_breaks_under_checker() {
        breaks("isa.cluster/checker", marker_facts);
    }

    #[test]
    fn stream_breaks_under_model_no_sync() {
        breaks("isa.vector/model_no_sync", stream_facts);
    }

    #[test]
    fn two_breaks_under_checker() {
        breaks("isa.cluster/checker", two_facts);
    }
}
