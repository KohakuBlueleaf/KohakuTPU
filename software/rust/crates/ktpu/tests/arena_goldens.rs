//! Frozen arena layouts from the existing end-to-end inputs. Regeneration
//! validates every extent independently and audits word-to-line changes.

#![cfg(all(feature = "stack", feature = "mutants"))]

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use ktpu::pipeline::stage::Backend;
use ktpu::Stack;
use ktpu_l1::layout::{buffer::Buffer, scratch::arenas};
use ktpu_l1::text::read;
use ktpu_machine::Machine;
use serde_json::{json, Value};

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../..")
}

fn layouts() -> (Value, Value, Value) {
    let mut paths: Vec<_> = std::fs::read_dir(root().join("software/rust/golden/e2e"))
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|e| e == "json"))
        .collect();
    paths.sort();
    let (mut new, mut old, mut review) = (BTreeMap::new(), BTreeMap::new(), BTreeMap::new());
    for path in paths {
        let case: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let (Some(l1), Some(l2)) = (case["l1"].as_str(), case["l2"].as_str()) else {
            continue;
        };
        let p = Stack.parse_l2(l2).unwrap();
        let name = case["name"].as_str().unwrap_or(&p.name);
        let caller: BTreeSet<_> = Stack.caller(&p, name).unwrap().into_iter().collect();
        let mo = read(l1, "<arena-golden>").unwrap();
        let m = Machine::named(case["machine"].as_str().unwrap()).unwrap();
        let got = arenas(&mo, name, &caller, &m).unwrap();
        let before = ktpu_l1::mutant::with("l1.layout/word-aligned-arena", || {
            arenas(&mo, name, &caller, &m).unwrap()
        });
        assert_eq!(got.len(), before.len());
        let line = u64::from(m.card.dram.line_bytes);
        let sizes: BTreeMap<_, _> = mo
            .body(name, "l1")
            .unwrap()
            .params
            .iter()
            .map(|(n, t)| (n.clone(), Buffer::of(t).unwrap().bytes(&m).unwrap()))
            .collect();
        let encode = |a: &ktpu_l1::layout::scratch::Arena| {
            json!({
                "space": a.space, "node": a.node, "bytes": a.bytes, "members": a.members,
            })
        };
        let mut changes = Vec::new();
        for (a, b) in got.iter().zip(&before) {
            assert_eq!((&a.space, a.node), (&b.space, b.node));
            assert_eq!(a.members.len(), b.members.len());
            let mut end = 0;
            for ((n, off), (bn, boff)) in a.members.iter().zip(&b.members) {
                assert_eq!(n, bn);
                assert_eq!(*off, end, "{}: {n}", path.display());
                assert_eq!(off % line, 0);
                assert!(off >= boff);
                end = (off + sizes[n]).div_ceil(line) * line;
            }
            assert_eq!(a.bytes, end);
            assert!(a.bytes >= b.bytes);
            if a != b {
                changes.push(json!({
                    "space": a.space, "node": a.node,
                    "bytes_before": b.bytes, "bytes_after": a.bytes,
                    "members": a.members.len(),
                    "offsets_moved": a.members.iter().zip(&b.members).filter(|(x, y)| x.1 != y.1).count(),
                }));
            }
        }
        let key = path.file_stem().unwrap().to_string_lossy().to_string();
        new.insert(key.clone(), got.iter().map(encode).collect::<Vec<_>>());
        old.insert(key.clone(), before.iter().map(encode).collect::<Vec<_>>());
        if !changes.is_empty() {
            review.insert(key, changes);
        }
    }
    (json!(new), json!(old), json!(review))
}

#[test]
fn frozen_arenas_have_exclusive_write_lines() {
    let (got, old, review) = layouts();
    let mode = std::env::var("KTPU_LINE_REGOLD").unwrap_or_default();
    assert!(["", "audit", "write"].contains(&mode.as_str()));
    println!(
        "{} layouts audited; {} cases grow or move offsets",
        got.as_object().unwrap().len(),
        review.as_object().unwrap().len()
    );
    println!("{}", serde_json::to_string_pretty(&review).unwrap());
    let golden = root().join("software/rust/golden/arena/lines.json");
    if mode == "write" {
        let dir = PathBuf::from(
            std::env::var("KTPU_LINE_REGOLD_OUT")
                .expect("KTPU_LINE_REGOLD_OUT: directory receiving the word-arena audit"),
        );
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::create_dir_all(golden.parent().unwrap()).unwrap();
        for (path, value) in [
            (dir.join("arena-word.json"), old),
            (dir.join("arena-review.json"), review),
            (golden.clone(), got.clone()),
        ] {
            std::fs::write(path, serde_json::to_string_pretty(&value).unwrap() + "\n").unwrap();
        }
    }
    if mode != "audit" {
        let want: Value = serde_json::from_str(&std::fs::read_to_string(golden).unwrap()).unwrap();
        assert_eq!(got, want);
    }
}
