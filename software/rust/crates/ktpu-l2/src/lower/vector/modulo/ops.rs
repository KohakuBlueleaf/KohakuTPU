//! Scheduler operations and dependences; template parsing is witness-only.

use std::collections::{BTreeSet, HashMap, HashSet};

use super::*;

pub(super) mod typed;

#[derive(Clone, Debug)]
#[cfg_attr(test, derive(PartialEq, Eq))]
pub struct MOp {
    pub op: String,
    #[cfg(feature = "witness")]
    pub tmpl: String,
    pub syms: Vec<String>,
    /// 'U' unpack, 'M' math, 'P' pack
    pub eng: char,
    pub reads: Vec<String>,
    pub writes: Vec<String>,
    pub occ: i64,
    /// the L1 word an unpack reads / a pack writes
    pub l1: Option<i64>,
    pub t: i64,
    pub e: i64,
    pub deps: Vec<(usize, i64, bool)>,
    pub xreads: Vec<String>,
}

#[cfg(feature = "witness")]
fn walk(opnd: &str, syms: &[String]) -> Option<(String, u64, u64)> {
    let sym = pat::first_sym(opnd)?;
    let s = syms[sym].clone();
    match pat::first_select(opnd) {
        None => Some((s, 0, 1)),
        Some((b, colon)) => Some((s, b, if colon { 1 } else { 0 })),
    }
}

#[cfg(feature = "witness")]
fn hold_beats(op: &str, opnds: &[String], syms: &[String], beats: u64, core: &Core) -> i64 {
    let mut fields = vec!['a'];
    if READS_B.contains(&op) {
        fields.push('b');
    }
    if READS_C.contains(&op) {
        fields.push('c');
    }
    if fields.len() != 3 || opnds.len() < 4 {
        return 0;
    }
    if opnds[3].contains('(') {
        return 0;
    }
    let mut walks = Vec::new();
    for o in &opnds[1..4] {
        match walk(o, syms) {
            Some(w) => walks.push(w),
            None => return 0,
        }
    }
    let mut held = 0;
    for i in 0..beats {
        let words: HashSet<(&str, u64)> = walks
            .iter()
            .map(|(s, b, st)| (s.as_str(), (b + st * i) % core.chunks as u64))
            .collect();
        let banks: HashSet<u64> = words.iter().map(|(_, c)| c % core.rf_banks).collect();
        if words.len() == 3 && banks.len() == 1 {
            held += 1;
        }
    }
    held
}

/// One instruction of an iteration, from its template and symbols.
#[cfg(feature = "witness")]
pub fn parse(tmpl: &str, syms: &[String], copies: u32, core: &Core) -> MOp {
    let chunks_all = core.chunks as u64;
    let (op, rest) = tmpl.split_once(' ').unwrap_or((tmpl, ""));
    let l1 = || pat::l1_word(tmpl).map(|(_, _, n)| n as i64);
    if op == "vunpk" {
        let chunks = match pat::kw_num(tmpl, "n") {
            Some(n) => n,
            None => {
                let cb = pat::refs(tmpl)
                    .into_iter()
                    .find(|r| r.sym == 0 && matches!(r.sel, Some((_, true))))
                    .map(|r| r.sel.unwrap().0)
                    .unwrap_or(0);
                chunks_all - cb
            }
        };
        let words = chunks * if tmpl.contains(" f32") { 2 } else { 1 };
        let partial = chunks < chunks_all;
        return MOp {
            op: op.to_string(),
            tmpl: tmpl.to_string(),
            syms: syms.to_vec(),
            eng: 'U',
            reads: if partial {
                vec![syms[0].clone()]
            } else {
                vec![]
            },
            writes: vec![syms[0].clone()],
            occ: words as i64,
            l1: l1(),
            t: 0,
            e: 0,
            deps: vec![],
            xreads: vec![],
        };
    }
    if op == "vpack" {
        let mut words = if tmpl.contains(" mx7") {
            (core.timing.mx7_pack_words + core.timing.mx7_issue_gap) as u64
        } else {
            pat::kw_num(tmpl, "n").unwrap_or(chunks_all)
        };
        if tmpl.contains(" f32") {
            words *= 2;
        }
        return MOp {
            op: op.to_string(),
            tmpl: tmpl.to_string(),
            syms: syms.to_vec(),
            eng: 'P',
            reads: vec![syms[0].clone()],
            writes: vec![],
            occ: words as i64,
            l1: l1(),
            t: 0,
            e: 0,
            deps: vec![],
            xreads: vec![],
        };
    }
    let body = pat::strip_options(rest);
    let opnds = pat::operands(&body);
    let beats = core.beats(pat::kw_num(tmpl, "vl"));
    let dest = opnds.first().cloned().unwrap_or_default();
    let mut reads: BTreeSet<String> = BTreeSet::new();
    let mut xreads: BTreeSet<String> = BTreeSet::new();
    for x in opnds.iter().skip(1) {
        for s in pat::syms(x) {
            reads.insert(syms[s].clone());
            if x.contains('(') && !crate::mutant::on("l2.vector/xreads-dropped") {
                xreads.insert(syms[s].clone());
            }
        }
    }
    let writes = if COMPARE.contains(&op) {
        vec![PRED.to_string()]
    } else if let Some(s) = pat::first_sym(&dest) {
        vec![syms[s].clone()]
    } else {
        vec![]
    };
    if pat::predicated(tmpl) {
        reads.insert(PRED.to_string());
    }
    let hold = if copies == 2 {
        hold_beats(op, &opnds, syms, beats, core)
    } else {
        0
    };
    MOp {
        op: op.to_string(),
        tmpl: tmpl.to_string(),
        syms: syms.to_vec(),
        eng: 'M',
        reads: reads.into_iter().collect(),
        writes,
        occ: beats as i64 + hold,
        l1: None,
        t: 0,
        e: 0,
        deps: vec![],
        xreads: xreads.into_iter().collect(),
    }
}

/// RAW with latency after the producer's last beat; WAR / WAW in order.
pub fn link(ops: &mut [MOp], lat: &Params) {
    let mut last_w: HashMap<String, Vec<usize>> = HashMap::new();
    let mut readers: HashMap<String, Vec<usize>> = HashMap::new();
    for i in 0..ops.len() {
        let mut deps = Vec::new();
        for x in &ops[i].reads {
            for &p in last_w.get(x).map(|v| v.as_slice()).unwrap_or(&[]) {
                let po = &ops[p];
                let mut l = if po.eng == 'U' {
                    lat.unpack_math
                } else if ops[i].eng == 'P' {
                    lat.math_pack
                } else {
                    lat.math_math
                };
                if po.eng == 'M' && ops[i].xreads.contains(x) {
                    l += lat.xbar;
                }
                deps.push((p, l, true));
            }
        }
        for x in &ops[i].writes {
            for &p in last_w.get(x).map(|v| v.as_slice()).unwrap_or(&[]) {
                deps.push((p, 1, false));
            }
            for &p in readers.get(x).map(|v| v.as_slice()).unwrap_or(&[]) {
                deps.push((p, 1, false));
            }
        }
        ops[i].deps.extend(deps);
        for x in ops[i].reads.clone() {
            readers.entry(x).or_default().push(i);
        }
        for x in ops[i].writes.clone() {
            last_w.entry(x.clone()).or_default().push(i);
            readers.insert(x, Vec::new());
        }
    }
}
