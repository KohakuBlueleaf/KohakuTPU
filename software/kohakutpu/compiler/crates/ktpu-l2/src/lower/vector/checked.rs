//! Checked MXFP7 packs of a stream instance (L2.VC.028): values classified into
//! the sticky latch, folded into the status register before every HALT.

use std::sync::Arc;

use ktpu_l1::text::{Env, Stmt, Term};
use ktpu_machine::packing::STATUS_RECORD;

use super::gen::typed as ir;
use super::{ops_of, resolve, split};
use crate::ir::{Body, Node, Op, Par, Result};
use crate::lower::mover::quant_status::{region, Status, STATUS_V};
use crate::target::Target;

/// The mutant that packs a checked value unclassified.
pub const UNCHECKED: &str = "l2.vector/unchecked-pack";

/// Whether `op` is a checked pack (`pack ... status=VIEW`).
pub fn checked(op: &Op) -> bool {
    op.op == "pack" && op.st.kw("status").is_some()
}

/// Whether any of `ops` is a checked pack.
pub fn holds(ops: &[Arc<Node>]) -> bool {
    ops.iter()
        .any(|n| matches!(n.as_ref(), Node::Op(o) if checked(o)))
}

/// The registers a checked instance allocates: the status register is the core's.
pub fn registers(vregs: u32, checked: bool) -> u32 {
    if checked {
        vregs.min(STATUS_V)
    } else {
        vregs
    }
}

/// The fold of the sticky latch into the status register that precedes every HALT
/// of a checked image: a RUN clears the latch, not the register file.
pub fn fold(t: &Target) -> Vec<Stmt> {
    let (s, v) = (format!("s{}", t.stk_s()), format!("v{STATUS_V}"));
    vec![
        ir::sync("fe", "math", None),
        ir::instruction("getstk", vec![Term::name(&s)], vec![]),
        ir::math(
            "vmax",
            Term::name(&v),
            vec![Term::name(&v), Term::name(&s)],
            vec![],
        ),
        ir::sync("fe", "math", None),
    ]
}

/// Whether `stmts[at..]` begins with a [`fold`].
pub fn folds_at(stmts: &[Stmt], at: usize) -> bool {
    let v = Term::name(format!("v{STATUS_V}"));
    stmts.len() >= at + 4
        && stmts[at].op == "vsync"
        && stmts[at + 1].op == "getstk"
        && stmts[at + 2].op == "vmax"
        && stmts[at + 2].args.first() == Some(&v)
        && stmts[at + 3].op == "vsync"
}

/// The one whole record an instance's checked MXFP7 packs name, if any; no other
/// op of the instance raises or reads its sticky latch.
pub fn record(body: &Body, par: &Par, env: &Env, t: &Target) -> Result<Option<Status>> {
    let rp = resolve(body, par, env)?;
    let (pre, lp) = split(body, &rp)?;
    let mut ops = pre;
    if let Some(lp) = &lp {
        ops.extend(ops_of(&lp.body));
    }
    let fail = |st: &Stmt, msg: &str| body.reader.fail(st, "L2.VC.028", msg.to_string());
    let mut found: Option<(Status, Stmt)> = None;
    for n in &ops {
        let Node::Op(o) = n.as_ref() else { continue };
        if !checked(o) {
            continue;
        }
        if !o
            .ty
            .as_ref()
            .is_some_and(|ty| ty.dtype == "mx7a" || ty.dtype == "mx7b")
        {
            return Err(fail(&o.st, "a checked pack packs MXFP7"));
        }
        let s = region(body, &o.st, o.st.kw("status").unwrap(), env, t)?;
        if s.bytes != STATUS_RECORD {
            return Err(fail(
                &o.st,
                &format!("a checked pack's status is one {STATUS_RECORD}-byte record"),
            ));
        }
        match &found {
            Some((f, _)) if f.name != s.name || f.offset != s.offset => {
                return Err(fail(&o.st, "an instance's checked packs name one record"));
            }
            Some(_) => {}
            None => found = Some((s, o.st.clone())),
        }
    }
    let Some((s, st)) = found else {
        return Ok(None);
    };
    let latched = lp
        .as_ref()
        .is_some_and(|lp| lp.body.iter().any(|n| matches!(n.as_ref(), Node::If(_))))
        || ops
            .iter()
            .any(|n| matches!(n.as_ref(), Node::Op(o) if o.op == "exp2d"));
    if latched {
        return Err(fail(
            &st,
            "a checked pack's instance holds no exp2d or if sticky: its sticky latch is the finite status",
        ));
    }
    let vc = &t.machine.vc;
    if t.machine.fw.status.get("KA_ST_CONTRACT") != Some(&0x24)
        || t.vregs <= STATUS_V
        || t.sregs < 15
        || t.descriptors < 3
        || vc.tau != 8
        || t.l1_words < crate::lower::mover::quant_status::DRAIN_L1.1
    {
        return Err(fail(
            &st,
            "a checked pack requires contract completion, 32 registers, three descriptors and EXP2D tau=8",
        ));
    }
    Ok(Some(s))
}
