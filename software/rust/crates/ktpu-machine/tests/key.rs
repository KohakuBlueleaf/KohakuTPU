//! The digest against the read set: it moves on a keyed fact and on no
//! [`Runtime`](ktpu_machine::Runtime) fact, no compile path reaches a runtime
//! fact, and every keyed field has a compile path that names it.

mod leaves;

use ktpu_machine::{machines_dir, names, Machine};
use leaves::{leaves, moved, runtime_moved, set};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

/// The source a compile path runs: graph to image, and the generators and
/// planners behind a library kernel. Under `crates/`.
const COMPILE: [&str; 11] = [
    "ktpu-graph/src",
    "ktpu-l0/src",
    "ktpu-l1/src",
    "ktpu-l2/src",
    "ktpu-verify/src",
    "ktpu-isa/src",
    "ktpu-kernels/src",
    "ktpu-plan/src",
    "ktpu/src/stack",
    "ktpu/src/pipeline",
    "ktpu/src/templates",
];

fn crates() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("..")
}

fn sources(root: &Path, out: &mut Vec<PathBuf>) {
    for entry in std::fs::read_dir(root).unwrap().flatten() {
        let path = entry.path();
        if path.is_dir() {
            sources(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            out.push(path);
        }
    }
}

/// The source files of a compile path under `root`; the `spec` conformance
/// runners run no compile.
fn compile_sources(root: &Path) -> Vec<PathBuf> {
    let mut all = Vec::new();
    for dir in COMPILE {
        sources(&root.join(dir), &mut all);
    }
    all.retain(|p| !p.components().any(|c| c.as_os_str() == "spec"));
    all
}

/// The `file:line`s under `root` that read a runtime fact, directly or through
/// the runtime's card view.
fn runtime_reads(root: &Path) -> Vec<String> {
    let mut out = Vec::new();
    for path in compile_sources(root) {
        let text = std::fs::read_to_string(&path).unwrap();
        for (i, line) in text.lines().enumerate() {
            let code = line.split("//").next().unwrap();
            if [".runtime()", ".runtime_fact(", "Card::of("]
                .iter()
                .any(|r| code.contains(r))
            {
                out.push(format!("{}:{}", path.display(), i + 1));
            }
        }
    }
    out
}

#[test]
fn compile_reads_no_runtime_fact() {
    let reads = runtime_reads(&crates());
    assert!(
        reads.is_empty(),
        "compile paths read runtime facts: {reads:?}"
    );
}

#[test]
fn a_runtime_read_in_a_compile_path_is_seen() {
    let root = std::env::temp_dir().join(format!("ktpu-key-{}", std::process::id()));
    let dir = root.join("ktpu-l2/src");
    std::fs::create_dir_all(&dir).unwrap();
    for other in COMPILE {
        std::fs::create_dir_all(root.join(other)).unwrap();
    }
    std::fs::write(dir.join("lower.rs"), "let q = m.fw.queue.runtime();\n").unwrap();
    let reads = runtime_reads(&root);
    std::fs::remove_dir_all(&root).unwrap();
    assert_eq!(reads.len(), 1, "{reads:?}");
}

/// (keyed leaves that kept the digest, runtime leaves that moved it, leaves
/// walked) over every machine file.
fn walk() -> (Vec<String>, Vec<String>, usize) {
    let (mut kept, mut moved_by_runtime, mut walked) = (Vec::new(), Vec::new(), 0);
    let mut refused = Vec::new();
    let dir = machines_dir();
    for name in names(&dir) {
        let text = std::fs::read_to_string(dir.join(format!("{name}.json"))).unwrap();
        let mut doc: Value = serde_json::from_str(&text).unwrap();
        let backends = serde_json::json!(["mover_quant", "vector_mx7"]);
        doc["machine"]["conversion"]["fp16_mx7"]["allowed_backends"] = backends;
        let base = Machine::from_json(&doc.to_string()).unwrap();
        for (path, at) in leaves(&doc) {
            let Some(to) = moved(&doc, &at) else { continue };
            let mut next = doc.clone();
            set(&mut next, &at, to);
            let next = match Machine::from_json(&next.to_string()) {
                Ok(m) => m,
                Err(e) => {
                    refused.push(format!("{name} {path}: {e}"));
                    continue;
                }
            };
            walked += 1;
            let keyed = base.get(&path).is_some();
            let same = next.digest() == base.digest();
            if keyed && same {
                kept.push(format!("{name} {path}"));
            }
            if !keyed && !same {
                moved_by_runtime.push(format!("{name} {path}"));
            }
        }
        let all = Machine::from_json(&runtime_moved(&doc).to_string()).unwrap();
        if all.digest() != base.digest() {
            moved_by_runtime.push(format!("{name} every runtime fact"));
        }
    }
    assert!(refused.is_empty(), "{refused:?}");
    (kept, moved_by_runtime, walked)
}

#[test]
fn the_digest_moves_on_each_keyed_leaf_and_no_runtime_leaf() {
    let (kept, runtime, walked) = walk();
    assert!(walked > 1000, "{walked} leaves");
    assert!(kept.is_empty(), "keyed facts out of the digest: {kept:?}");
    assert!(
        runtime.is_empty(),
        "runtime facts in the digest: {runtime:?}"
    );
}

#[cfg(feature = "mutants")]
#[test]
fn key_mutants_are_killed() {
    use ktpu_machine::mutant;
    let (kept, runtime, _) = mutant::with("machine.key/whole-file", walk);
    assert!(kept.is_empty() && !runtime.is_empty());
    assert!(runtime.iter().any(|l| l.contains(" fw.sections.")));
    let (kept, runtime, _) = mutant::with("machine.key/drops-fact", walk);
    assert!(!kept.is_empty() && runtime.is_empty());
    assert!(kept.iter().all(|l| l.ends_with(" vc.sregs")), "{kept:?}");
    let (kept, runtime, _) = mutant::with("machine.key/drops-policy", walk);
    assert!(!kept.is_empty() && runtime.is_empty());
    assert!(kept.iter().all(|l| l.contains(" conversion.")), "{kept:?}");
}

/// Each pair of facts that does not hold on `m`: a keyed fact derived from a
/// runtime fact, or a runtime fact bound by a keyed one, or two runtime facts
/// from one definition.
fn disagreements(m: &Machine) -> Vec<&'static str> {
    let mut out = Vec::new();
    let mut must = |holds: bool, name| {
        if !holds {
            out.push(name);
        }
    };
    must(
        m.mover.max_index == m.mover.index_words.runtime() * 8,
        "mover.max_index against mover.index_words",
    );
    must(
        m.mg.dq_limit == m.mg.dq_depth.runtime() - 2 * m.mg.wburst,
        "mg.dq_limit against mg.dq_depth",
    );
    must(
        m.mg.acc_bits == m.mg.acc_mantissa_bits.runtime() + 8,
        "mg.acc_bits against mg.acc_mantissa_bits",
    );
    must(
        *m.mg.macs_per_cycle.runtime() == m.mg.lanes * m.mg.lanes * m.mg.kblock * m.mg.kstep,
        "mg.macs_per_cycle against the cluster's geometry",
    );
    must(
        m.unit.burst_words <= *m.unit.recv_depth.runtime(),
        "unit.burst_words past unit.recv_depth",
    );
    must(
        *m.node.imem_bytes.runtime() == m.fw.memory.runtime().imem_bytes,
        "node.imem_bytes against fw.memory.imem_bytes",
    );
    must(
        *m.node.mover_room_writes.runtime() <= *m.mover.cfgq_margin.runtime(),
        "node.mover_room_writes past mover.cfgq_margin",
    );
    must(
        m.sr.f16_ieee.runtime().is_none()
            || *m.sr.f16_ieee.runtime() == m.sr.profile.as_ref().map(|p| *p.f16_ieee.runtime()),
        "sr.f16_ieee against its profile",
    );
    out
}

#[test]
fn paired_facts_agree_on_every_machine() {
    for name in names(&machines_dir()) {
        let m = Machine::named(&name).unwrap();
        assert_eq!(disagreements(&m), Vec::<&str>::new(), "{name}");
    }
}

#[test]
fn a_disagreeing_pair_is_seen() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let moves = [
        ("mover.index_words", 300),
        ("mg.dq_depth", 5000),
        ("mg.acc_mantissa_bits", 15),
        ("mg.macs_per_cycle", 1),
        ("unit.recv_depth", 16),
        ("node.imem_bytes", 16),
        ("node.mover_room_writes", 9),
    ];
    for (path, to) in moves {
        let next = m.with_fact(path, to.into()).unwrap();
        assert_eq!(disagreements(&next).len(), 1, "{path}");
    }
    let sr = Machine::named("sru_v9t7").unwrap();
    let flipped = sr.with_fact("sr.f16_ieee", 1.into()).unwrap();
    assert_eq!(disagreements(&flipped).len(), 1);
}

/// The keyed fields of each schema struct: `pub name: Type`, a `Runtime` type
/// left out; `(field name, the struct it holds)`.
fn schema_fields(text: &str) -> BTreeMap<String, Vec<(String, String)>> {
    let mut out: BTreeMap<String, Vec<(String, String)>> = BTreeMap::new();
    let mut current = None;
    for line in text.lines() {
        let line = line.trim();
        if let Some(rest) = line.strip_prefix("pub struct ") {
            let name: String = rest.chars().take_while(|c| c.is_alphanumeric()).collect();
            current = Some(name);
        } else if line == "}" {
            current = None;
        } else if let (Some(s), Some(field)) = (&current, line.strip_prefix("pub ")) {
            let Some((name, ty)) = field.trim_end_matches(',').split_once(": ") else {
                continue;
            };
            if !ty.starts_with("Runtime<") {
                let inner: String = ty
                    .trim_start_matches("Option<")
                    .chars()
                    .take_while(|c| c.is_alphanumeric())
                    .collect();
                out.entry(s.clone())
                    .or_default()
                    .push((name.to_string(), inner));
            }
        }
    }
    out
}

#[test]
fn every_keyed_field_has_a_compile_reader() {
    let schema: String = std::fs::read_dir(crates().join("ktpu-machine/src/schema"))
        .unwrap()
        .flatten()
        .map(|e| std::fs::read_to_string(e.path()).unwrap())
        .collect::<Vec<_>>()
        .join("\n");
    let fields = schema_fields(&schema);
    let mut readers = compile_sources(&crates());
    sources(&crates().join("ktpu-machine/src"), &mut readers);
    let compile: String = readers
        .iter()
        .map(|p| std::fs::read_to_string(p).unwrap())
        .collect::<Vec<_>>()
        .join("\n");
    let read = |name: &str| {
        [".", "\"", "|", "(", "{ "].iter().any(|before| {
            compile
                .match_indices(&format!("{before}{name}"))
                .any(|(at, m)| {
                    let after = compile[at + m.len()..].chars().next();
                    !after.is_some_and(|c| c.is_alphanumeric() || c == '_')
                })
        })
    };
    let mut seen = BTreeSet::new();
    let mut open = vec!["Facts".to_string()];
    let mut unread = Vec::new();
    while let Some(s) = open.pop() {
        if !seen.insert(s.clone()) {
            continue;
        }
        for (name, inner) in fields.get(&s).into_iter().flatten() {
            open.push(inner.clone());
            if !read(name) {
                unread.push(format!("{s}.{name}"));
            }
        }
    }
    assert!(seen.contains("VcTiming"), "{seen:?}");
    assert!(
        unread.is_empty(),
        "keyed fields no compile path names: {unread:?}"
    );
}
