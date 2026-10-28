//! Typed reduction steps and quantiser operand edits.

use crate::lower::vector::gen::typed::{self as ir, Event, Select, Symbol};
use ktpu_l1::text::{Stmt, Term};

#[cfg(all(test, feature = "witness"))]
pub(crate) mod tests;

#[derive(Clone, Debug, PartialEq)]
pub struct Insn {
    pub stmt: Stmt,
    pub syms: Vec<Symbol>,
}
pub type Steps = Vec<Vec<Insn>>;

#[derive(Default, Clone, Debug, PartialEq)]
pub struct Gen {
    pub n: u32,
    pub code: Vec<Event>,
}
impl Gen {
    pub fn sym(&mut self) -> Symbol {
        self.n += 1;
        Symbol(self.n)
    }
    pub fn emit(&mut self, insn: Insn) {
        self.code.push(Event::Op {
            stmt: insn.stmt,
            syms: insn.syms,
        });
    }
}

fn whole(s: &Symbol) -> Term {
    s.operand(Select::Whole)
}
fn chunk(s: &Symbol, c: u64) -> Term {
    s.operand(Select::Stride(c))
}
fn insn(op: &str, dest: Term, srcs: Vec<Term>, vl: Option<i128>, syms: &[Symbol]) -> Insn {
    Insn {
        stmt: ir::math(
            op,
            dest,
            srcs,
            vl.map(|v| vec![ir::kw("vl", Term::int(v))])
                .unwrap_or_default(),
        ),
        syms: syms.to_vec(),
    }
}

pub fn interleave(g: &mut Gen, trees: &[Steps]) {
    for s in 0..trees[0].len() {
        let longest = trees.iter().map(|t| t[s].len()).max().unwrap_or(0);
        for i in 0..longest {
            for t in trees {
                if i < t[s].len() {
                    g.emit(t[s][i].clone());
                }
            }
        }
    }
}

pub fn tree4(
    g: &mut Gen,
    code: &str,
    regs: &[Vec<Symbol>],
    into: Option<Symbol>,
    fused: bool,
) -> (Steps, Symbol) {
    let mut steps = vec![];
    let mut syms;
    if fused {
        let (mut muls, mut fmas, mut next) = (vec![], vec![], vec![]);
        for k in (0..regs.len()).step_by(2) {
            let d = g.sym();
            muls.push(insn(
                "vmul",
                whole(&d),
                vec![whole(&regs[k][0]), whole(&regs[k][1])],
                None,
                &[d.clone(), regs[k][0].clone(), regs[k][1].clone()],
            ));
            if k + 1 < regs.len() {
                fmas.push(insn(
                    "vfma",
                    whole(&d),
                    vec![whole(&regs[k + 1][0]), whole(&regs[k + 1][1]), whole(&d)],
                    None,
                    &[d.clone(), regs[k + 1][0].clone(), regs[k + 1][1].clone()],
                ));
            }
            next.push(d);
        }
        muls.extend(fmas);
        steps.push(muls);
        syms = next;
    } else {
        syms = regs.iter().map(|r| r[0].clone()).collect::<Vec<_>>();
    }
    while syms.len() > 1 {
        let (mut level, mut next) = (vec![], vec![]);
        for pair in syms.chunks_exact(2) {
            let d = g.sym();
            level.push(insn(
                code,
                whole(&d),
                vec![whole(&pair[0]), whole(&pair[1])],
                None,
                &[d.clone(), pair[0].clone(), pair[1].clone()],
            ));
            next.push(d);
        }
        steps.push(level);
        if syms.len() % 2 == 1 {
            next.push(syms.last().unwrap().clone());
        }
        syms = next;
    }
    let d = into.unwrap_or_else(|| g.sym());
    steps.push(vec![insn(
        code,
        chunk(&d, 0),
        vec![chunk(&syms[0], 0), chunk(&syms[0], 4)],
        Some(64),
        &[d.clone(), syms[0].clone()],
    )]);
    for k in [8, 4, 2, 1] {
        steps.push(vec![insn(
            code,
            chunk(&d, 0),
            vec![chunk(&d, 0), ir::crossbar("xor", whole(&d), k)],
            Some(64),
            std::slice::from_ref(&d),
        )]);
    }
    if !crate::mutant::on("l2.vector/band-no-broadcast") {
        steps.push(vec![insn(
            "vmov",
            chunk(&d, 4),
            vec![chunk(&d, 0)],
            Some(64),
            std::slice::from_ref(&d),
        )]);
    }
    (steps, d)
}

pub fn tree(
    g: &mut Gen,
    code: &str,
    rows: &[Vec<Symbol>],
    fused: bool,
    into: Option<Symbol>,
) -> (Steps, Symbol) {
    let t: Vec<_> = (0..4).map(|_| g.sym()).collect();
    let mut first = vec![];
    for i in 0..8 {
        let c = (4 * (i / 4)) as u64;
        let x = &rows[2 * (i % 4) + i / 4];
        let d = &t[i % 4];
        if fused {
            let syms = [d.clone(), x[0].clone(), x[1].clone()];
            first.push(insn(
                "vmul",
                chunk(d, c),
                vec![chunk(&x[0], 4), chunk(&x[1], 4)],
                Some(64),
                &syms,
            ));
            first.push(insn(
                "vfma",
                chunk(d, c),
                vec![chunk(&x[0], 0), chunk(&x[1], 0), chunk(d, c)],
                Some(64),
                &syms,
            ));
        } else {
            first.push(insn(
                code,
                chunk(d, c),
                vec![chunk(&x[0], 0), chunk(&x[0], 4)],
                Some(64),
                &[d.clone(), x[0].clone()],
            ));
        }
    }
    if fused {
        first = first
            .iter()
            .step_by(2)
            .chain(first.iter().skip(1).step_by(2))
            .cloned()
            .collect();
    }
    let mut second = vec![];
    for i in 0..8 {
        let c = (4 * (i / 4)) as u64;
        let d = &t[i % 4];
        second.push(insn(
            code,
            chunk(d, c),
            vec![chunk(d, c), chunk(d, c + 2)],
            Some(32),
            std::slice::from_ref(d),
        ));
    }
    let acc = g.sym();
    let mut third = vec![];
    for r in 0..8 {
        let c = (4 * (r % 2)) as u64;
        let s = &t[r / 2];
        third.push(insn(
            code,
            chunk(&acc, r as u64),
            vec![chunk(s, c), chunk(s, c + 1)],
            Some(16),
            &[acc.clone(), s.clone()],
        ));
    }
    let m = g.sym();
    let d = into.unwrap_or_else(|| g.sym());
    let steps = vec![
        first,
        second,
        third,
        vec![insn(
            code,
            whole(&m),
            vec![whole(&acc), ir::crossbar("merge", whole(&acc), 8)],
            None,
            &[m.clone(), acc],
        )],
        vec![insn(
            code,
            whole(&m),
            vec![whole(&m), ir::crossbar("merge", chunk(&m, 4), 4)],
            Some(64),
            std::slice::from_ref(&m),
        )],
        vec![insn(
            code,
            whole(&m),
            vec![whole(&m), ir::crossbar("merge", chunk(&m, 2), 2)],
            Some(32),
            std::slice::from_ref(&m),
        )],
        vec![insn(
            code,
            chunk(&d, 0),
            vec![whole(&m), ir::crossbar("merge", chunk(&m, 1), 1)],
            Some(16),
            &[d.clone(), m],
        )],
    ];
    (steps, d)
}

pub fn compact(g: &mut Gen, bands: &[Symbol]) -> Symbol {
    let reversed: Vec<_> = bands.iter().rev().cloned().collect();
    let bands = if crate::mutant::on("l2.vector/compact-reversed") {
        &reversed
    } else {
        bands
    };
    let (a, b, c, d) = (g.sym(), g.sym(), g.sym(), g.sym());
    for r in 0..8 {
        let (lo, hi) = (&bands[r / 4], &bands[(r + 8) / 4]);
        g.emit(insn(
            "vmax",
            chunk(&a, r as u64),
            vec![
                chunk(lo, (r % 4) as u64),
                ir::crossbar("merge", chunk(hi, (r % 4) as u64), 8),
            ],
            Some(16),
            &[a.clone(), lo.clone(), hi.clone()],
        ));
    }
    for (count, step, dest, src) in [(4, 4, &b, &a), (2, 2, &c, &b), (1, 1, &d, &c)] {
        for r in 0..count {
            g.emit(insn(
                "vmax",
                chunk(dest, r),
                vec![
                    chunk(src, r),
                    ir::crossbar("merge", chunk(src, r + step), step as i64),
                ],
                Some(16),
                &[dest.clone(), src.clone()],
            ));
        }
    }
    d
}

/// Whole symbolic operands and literal scalar/constant registers only.
fn plain(t: &Term, syms: &[Symbol]) -> bool {
    let Term::Name(n) = t else { return false };
    if syms.iter().any(|s| s.name() == *n) {
        return true;
    }
    let b = n.as_bytes();
    b.len() >= 2 && matches!(b[0], b's' | b'k') && b[1..].iter().all(u8::is_ascii_digit)
}

pub fn quantiser_order(g: &mut Gen, sym: &Symbol, into: &Symbol) -> bool {
    if crate::mutant::on("l2.vector/no-quantiser-order") {
        return false;
    }
    let mut at = None;
    for j in (0..g.code.len()).rev() {
        let Event::Op { syms, .. } = &g.code[j] else {
            continue;
        };
        if !syms.contains(sym) {
            continue;
        }
        if syms.first() == Some(sym) {
            at = Some(j);
            break;
        }
        return false;
    }
    let Some(at) = at else { return false };
    let Event::Op { stmt, syms } = &g.code[at] else {
        unreachable!()
    };
    if syms[1..].contains(sym)
        || stmt.op.starts_with("vcmp")
        || stmt.args.first() != Some(&whole(sym))
        || !stmt.args[1..].iter().all(|a| plain(a, syms))
    {
        return false;
    }
    let mut all = vec![into.clone()];
    all.extend_from_slice(&syms[1..]);
    let mut out = vec![];
    for i in 0..4 {
        for q in 0..2 {
            let srcs = stmt.args[1..]
                .iter()
                .map(|t| {
                    if let Some(s) = syms.iter().find(|s| whole(s) == *t) {
                        chunk(if s == sym { into } else { s }, 4 * q + i)
                    } else {
                        t.clone()
                    }
                })
                .collect();
            let st = ir::math(
                &stmt.op,
                chunk(into, 2 * i + q),
                srcs,
                vec![ir::kw("vl", Term::int(16))],
            );
            out.push(Event::Op {
                stmt: st,
                syms: all.clone(),
            });
        }
    }
    g.code.splice(at..at + 1, out);
    true
}
