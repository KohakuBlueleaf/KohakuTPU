//! L1.VER.014: distinct register words competing for a bank's read copies.

use crate::{Finding, Kind};
use ktpu_l1::text::{Env, Expander, Module, Reader, Stmt, Term, Val};
use ktpu_machine::Machine;
use std::collections::BTreeSet;

fn operand(term: &Term) -> Option<(u32, u32, u32)> {
    let (name, base, stride) = match term {
        Term::Name(name) => (name, 0, 1),
        Term::View(name, view) => match view.first()? {
            Term::Int(n, _) => (name, u32::try_from(*n).ok()?, 0),
            Term::Slice(lo, _) => (
                name,
                lo.as_ref().and_then(|v| v.as_int()).unwrap_or(0) as u32,
                1,
            ),
            _ => return None,
        },
        Term::Call(_, args) => return args.iter().find_map(operand),
        Term::Neg(t) => return operand(t),
        _ => return None,
    };
    Some((name.strip_prefix('v')?.parse().ok()?, base, stride))
}

fn visit(stmts: &[Stmt], out: &mut Vec<Stmt>) {
    for st in stmts {
        if st.body.is_empty() {
            out.push(st.clone());
        } else {
            visit(&st.body, out);
        }
    }
}

/// Diagnose all expanded images against the selected machine's register file.
pub fn lint(module: &Module, machine: &Machine) -> ktpu_l1::error::Result<Vec<Finding>> {
    let vc = &machine.vc;
    let (chunks, lanes, copies) = (vc.chunks, vc.lanes, vc.rf_copies);
    let banks = if crate::mutant::on("verify.lint/one-bank") {
        1
    } else {
        vc.rf_banks
    };
    let reader = Reader::new(&module.source, &module.file);
    let macros = module
        .macros
        .iter()
        .map(|(k, v)| (k.clone(), (v.params.clone(), v.stmts.clone())))
        .collect();
    let mut out = Vec::new();
    for image in module.images.values() {
        let env: Env = image
            .params
            .iter()
            .map(|p| (p.clone(), Val::Int(0)))
            .collect();
        let expanded = Expander::new(&macros, &reader, true).stmts(&image.stmts, &env)?;
        let mut flat = Vec::new();
        visit(&expanded, &mut flat);
        for (index, st) in flat.iter().enumerate() {
            if !ktpu_l1::lower::ops::is_math(&st.op) {
                continue;
            }
            let reads: Vec<_> = st
                .positional()
                .iter()
                .skip(1)
                .filter_map(|t| operand(t))
                .collect();
            let beats = st
                .kw("vl")
                .and_then(Term::as_int)
                .map(|v| (v.max(1) as u32).div_ceil(lanes))
                .unwrap_or(chunks);
            let (mut held, mut starved) = (0, 0);
            for beat in 0..beats {
                let words: BTreeSet<_> = reads
                    .iter()
                    .map(|&(reg, base, stride)| (reg, (base + stride * beat) % chunks))
                    .collect();
                for bank in 0..banks {
                    let n = words
                        .iter()
                        .filter(|(_, chunk)| chunk % banks == bank)
                        .count() as u32;
                    held += u32::from(n > copies);
                    starved += u32::from(n >= copies);
                }
            }
            if held > 0 || starved > 0 {
                let mut reasons = Vec::new();
                if held > 0 {
                    reasons.push(format!("{held} beats hold the math engine"));
                }
                if starved > 0 {
                    reasons.push(format!("{starved} leave the pack engine no copy"));
                }
                out.push(Finding::new(
                    Kind::Bank,
                    format!("image {}", image.name),
                    format!(
                        "line {}, statement {}: {}: {}",
                        st.line,
                        index,
                        ktpu_l1::text::head(st),
                        reasons.join("; ")
                    ),
                ));
            }
        }
    }
    Ok(out)
}
