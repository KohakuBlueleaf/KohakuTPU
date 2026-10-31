//! Bank-aware placement of a vector core's values (L2.SEARCH.006).
//!
//! A V2 register file is `vc.rf_banks` banks (a word's chunk mod that), each `rf_copies`
//! read copies. With two copies a math beat reading three distinct words of
//! one bank holds the math engine a cycle, and a beat reading two leaves the
//! pack engine no copy; with three only the second, at three words. A value
//! stored rotated by one chunk reads the other bank on every beat: `rotate`
//! picks a rotation (0 or 1) per value and rewrites every reference.

use std::collections::{HashMap, HashSet};

#[cfg(feature = "witness")]
use super::gen::Code;
use super::modulo::Core;
#[cfg(feature = "witness")]
use super::pat;

pub mod typed;

const HOLD: i64 = 2;
const STARVE: i64 = 1;
const PASSES: usize = 16;
/// Past this many `vsel`s a value is assembled lane by lane: it stays unrotated.
const ASSEMBLED_VSELS: usize = 16;

#[cfg(feature = "witness")]
fn walk_op(tmpl: &str) -> bool {
    tmpl.starts_with("vunpk ") || tmpl.starts_with("vpack ")
}

/// An op whose register operand takes no chunk select.
#[cfg(feature = "witness")]
fn fixed(tmpl: &str) -> bool {
    walk_op(tmpl) && (tmpl.contains(" mx7") || tmpl.contains(" gt4"))
}

/// `(symbol, base, stride)` a math op reads a beat, or [] for a walk.
#[cfg(feature = "witness")]
fn reads<'a>(tmpl: &str, syms: &'a [String]) -> Vec<(&'a str, u64, u64)> {
    if walk_op(tmpl) {
        return Vec::new();
    }
    let rest = tmpl.split_once(' ').map(|x| x.1).unwrap_or("");
    let body = pat::strip_options(rest);
    let mut out = Vec::new();
    for opnd in pat::operands(&body).iter().skip(1) {
        let Some(r) = pat::refs(opnd).into_iter().next() else {
            continue;
        };
        let (base, stride) = match r.sel {
            Some((b, colon)) => (b, if colon { 1 } else { 0 }),
            None => (0, 1),
        };
        out.push((syms[r.sym].as_str(), base, stride));
    }
    out
}

#[cfg(feature = "witness")]
pub fn beats(tmpl: &str, core: &Core) -> u64 {
    core.beats(pat::kw_num(tmpl, "vl"))
}

fn cost(
    reads: &[(&str, u64, u64)],
    beats: u64,
    rho: &HashMap<&str, u64>,
    copies: u32,
    core: &Core,
) -> i64 {
    let (chunks, banks) = (core.chunks as u64, core.rf_banks);
    let mut cost = 0;
    for i in 0..beats {
        let mut words: HashSet<(&str, u64)> = HashSet::new();
        for &(s, b, st) in reads {
            words.insert((s, (b + rho.get(s).copied().unwrap_or(0) + st * i) % chunks));
        }
        for bank in 0..banks {
            let n = words.iter().filter(|(_, c)| c % banks == bank).count();
            if copies == 2 {
                cost += HOLD * (n >= 3) as i64 + STARVE * (n >= 2) as i64;
            } else if copies == 3 {
                cost += STARVE * (n >= 3) as i64;
            }
        }
    }
    cost
}

/// symbol -> rotation (only the rotated ones) for the ops of `code`.
#[cfg(feature = "witness")]
pub fn choose(code: &[Code], copies: u32, core: &Core) -> HashMap<String, u64> {
    if copies >= 4 || crate::mutant::on("l2.banks/rotates-none") {
        return HashMap::new();
    }
    let tuples: Vec<(&str, &[String])> = code
        .iter()
        .filter_map(|c| match c {
            Code::Op(t, s) => Some((t.as_str(), s.as_slice())),
            Code::Raw(_) => None,
        })
        .collect();
    let ops: Vec<_> = tuples
        .iter()
        .filter(|(t, _)| !walk_op(t))
        .map(|(t, s)| (reads(t, s), beats(t, core)))
        .collect();
    let mut fixed_syms: HashSet<&str> = tuples
        .iter()
        .filter(|(t, _)| fixed(t))
        .flat_map(|(_, s)| s.iter().map(|x| x.as_str()))
        .collect();
    let mut vsel_counts: HashMap<&str, usize> = HashMap::new();
    for (t, syms) in &tuples {
        if t.starts_with("vsel ") {
            for sym in *syms {
                *vsel_counts.entry(sym.as_str()).or_insert(0) += 1;
            }
        }
    }
    fixed_syms.extend(
        vsel_counts
            .into_iter()
            .filter_map(|(s, n)| (n > ASSEMBLED_VSELS).then_some(s)),
    );
    // A predicated write takes its lane mask by destination chunk
    // (v2_lanes.v:332), so its destination keeps rotation 0.
    if !crate::mutant::on("l2.banks/rotates-predicated") {
        fixed_syms.extend(
            tuples
                .iter()
                .filter(|(t, _)| pat::predicated(t))
                .filter_map(|(t, s)| {
                    let rest = t.split_once(' ').map(|x| x.1).unwrap_or("");
                    let dest = pat::operands(&pat::strip_options(rest)).first().cloned()?;
                    pat::first_sym(&dest).map(|i| s[i].as_str())
                }),
        );
    }
    let mut touching: Vec<(&str, Vec<usize>)> = Vec::new();
    let mut at: HashMap<&str, usize> = HashMap::new();
    for (k, (rs, _)) in ops.iter().enumerate() {
        for &(s, _, _) in rs {
            let i = *at.entry(s).or_insert_with(|| {
                touching.push((s, Vec::new()));
                touching.len() - 1
            });
            if !touching[i].1.contains(&k) {
                touching[i].1.push(k);
            }
        }
    }
    let free: Vec<usize> = (0..touching.len())
        .filter(|&i| !fixed_syms.contains(touching[i].0))
        .collect();
    let mut rho: HashMap<&str, u64> = HashMap::new();
    for _ in 0..PASSES {
        let mut moved = false;
        for &i in &free {
            let (s, ks) = (&touching[i].0, &touching[i].1);
            let now: i64 = ks
                .iter()
                .map(|&k| cost(&ops[k].0, ops[k].1, &rho, copies, core))
                .sum();
            let flip = 1 - rho.get(s).copied().unwrap_or(0);
            rho.insert(s, flip);
            let then: i64 = ks
                .iter()
                .map(|&k| cost(&ops[k].0, ops[k].1, &rho, copies, core))
                .sum();
            if then < now {
                moved = true;
            } else {
                rho.insert(s, 1 - flip);
            }
        }
        if !moved {
            break;
        }
    }
    rho.into_iter()
        .filter(|(_, r)| *r != 0)
        .map(|(s, r)| (s.to_string(), r))
        .collect()
}

/// `tmpl` with each reference to a rotated symbol moved by its rotation.
#[cfg(feature = "witness")]
pub fn apply(tmpl: &str, syms: &[String], rho: &HashMap<String, u64>, chunks: u64) -> String {
    if !syms.iter().any(|s| rho.get(s).copied().unwrap_or(0) != 0) {
        return tmpl.to_string();
    }
    let walk = walk_op(tmpl);
    let mut cb = 0u64;
    let mut out = String::with_capacity(tmpl.len() + 8);
    let mut last = 0;
    for r in pat::refs(tmpl) {
        out.push_str(&tmpl[last..r.start]);
        last = r.end;
        let rot = rho.get(&syms[r.sym]).copied().unwrap_or(0);
        if rot == 0 {
            out.push_str(&tmpl[r.start..r.end]);
            continue;
        }
        match r.sel {
            None => out.push_str(&format!("{{{}}}[{rot}:]", r.sym)),
            Some((b, colon)) => {
                cb = b;
                let c = if colon { ":" } else { "" };
                out.push_str(&format!("{{{}}}[{}{c}]", r.sym, (b + rot) % chunks));
            }
        }
    }
    out.push_str(&tmpl[last..]);
    if walk && !out.contains(" n=") {
        out.push_str(&format!(" n={}", chunks - cb));
    }
    out
}

/// `code` with its values placed by `choose`.
#[cfg(feature = "witness")]
pub fn rotate(code: Vec<Code>, copies: u32, core: &Core) -> Vec<Code> {
    if crate::mutant::banks_off() {
        return code;
    }
    let mut out = placed(code, copies, core);
    if crate::mutant::on("l2.banks/drops-op") {
        if let Some(at) = out.iter().rposition(|c| matches!(c, Code::Op(..))) {
            out.remove(at);
        }
    }
    out
}

#[cfg(feature = "witness")]
fn placed(code: Vec<Code>, copies: u32, core: &Core) -> Vec<Code> {
    let rho = choose(&code, copies, core);
    if rho.is_empty() {
        return code;
    }
    code.into_iter()
        .map(|c| match c {
            Code::Op(t, s) => {
                let t = apply(&t, &s, &rho, core.chunks as u64);
                Code::Op(t, s)
            }
            raw => raw,
        })
        .collect()
}

#[cfg(all(test, feature = "witness"))]
mod tests {
    use super::*;
    use crate::target::Target;
    use ktpu_machine::Machine;

    fn op(t: &str, s: &[&str]) -> Code {
        Code::Op(t.to_string(), s.iter().map(|x| x.to_string()).collect())
    }

    fn core() -> Core {
        Core::of(&Target::of(&Machine::named("multimesh_v9t6").unwrap()))
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_SEARCH_006_banks_keep_values() {
        let core = core();
        // Three operands of one bank on a two-copy file: one is rotated.
        let code = vec![op("vfma {0}, {1}, {2}, {3}", &["%1", "%2", "%3", "%4"])];
        let rho = choose(&code, 2, &core);
        assert!(!rho.is_empty());
        // Four copies: nothing to place.
        assert!(choose(&code, 4, &core).is_empty());
        let rot: HashMap<String, u64> = [("%2".to_string(), 1)].into();
        let names: Vec<String> = ["%1", "%2"].iter().map(|x| x.to_string()).collect();
        assert_eq!(
            apply("vadd {0}, {1}[4:], {1} vl=64", &names, &rot, 8),
            "vadd {0}, {1}[5:], {1}[1:] vl=64"
        );
        assert_eq!(
            apply("vunpk {1} <- l1[8] f16", &names, &rot, 8),
            "vunpk {1}[1:] <- l1[8] f16 n=8"
        );
    }
}
