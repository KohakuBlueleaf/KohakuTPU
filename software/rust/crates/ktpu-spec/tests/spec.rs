//! Every tier-0 spec vector whose runner has a Rust port, by clause: frozen
//! and ruling vectors alike must pass, and each mutant a frozen vector names
//! must break it (a mutant with no Rust knob is a finding, as one that
//! survives).

use ktpu_spec::{coverage, run, vectors, Witness};
use std::collections::BTreeMap;

#[test]
fn tier0_vectors_with_a_port_pass() {
    let all = vectors::load(&vectors::root()).expect("vectors");
    let (with, without) = coverage(&all);
    println!("runners with a port ({}):", with.len());
    for (r, n) in &with {
        println!("  {r} ({n} vectors)");
    }
    println!("runners without a port, skipped ({}):", without.len());
    for (r, n) in &without {
        println!("  {r} ({n} vectors)");
    }
    let got = run(&all);
    let mut by_clause: BTreeMap<&str, (usize, usize)> = BTreeMap::new();
    let mut by_status: BTreeMap<&str, (usize, usize)> = BTreeMap::new();
    let mut witnesses: BTreeMap<&str, usize> = BTreeMap::new();
    let mut bad = Vec::new();
    for o in &got {
        for e in [
            by_clause.entry(&o.clause).or_default(),
            by_status.entry(&o.status).or_default(),
        ] {
            match o.failure {
                None => e.0 += 1,
                Some(_) => e.1 += 1,
            }
        }
        if let Some(why) = &o.failure {
            bad.push(format!("{} [{}]: {why}", o.id, o.status));
        }
        for (m, w) in &o.mutants {
            let tag = match w {
                Witness::Killed => "killed",
                Witness::Survived => "survived",
                Witness::Skipped(_) => "no knob",
            };
            *witnesses.entry(tag).or_default() += 1;
            match w {
                Witness::Killed => {}
                Witness::Survived => bad.push(format!("{} ~ {m}: the mutant survives", o.id)),
                Witness::Skipped(why) => bad.push(format!("{} ~ {m}: no knob ({why})", o.id)),
            }
        }
    }
    println!("clauses (pass / fail):");
    for (c, (p, f)) in &by_clause {
        println!("  {c} {p} / {f}");
    }
    for (s, (p, f)) in &by_status {
        println!("{s} vectors: {} run, {p} pass, {f} fail", p + f);
    }
    let failed = got.iter().filter(|o| o.failure.is_some()).count();
    println!(
        "all: {} run, {} pass, {failed} fail",
        got.len(),
        got.len() - failed
    );
    println!("mutants: {witnesses:?}");
    let superseded: Vec<&str> = got
        .iter()
        .filter(|o| o.superseded)
        .map(|o| o.id.as_str())
        .collect();
    println!(
        "frozen vectors run as a ruling supersedes them ({}): {superseded:?}",
        superseded.len()
    );
    assert!(bad.is_empty(), "\n{}", bad.join("\n"));
}

#[test]
fn every_vector_reads() {
    let all = vectors::load(&vectors::root()).expect("vectors");
    assert!(all.len() > 1000, "{} vectors", all.len());
    for v in &all {
        assert!(
            v.id.rsplit('/')
                .next()
                .unwrap()
                .starts_with(&format!("{}.", v.clause)),
            "{}",
            v.id
        );
        assert!(
            v.status == "frozen" || v.status == "ruling",
            "{}: status {}",
            v.id,
            v.status
        );
    }
}
