//! Reduction trees, statistic packing and the quantiser's chunk order
//! (L2.OP.011-.015, L2.STAT.003).

#[cfg(feature = "witness")]
use super::gen::{Code, Gen};

pub(super) mod typed;

/// One instruction of a tree step: template and symbols.
#[cfg(feature = "witness")]
pub type Insn = (String, Vec<String>);
/// A tree as steps of independent instructions.
#[cfg(feature = "witness")]
pub type Steps = Vec<Vec<Insn>>;

#[cfg(feature = "witness")]
fn insn(t: impl Into<String>, syms: &[&str]) -> Insn {
    (t.into(), syms.iter().map(|s| s.to_string()).collect())
}

/// Several trees' steps, step by step: each step's instructions of every
/// tree before the next step.
#[cfg(feature = "witness")]
pub fn interleave(g: &mut Gen, trees: &[Steps]) {
    for s in 0..trees[0].len() {
        let longest = trees.iter().map(|t| t[s].len()).max().unwrap_or(0);
        for i in 0..longest {
            for t in trees {
                if i < t[s].len() {
                    let (tm, sy) = &t[s][i];
                    g.emit_owned(tm.clone(), sy.clone());
                }
            }
        }
    }
}

/// A 4-row band's reduction over its GT4 registers: pairwise folds, chunk
/// 4 + i onto chunk i, an xor butterfly over the 16 lanes, chunks 0-3 copied
/// to 4-7. With `fused`, `regs` are (a, b) pairs whose products are summed.
#[cfg(feature = "witness")]
pub fn tree4(
    g: &mut Gen,
    code: &str,
    regs: &[Vec<String>],
    into: Option<String>,
    fused: bool,
) -> (Steps, String) {
    let mut steps: Steps = Vec::new();
    let mut syms: Vec<String>;
    if fused {
        let (mut muls, mut fmas, mut nxt) = (Vec::new(), Vec::new(), Vec::new());
        let mut k = 0;
        while k < regs.len() {
            let d = g.sym();
            muls.push(insn("vmul {0}, {1}, {2}", &[&d, &regs[k][0], &regs[k][1]]));
            if k + 1 < regs.len() {
                fmas.push(insn(
                    "vfma {0}, {1}, {2}, {0}",
                    &[&d, &regs[k + 1][0], &regs[k + 1][1]],
                ));
            }
            nxt.push(d);
            k += 2;
        }
        muls.extend(fmas);
        steps.push(muls);
        syms = nxt;
    } else {
        syms = regs.iter().map(|r| r[0].clone()).collect();
    }
    while syms.len() > 1 {
        let (mut level, mut nxt) = (Vec::new(), Vec::new());
        let mut k = 0;
        while k + 1 < syms.len() {
            let d = g.sym();
            level.push(insn(
                format!("{code} {{0}}, {{1}}, {{2}}"),
                &[&d, &syms[k], &syms[k + 1]],
            ));
            nxt.push(d);
            k += 2;
        }
        steps.push(level);
        if syms.len() % 2 == 1 {
            nxt.push(syms.last().unwrap().clone());
        }
        syms = nxt;
    }
    let d = into.unwrap_or_else(|| g.sym());
    steps.push(vec![insn(
        format!("{code} {{0}}[0:], {{1}}[0:], {{1}}[4:] vl=64"),
        &[&d, &syms[0]],
    )]);
    for k in [8, 4, 2, 1] {
        steps.push(vec![insn(
            format!("{code} {{0}}[0:], {{0}}[0:], xor({{0}}, {k}) vl=64"),
            &[&d],
        )]);
    }
    if !crate::mutant::on("l2.vector/band-no-broadcast") {
        steps.push(vec![insn("vmov {0}[4:], {0}[0:] vl=64", &[&d])]);
    }
    (steps, d)
}

/// The merge tree over 8 rows: the symbol whose chunk 0 lane r is row r's
/// reduction. `rows` are row registers, or with `fused` (a, b) pairs.
#[cfg(feature = "witness")]
pub fn tree(
    g: &mut Gen,
    code: &str,
    rows: &[Vec<String>],
    fused: bool,
    into: Option<String>,
) -> (Steps, String) {
    let t: Vec<String> = (0..4).map(|_| g.sym()).collect();
    let mut first = Vec::new();
    for i in 0..8 {
        let c = 4 * (i / 4);
        let x = &rows[2 * (i % 4) + i / 4];
        if fused {
            let (a, b) = (&x[0], &x[1]);
            first.push(insn(
                format!("vmul {{0}}[{c}:], {{1}}[4:], {{2}}[4:] vl=64"),
                &[&t[i % 4], a, b],
            ));
            first.push(insn(
                format!("vfma {{0}}[{c}:], {{1}}[0:], {{2}}[0:], {{0}}[{c}:] vl=64"),
                &[&t[i % 4], a, b],
            ));
        } else {
            first.push(insn(
                format!("{code} {{0}}[{c}:], {{1}}[0:], {{1}}[4:] vl=64"),
                &[&t[i % 4], &x[0]],
            ));
        }
    }
    if fused {
        let evens: Vec<Insn> = first.iter().step_by(2).cloned().collect();
        let odds: Vec<Insn> = first.iter().skip(1).step_by(2).cloned().collect();
        first = evens.into_iter().chain(odds).collect();
    }
    let mut second = Vec::new();
    for i in 0..8 {
        let c = 4 * (i / 4);
        second.push(insn(
            format!("{code} {{0}}[{c}:], {{0}}[{c}:], {{0}}[{}:] vl=32", c + 2),
            &[&t[i % 4]],
        ));
    }
    let acc = g.sym();
    let mut third = Vec::new();
    for r in 0..8 {
        let c = 4 * (r % 2);
        third.push(insn(
            format!("{code} {{0}}[{r}:], {{1}}[{c}:], {{1}}[{}:] vl=16", c + 1),
            &[&acc, &t[r / 2]],
        ));
    }
    let m = g.sym();
    let d = into.unwrap_or_else(|| g.sym());
    let steps = vec![
        first,
        second,
        third,
        vec![insn(
            format!("{code} {{0}}, {{1}}, merge({{1}}, 8)"),
            &[&m, &acc],
        )],
        vec![insn(
            format!("{code} {{0}}, {{0}}, merge({{0}}[4:], 4) vl=64"),
            &[&m],
        )],
        vec![insn(
            format!("{code} {{0}}, {{0}}, merge({{0}}[2:], 2) vl=32"),
            &[&m],
        )],
        vec![insn(
            format!("{code} {{0}}[0:], {{1}}, merge({{1}}[1:], 1) vl=16"),
            &[&d, &m],
        )],
    ];
    (steps, d)
}

/// The symbol whose chunk 0 lane r is row r of four GT4 band statistics:
/// rows r and r + 8 merged by lane bit 3, then 4, 2, 1 (15 one-chunk ops).
#[cfg(feature = "witness")]
pub fn compact(g: &mut Gen, bands: &[String]) -> String {
    let reversed: Vec<String> = bands.iter().rev().cloned().collect();
    let bands: &[String] = if crate::mutant::on("l2.vector/compact-reversed") {
        &reversed
    } else {
        bands
    };
    let (a, b, c, d) = (g.sym(), g.sym(), g.sym(), g.sym());
    for r in 0..8 {
        let (lo, hi) = (&bands[r / 4], &bands[(r + 8) / 4]);
        g.emit(
            format!(
                "vmax {{0}}[{r}:], {{1}}[{}:], merge({{2}}[{}:], 8) vl=16",
                r % 4,
                r % 4
            ),
            &[&a, lo, hi],
        );
    }
    for r in 0..4 {
        g.emit(
            format!(
                "vmax {{0}}[{r}:], {{1}}[{r}:], merge({{1}}[{}:], 4) vl=16",
                r + 4
            ),
            &[&b, &a],
        );
    }
    for r in 0..2 {
        g.emit(
            format!(
                "vmax {{0}}[{r}:], {{1}}[{r}:], merge({{1}}[{}:], 2) vl=16",
                r + 2
            ),
            &[&c, &b],
        );
    }
    g.emit("vmax {0}[0:], {1}[0:], merge({1}[1:], 1) vl=16", &[&d, &c]);
    d
}

#[cfg(feature = "witness")]
fn plain(p: &str) -> bool {
    let b = p.as_bytes();
    if b.len() >= 3 && b[0] == b'{' && b[b.len() - 1] == b'}' {
        return b[1..b.len() - 1].iter().all(|c| c.is_ascii_digit());
    }
    b.len() >= 2 && (b[0] == b's' || b[0] == b'k') && b[1..].iter().all(|c| c.is_ascii_digit())
}

/// The full-register op that made `sym` (read by nothing else) rewritten as
/// eight one-chunk ops writing `into` in the quantiser's chunk order (chunk
/// 2i + q from GT4 chunk 4q + i); false when no such op is found.
#[cfg(feature = "witness")]
pub fn quantiser_order(g: &mut Gen, sym: &str, into: &str) -> bool {
    if crate::mutant::on("l2.vector/no-quantiser-order") {
        return false;
    }
    let mut at = None;
    for j in (0..g.code.len()).rev() {
        let Code::Op(_, syms) = &g.code[j] else {
            continue;
        };
        if !syms.iter().any(|s| s == sym) {
            continue;
        }
        if syms[0] == sym {
            at = Some(j);
            break;
        }
        return false;
    }
    let Some(at) = at else { return false };
    let Code::Op(tmpl, syms) = g.code[at].clone() else {
        unreachable!()
    };
    if tmpl.contains(" vl=")
        || tmpl.contains(" if=")
        || tmpl.contains('[')
        || syms[1..].iter().any(|s| s == sym)
    {
        return false;
    }
    let (op, rest) = tmpl.split_once(' ').unwrap_or((&tmpl, ""));
    let parts: Vec<String> = rest.split(',').map(|p| p.trim().to_string()).collect();
    if op.starts_with("vcmp") || parts[0] != "{0}" || !parts[1..].iter().all(|p| plain(p)) {
        return false;
    }
    let mut srcs = vec![into.to_string()];
    srcs.extend(syms[1..].iter().cloned());
    let mut out = Vec::new();
    for i in 0..4 {
        for q in 0..2 {
            let ops: Vec<String> = parts[1..]
                .iter()
                .map(|p| {
                    if p.starts_with('{') {
                        format!("{{{}}}[{}:]", &p[1..p.len() - 1], 4 * q + i)
                    } else {
                        p.clone()
                    }
                })
                .collect();
            out.push(Code::Op(
                format!("{op} {{0}}[{}:], {} vl=16", 2 * i + q, ops.join(", ")),
                srcs.clone(),
            ));
        }
    }
    g.code.splice(at..at + 1, out);
    true
}
