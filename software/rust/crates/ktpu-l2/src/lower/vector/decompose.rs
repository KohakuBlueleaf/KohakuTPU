//! Bounded physical lane programs. HALT joins engines; RF and L1 remain on-core.

use std::collections::BTreeMap;

use ktpu_l1::text::{Module, Stmt, Term};

use super::{
    encode,
    gen::{print, typed as ir},
};
use crate::{ir::Result, target::Target};

fn integer(t: &Term) -> i128 {
    match t {
        Term::Int(n, _) => *n,
        Term::Neg(x) => -integer(x),
        _ => unreachable!("generated integer"),
    }
}

fn key(st: &Stmt) -> String {
    match &st.args[0] {
        Term::Assign(a, _) | Term::AddAssign(a, _) => a.as_name().unwrap().to_string(),
        a => a.as_name().unwrap().to_string(),
    }
}

fn linear(stmts: &[Stmt], scalars: &mut BTreeMap<String, i128>, out: &mut Vec<Stmt>) {
    for st in stmts {
        if st.op == "seti" {
            if let Term::Assign(a, b) = &st.args[0] {
                if let Some(n) = b.as_int() {
                    scalars.insert(a.as_name().unwrap().into(), n);
                }
            }
        }
        if st.op == "loop" {
            let n = scalars[st.args[0].as_name().unwrap()];
            for _ in 0..n {
                linear(&st.body, scalars, out);
            }
        } else if st.op != "halt" {
            out.push(st.clone());
        }
    }
}

fn shifted(st: &Stmt, offset: i64) -> Stmt {
    let mut st = st.clone();
    if offset != 0 && !crate::mutant::on("l2.readmap/no-rebase") {
        if let Term::Assign(_, base) = &mut st.args[0] {
            **base = Term::Offset(base.clone(), Box::new(Term::int(offset as i128)));
        }
    }
    st
}

/// A candidate's images: `parts` is `(name, text)` of each continuation, and
/// empty for a candidate that runs whole.
pub struct Split {
    pub parts: Vec<(String, String)>,
    pub cycles: i64,
}

/// One-part candidates retain their schedule. Oversized programs are split only
/// after physical register allocation, so no intermediate DRAM value is created.
/// `timed`: the whole candidate's cycles; `splits`: whether it may split.
#[allow(clippy::too_many_arguments)]
pub fn split(
    module: &Module,
    name: &str,
    nparams: usize,
    t: &Target,
    timed: i64,
    splits: bool,
    fill: (i64, i64),
    copies: u32,
) -> Result<Split> {
    let limit = (crate::target::IMEM_ROOM as usize).min(t.machine.vc.imem_words as usize);
    let mut code = false;
    let inline_desc = module.images[name].stmts.iter().any(|s| {
        if s.op == "desc" {
            code
        } else {
            code = true;
            false
        }
    });
    let words = encode::words_of(module, name, nparams, &t.machine);
    if let Ok(words) = &words {
        if !inline_desc && (*words <= limit || crate::mutant::on("l2.vector/imem-unbounded")) {
            return Ok(Split {
                parts: Vec::new(),
                cycles: timed,
            });
        }
    }
    if !splits || crate::mutant::on("l2.readmap/no-decompose") {
        let words = words?;
        return Err(ktpu_l1::Error::new(
            "L2.VC.017",
            format!("{words} IMEM words (an image takes {limit})"),
        ));
    }
    let def = &module.images[name];
    let mut flat = Vec::new();
    linear(&def.stmts, &mut BTreeMap::new(), &mut flat);
    let mut descs = BTreeMap::<String, Stmt>::new();
    let mut offsets = BTreeMap::<String, i64>::new();
    let mut incs = BTreeMap::<String, i64>::new();
    let mut normalized = Vec::new();
    for st in flat {
        if st.op == "vmark" {
            continue;
        }
        if st.op == "vsync" {
            if st.kw("on").is_none() {
                for eng in ["fill", "unpack", "math", "pack", "drain"] {
                    normalized.push(ir::sync("fe", eng, None));
                }
                continue;
            }
            let mut st = st;
            st.args
                .retain(|a| !matches!(a,Term::Assign(k,_) if k.as_name()==Some("slack")));
            normalized.push(st);
        } else {
            normalized.push(st);
        }
    }
    if crate::mutant::on("l2.readmap/split-no-join") {
        normalized.retain(|s| s.op != "vsync");
    }
    let flat = normalized;
    // RUN clears engine sticky state. Keep every observed EXP2D/GETSTK
    // interval in one image; RF, scalar registers and L1 survive the cut.
    let last_read = flat.iter().rposition(|s| s.op == "getstk");
    let mut sticky = false;
    let mut cuts = vec![0];
    for (i, st) in flat.iter().enumerate() {
        if st.op == "vexp2d" && last_read.is_some_and(|last| i < last) {
            sticky = true;
        }
        if st.op == "getstk" {
            sticky = false;
        }
        if !sticky || crate::mutant::on("l2.readmap/split-sticky-cut") {
            cuts.push(i + 1);
        }
    }
    let mut parts = Vec::new();
    let mut cursor = 0;
    let mut cycles = 0;
    while cursor < flat.len() {
        while cursor < flat.len() && flat[cursor].op == "desc" {
            let st = &flat[cursor];
            let ad = key(st);
            descs.insert(ad.clone(), st.clone());
            offsets.insert(ad, 0);
            cursor += 1;
        }
        if cursor == flat.len() {
            break;
        }
        let part_name = match parts.len() {
            0 => name.to_string(),
            n => format!("{name}__part{n}"),
        };
        let mut body = descs
            .iter()
            .map(|(ad, st)| shifted(st, *offsets.get(ad).unwrap_or(&0)))
            .collect::<Vec<_>>();
        for (ad, inc) in &incs {
            body.push(ir::ainc(ad, *inc));
        }
        // Every engine starts empty. Zero-slack joins also discharge references
        // to marks in preceding parts, whose instructions HALT already retired.
        let stop = (cursor..flat.len())
            .find(|&i| flat[i].op == "desc")
            .unwrap_or(flat.len());
        let candidates = cuts
            .iter()
            .copied()
            .filter(|&i| cursor < i && i <= stop)
            .collect::<Vec<_>>();
        let mut low = 0;
        let mut high = candidates.len();
        let mut end = cursor;
        let mut last = None;
        let mut why = String::new();
        while low < high {
            let ix = (low + high) / 2;
            let mid = candidates[ix];
            let mut trial = body.clone();
            trial.extend_from_slice(&flat[cursor..mid]);
            if !parts.is_empty() && crate::mutant::on("l2.readmap/split-copy") {
                trial.insert(
                    body.len(),
                    ir::math("vmov", Term::name("v0"), vec![Term::name("k0")], vec![]),
                );
            }
            let needed = trial
                .iter()
                .filter(|s| matches!(s.op.as_str(), "vfill" | "vdrain"))
                .map(key)
                .collect::<std::collections::BTreeSet<_>>();
            trial.retain(|s| s.op != "desc" || needed.contains(&key(s)));
            trial.push(ir::instruction("halt", vec![], vec![]));
            let m = print::image(&part_name, def.params.clone(), trial, module.vregs).unwrap();
            match encode::words_of(&m, &part_name, nparams, &t.machine) {
                Ok(words) if words <= limit => {
                    last = Some(m);
                    end = mid;
                    low = ix + 1;
                }
                Ok(words) => {
                    why = format!("{words} words exceed {limit}");
                    high = ix;
                }
                Err(e) => {
                    why = e.to_string();
                    high = ix;
                }
            }
        }
        let m = last.ok_or_else(|| {
            ktpu_l1::Error::new("L2.VC.017", format!("continuation cannot encode: {why}"))
        })?;
        // Dispatch, IMEM reload and configuration cost are charged for every continuation.
        let part_words = encode::words_of(&m, &part_name, nparams, &t.machine)? as i64;
        cycles += encode::cycles(&m, &part_name, module.vregs, copies, fill, &t.machine)?
            + part_words
            + t.read.continuation_cycles;
        for st in &flat[cursor..end] {
            if st.op == "ainc" {
                if let Term::AddAssign(_, n) = &st.args[0] {
                    incs.insert(key(st), integer(n) as i64);
                }
            } else if matches!(st.op.as_str(), "vfill" | "vdrain")
                && st.args.iter().any(|a| a.as_name() == Some("rel"))
            {
                let ad = key(st);
                *offsets.entry(ad.clone()).or_default() += incs.get(&ad).copied().unwrap_or(0);
            }
        }
        parts.push((part_name, m.source));
        cursor = end;
    }
    Ok(Split { parts, cycles })
}

#[cfg(all(test, feature = "mutants"))]
mod tests {
    use super::*;

    #[test]
    fn sticky_region_does_not_cross_runs() {
        let machine = ktpu_machine::Machine::named("multimesh_v9t6").unwrap();
        let t = Target::of(&machine);
        let mut source = String::from("image sticky()\n    vmov v0, k1\n");
        for _ in 0..975 {
            source.push_str("    vadd v0, v0, k1\n");
        }
        source.push_str("    vexp2d v1, v0, k0\n");
        for _ in 0..30 {
            source.push_str("    vadd v0, v0, k1\n");
        }
        source.push_str("    vsync fe on=math\n    getstk s1\n");
        for _ in 0..100 {
            source.push_str("    vadd v0, v0, k1\n");
        }
        source.push_str("    halt\n");
        let m = encode::read_image(&source, "sticky", 128).unwrap();
        let one_region = |parts: &[(String, String)]| {
            parts.iter().any(|(name, text)| {
                let m = encode::read_image(text, name, 128).unwrap();
                let d = &m.images[name];
                d.stmts.iter().any(|s| s.op == "vexp2d") && d.stmts.iter().any(|s| s.op == "getstk")
            })
        };
        let cut = |m: &Module| split(m, "sticky", 0, &t, 0, true, (0, 0), 2);
        let good = cut(&m).unwrap();
        assert!(one_region(&good.parts));
        assert!(good.parts.len() > 1);
        assert_eq!(good.parts[0].0, "sticky");
        crate::mutant::scoped(
            crate::mutant::State {
                active: Some("l2.readmap/no-decompose"),
                ..Default::default()
            },
            || assert!(cut(&m).is_err()),
        );
        crate::mutant::scoped(
            crate::mutant::State {
                active: Some("l2.readmap/split-sticky-cut"),
                ..Default::default()
            },
            || assert!(!one_region(&cut(&m).unwrap().parts)),
        );
    }
}
