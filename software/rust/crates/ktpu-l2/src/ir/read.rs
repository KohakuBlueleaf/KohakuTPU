//! Reading an `fn NAME.l2` body to a tree (L2.BODY.001-.019) and walking its
//! instances.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use indexmap::IndexMap;
use ktpu_l1::text::{emit_at, fmt, Env, Expander, Module, Reader, Stmt, Term, Val};

use super::repr::repr;
use super::{Body, Buffer, For, If, Node, Op, Par, Result, Type, When};
use crate::mutant::on;

/// Unit kinds a `par` may name.
pub const KINDS: [&str; 3] = ["node", "mg", "vc"];

static UID: AtomicUsize = AtomicUsize::new(1);

/// A fresh node identity.
pub fn uid() -> usize {
    UID.fetch_add(1, Ordering::Relaxed)
}

/// The text `_write` makes: the module's target line, then `fn name.l2`.
pub fn write_text(
    module: &Module,
    name: &str,
    params: &[(String, Term)],
    stmts: &[Stmt],
) -> String {
    let ps: Vec<String> = params
        .iter()
        .map(|(p, t)| format!("{p}: {}", fmt(t)))
        .collect();
    let head = if module.target.is_some() {
        format!("{}\n\n", module.target_line())
    } else {
        String::new()
    };
    format!(
        "{head}fn {name}.l2({})\n{}",
        ps.join(", "),
        emit_at(stmts, 1, 48)
    )
}

/// `fn name.l2` of `module` as a tree.
pub fn read(module: &Module, name: &str) -> Result<Body> {
    let src = module.body(name, "l2")?;
    let r = module.reader();
    let macros = module.macro_table();
    let stmts = {
        let mut ex = Expander::new(&macros, &r, on("l2.body/loops-unrolled"));
        ex.stmts(&src.stmts, &Env::new())?
    };
    let mut params = Vec::new();
    for (p, t) in &src.params {
        match Buffer::of(t) {
            Ok(b) => params.push((p.clone(), b)),
            Err(e) => {
                let at = stmts
                    .first()
                    .map(|s| (s.line, s.col))
                    .unwrap_or((src.line, 0));
                return Err(ktpu_l1::Error::at(
                    "L2.SPACE.009",
                    format!("parameter {p}: {e}"),
                    at.0,
                    at.1,
                    &r.source,
                    &r.file,
                ));
            }
        }
    }
    let mut scratch = IndexMap::new();
    let mut k = 0;
    while k < stmts.len() && stmts[k].op == "buffer" {
        let st = &stmts[k];
        let (Some(target), Some(annot)) = (st.target_name(), &st.annot) else {
            return Err(r.fail(st, "L2.BODY.010", "NAME = buffer : TYPE @SPACE(LAYOUT)"));
        };
        match Buffer::of(annot) {
            Ok(b) => {
                scratch.insert(target.to_string(), b);
            }
            Err(e) => return Err(r.fail(st, "L2.SPACE.009", e)),
        }
        k += 1;
    }
    let mut buffers: IndexMap<String, Buffer> = params.iter().cloned().collect();
    for (n, b) in &scratch {
        buffers.insert(n.clone(), b.clone());
    }
    let nodes = stmts[k..]
        .iter()
        .map(|st| node(&r, st))
        .collect::<Result<Vec<_>>>()?;
    if on("l2.body/scratch-dropped") {
        scratch = IndexMap::new();
    }
    Ok(Body {
        name: name.to_string(),
        params,
        scratch,
        stmts: nodes,
        reader: r,
        macros,
        buffers,
        vregs: module.vregs,
        vcopies: module.vcopies,
        fill: (78, 2),
    })
}

fn kids(r: &Reader, sts: &[Stmt]) -> Result<Vec<Arc<Node>>> {
    sts.iter().map(|s| node(r, s)).collect()
}

fn node(r: &Reader, st: &Stmt) -> Result<Arc<Node>> {
    let pos = st.positional();
    if st.op == "when" && st.target.is_none() {
        let cond = pos.first().map(|t| (*t).clone()).unwrap_or(Term::int(0));
        return Ok(Arc::new(Node::When(When {
            uid: uid(),
            st: st.clone(),
            cond,
            body: kids(r, &st.body)?,
        })));
    }
    if st.op == "if" && st.target.is_none() {
        if pos.len() != 1 || pos[0] != &Term::name("sticky") || st.body.is_empty() {
            return Err(r.fail(
                st,
                "L2.BODY.015",
                "if sticky (a block): the only run-time condition",
            ));
        }
        return Ok(Arc::new(Node::If(If {
            uid: uid(),
            st: st.clone(),
            cond: Term::name("sticky"),
            body: kids(r, &st.body)?,
        })));
    }
    if (st.op == "par" || st.op == "for") && st.target.is_none() {
        let (var, dom) = match pos.first() {
            Some(Term::Member(lhs, rhs)) if matches!(lhs.as_ref(), Term::Name(_)) => {
                (lhs.as_name().unwrap().to_string(), rhs.as_ref())
            }
            _ => {
                return Err(r.fail(
                    st,
                    "L2.BODY.005",
                    format!("wanted `{} VAR in LO..HI ...`", st.op),
                ))
            }
        };
        let Term::Span(lo, hi) = dom else {
            return Err(r.fail(
                st,
                "L2.BODY.005",
                format!("`{}` ranges over LO..HI, got {}", st.op, repr(dom)),
            ));
        };
        let body = kids(r, &st.body)?;
        if body.is_empty() {
            return Err(r.fail(st, "L2.BODY.005", format!("`{}` has no body", st.op)));
        }
        if st.op == "for" {
            let kw = st.kwargs();
            if pos.len() != 1
                || kw
                    .iter()
                    .any(|(k, _)| *k != "pipe" && *k != crate::passes::step::LIFTED)
            {
                return Err(r.fail(st, "L2.BODY.014", "for VAR in LO..HI [pipe=K]"));
            }
            let pipe = match st.kw("pipe") {
                None => None,
                Some(Term::Int(v, _)) if *v >= 1 => Some(*v as i64),
                Some(_) => {
                    return Err(r.fail(st, "L2.BODY.014", "pipe=K: K iterations in flight, K >= 1"))
                }
            }
            .filter(|_| !on("l2.body/pipe-dropped"));
            return Ok(Arc::new(Node::For(For {
                uid: uid(),
                st: st.clone(),
                var,
                lo: (**lo).clone(),
                hi: (**hi).clone(),
                body,
                pipe,
            })));
        }
        let unit = match (pos.len(), pos.get(1), pos.get(2)) {
            (3, Some(Term::Name(on)), Some(Term::View(n, sl))) if on == "on" => (n, sl),
            _ => return Err(r.fail(st, "L2.BODY.012", "par VAR in LO..HI on UNIT[EXPR | any]")),
        };
        let kinds: Vec<&str> = KINDS
            .iter()
            .copied()
            .filter(|k| *k != "node" || !on("l2.body/no-node-kind"))
            .collect();
        if !kinds.contains(&unit.0.as_str()) || unit.1.len() != 1 {
            let names: Vec<String> = kinds.iter().map(|k| format!("{k}[i]")).collect();
            return Err(r.fail(
                st,
                "L2.BODY.012",
                format!("a par runs on {}", names.join(", ")),
            ));
        }
        let index = if unit.1[0] == Term::name("any") {
            None
        } else {
            Some(unit.1[0].clone())
        };
        if unit.0 == "node" && index.is_none() {
            return Err(r.fail(st, "L2.BODY.012", "nodes are named: node[EXPR]"));
        }
        return Ok(Arc::new(Node::Par(Par {
            uid: uid(),
            st: st.clone(),
            var,
            lo: (**lo).clone(),
            hi: (**hi).clone(),
            kind: unit.0.clone(),
            index,
            body,
        })));
    }
    if !st.body.is_empty() {
        return Err(r.fail(st, "L2.BODY.005", format!("`{}` takes no block", st.op)));
    }
    let ty = match &st.annot {
        None => None,
        Some(t) => Some(ty_of(r, st, t)?),
    };
    Ok(Arc::new(Node::Op(Op {
        uid: uid(),
        st: st.clone(),
        op: st.op.clone(),
        target: st.target_name().map(str::to_string),
        args: st.args.clone(),
        ty,
    })))
}

fn ty_of(r: &Reader, st: &Stmt, t: &Term) -> Result<Type> {
    let bad = || {
        r.fail(
            st,
            "L2.BODY.017",
            format!("a value's type is DTYPE[shape] @SPACE, got {}", repr(t)),
        )
    };
    let Term::Placed(x, space) = t else {
        return Err(bad());
    };
    let (space, mode) = match space.as_ref() {
        Term::Call(name, args) => {
            if args.len() != 1 || args[0].as_name().is_none() {
                return Err(r.fail(
                    st,
                    "L2.BODY.017",
                    format!("@SPACE(MODE) takes one name, got {}", repr(space)),
                ));
            }
            (name.clone(), Some(args[0].as_name().unwrap().to_string()))
        }
        Term::Name(n) => (n.clone(), None),
        _ => return Err(bad()),
    };
    let Term::View(dtype, dims) = x.as_ref() else {
        return Err(bad());
    };
    let mut shape = Vec::new();
    for d in dims {
        match d {
            Term::Int(v, _) => shape.push(*v as i64),
            _ => {
                return Err(r.fail(
                    st,
                    "L2.BODY.017",
                    format!("a value's shape is constant, got {}", repr(x)),
                ))
            }
        }
    }
    Ok(Type {
        dtype: dtype.clone(),
        shape,
        space,
        mode: mode.filter(|_| !on("l2.body/type-mode-dropped")),
    })
}

// ------------------------------------------------------------------ evaluation

/// `t` with `env` bound and constants folded.
pub fn value(body: &Body, st: &Stmt, t: &Term, env: &Env) -> Result<Term> {
    Expander::new(&body.macros, &body.reader, false).term(t, env, st)
}

/// `t` as a constant.
pub fn const_of(body: &Body, st: &Stmt, t: &Term, env: &Env) -> Result<i64> {
    match value(body, st, t, env)? {
        Term::Int(v, _) => Ok(v as i64),
        v => Err(body.reader.fail(
            st,
            "L2.BODY.009",
            format!("wanted a constant here, got {}", repr(&v)),
        )),
    }
}

/// The iterations of a `par` or `for`.
pub fn span(body: &Body, st: &Stmt, lo: &Term, hi: &Term, env: &Env) -> Result<Vec<i64>> {
    let mut out: Vec<i64> = (const_of(body, st, lo, env)?..const_of(body, st, hi, env)?).collect();
    if on("l2.body/span-short") {
        out.pop();
    }
    if on("l2.body/span-reversed") {
        out.reverse();
    }
    Ok(out)
}

/// `env` with `var` bound to `v`.
pub fn bind(env: &Env, var: &str, v: i64) -> Env {
    let mut e = env.clone();
    e.insert(var.to_string(), Val::Int(v as i128));
    e
}

/// The integer `env` binds `var` to.
pub fn env_int(env: &Env, var: &str) -> Option<i64> {
    match env.get(var) {
        Some(Val::Int(v)) => Some(*v as i64),
        _ => None,
    }
}

/// One item of an instance: an op or an `if sticky` block, with its env.
pub type Item = (Arc<Node>, Env);

/// One `par` instance: its unit, the node it is on, the variables bound, and
/// its items in program order, each `for` unrolled.
#[derive(Clone, Debug)]
pub struct Instance {
    pub kind: String,
    pub index: Option<i64>,
    pub node: i64,
    pub env: Env,
    pub par: Arc<Node>,
    pub items: Vec<Item>,
    /// item index where each iteration of the outermost `for` starts
    pub cuts: Vec<usize>,
}

impl Instance {
    pub fn par(&self) -> &Par {
        match self.par.as_ref() {
            Node::Par(p) => p,
            _ => unreachable!("an instance's par"),
        }
    }
}

/// What a stage holds: the concurrent instances of one `par`, or one
/// node-scope op with its env.
#[derive(Clone, Debug)]
pub enum StageX {
    Par(Vec<Instance>),
    Op(Arc<Node>, Env),
}

/// One stage: what it holds, its node, its `pipe` group (loop uid,
/// iteration, K) and its phase.
#[derive(Clone, Debug)]
pub struct Stage {
    pub x: StageX,
    pub node: i64,
    pub tag: Option<(usize, usize, i64)>,
    pub phase: i64,
}

#[derive(Default)]
struct Phases {
    next: i64,
    cur: Option<i64>,
}

/// Every top-level stage of `body` (L2.BODY.009, .019).
pub fn instances(body: &Body) -> Result<Vec<Stage>> {
    let mut out = instances_in(body, &body.stmts, &Env::new())?;
    if on("l2.body/phase-per-stage") {
        for (k, s) in out.iter_mut().enumerate() {
            s.phase = k as i64;
        }
    }
    Ok(out)
}

/// The stages under `stmts` with `env` bound.
pub fn instances_in(body: &Body, stmts: &[Arc<Node>], env: &Env) -> Result<Vec<Stage>> {
    let mut out = Vec::new();
    walk(body, stmts, env, 0, None, &mut Phases::default(), &mut out)?;
    Ok(out)
}

fn walk(
    body: &Body,
    stmts: &[Arc<Node>],
    env: &Env,
    node: i64,
    tag: Option<(usize, usize, i64)>,
    ph: &mut Phases,
    out: &mut Vec<Stage>,
) -> Result<()> {
    for s in stmts {
        match s.as_ref() {
            Node::Par(p) if p.kind == "node" => {
                let outer = ph.cur.is_none();
                if outer {
                    ph.cur = Some(ph.next);
                    ph.next += 1;
                }
                for v in span(body, &p.st, &p.lo, &p.hi, env)? {
                    let inner = bind(env, &p.var, v);
                    let n = const_of(body, &p.st, p.index.as_ref().unwrap(), &inner)?;
                    walk(body, &p.body, &inner, n, tag, ph, out)?;
                }
                if outer {
                    ph.cur = None;
                }
            }
            Node::Par(p) => {
                let mut stage = Vec::new();
                for v in span(body, &p.st, &p.lo, &p.hi, env)? {
                    let inner = bind(env, &p.var, v);
                    let idx = match &p.index {
                        None => None,
                        Some(t) => Some(const_of(body, &p.st, t, &inner)?),
                    };
                    let mut inst = Instance {
                        kind: p.kind.clone(),
                        index: idx,
                        node,
                        env: inner.clone(),
                        par: s.clone(),
                        items: Vec::new(),
                        cuts: Vec::new(),
                    };
                    flatten(body, &p.body, &inner, &mut inst, true)?;
                    stage.push(inst);
                }
                emit(out, ph, StageX::Par(stage), node, tag);
            }
            Node::For(f) => {
                for (k, v) in span(body, &f.st, &f.lo, &f.hi, env)?
                    .into_iter()
                    .enumerate()
                {
                    let inner_tag = match f.pipe {
                        Some(p) => Some((f.uid, k, p)),
                        None => tag,
                    };
                    walk(
                        body,
                        &f.body,
                        &bind(env, &f.var, v),
                        node,
                        inner_tag,
                        ph,
                        out,
                    )?;
                }
            }
            Node::When(w) => {
                if const_of(body, &w.st, &w.cond, env)? != 0 {
                    walk(body, &w.body, env, node, tag, ph, out)?;
                }
            }
            Node::If(i) => {
                return Err(body.reader.fail(
                    &i.st,
                    "L2.BODY.015",
                    "if sticky is a vector core's (its stream loop)",
                ))
            }
            Node::Op(_) => emit(out, ph, StageX::Op(s.clone(), env.clone()), node, tag),
        }
    }
    Ok(())
}

fn emit(
    out: &mut Vec<Stage>,
    ph: &mut Phases,
    x: StageX,
    node: i64,
    tag: Option<(usize, usize, i64)>,
) {
    let phase = match ph.cur {
        None => {
            let p = ph.next;
            ph.next += 1;
            p
        }
        Some(c) => c,
    };
    out.push(Stage {
        x,
        node,
        tag,
        phase,
    });
}

fn flatten(
    body: &Body,
    stmts: &[Arc<Node>],
    env: &Env,
    inst: &mut Instance,
    top: bool,
) -> Result<()> {
    for s in stmts {
        match s.as_ref() {
            Node::Par(p) => {
                return Err(body.reader.fail(
                    &p.st,
                    "L2.BODY.016",
                    "a par inside a unit's instance",
                ))
            }
            Node::If(i) => {
                if inst.kind != "vc" {
                    return Err(body.reader.fail(
                        &i.st,
                        "L2.BODY.015",
                        "if sticky is a vector core's (its stream loop)",
                    ));
                }
                inst.items.push((s.clone(), env.clone()));
            }
            Node::When(w) => {
                if const_of(body, &w.st, &w.cond, env)? != 0 {
                    flatten(body, &w.body, env, inst, top)?;
                }
            }
            Node::For(f) => {
                for v in span(body, &f.st, &f.lo, &f.hi, env)? {
                    if top {
                        inst.cuts.push(inst.items.len());
                    }
                    flatten(body, &f.body, &bind(env, &f.var, v), inst, false)?;
                }
            }
            Node::Op(_) => inst.items.push((s.clone(), env.clone())),
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ir::Program;

    fn prog(text: &str) -> Program {
        Program::parse(text, "<t>", None).unwrap()
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_BODY_001_scratch_first() {
        let p = prog(
            "fn k.l2(x: f16[64, 128] @dram)\n    h = buffer : f16[64, 128] @dram\n    copy h <- x\n",
        );
        let b = p.body().unwrap();
        assert_eq!(b.scratch.keys().collect::<Vec<_>>(), vec!["h"]);
        assert_eq!(b.stmts.len(), 1);
        assert_eq!(b.buffers.len(), 2);
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_BODY_010_buffer_form() {
        let p = prog("fn k.l2(x: f16[64] @dram)\n    buffer : f16[64] @dram\n    copy x <- x\n");
        let e = p.body().unwrap_err();
        assert_eq!(e.clause, "L2.BODY.010");
        assert_eq!(e.message, "NAME = buffer : TYPE @SPACE(LAYOUT)");
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_BODY_009_stages() {
        let p = prog(concat!(
            "fn k.l2(x: f16[64, 128] @dram, y: f16[64, 128] @dram)\n",
            "    par c in 0..2 on vc[c]\n",
            "        for t in 0..2\n",
            "            xl = load x[c*32 + t*16 : +16, :] : f16[16, 128] @l1\n",
            "    copy y <- x\n",
        ));
        let b = p.body().unwrap();
        let st = instances(&b).unwrap();
        assert_eq!(st.len(), 2);
        let StageX::Par(insts) = &st[0].x else {
            panic!()
        };
        assert_eq!(insts.len(), 2);
        assert_eq!(insts[1].index, Some(1));
        assert_eq!(insts[1].items.len(), 2);
        assert_eq!(insts[1].cuts, vec![0, 1]);
        assert_eq!((st[0].phase, st[1].phase), (0, 1));
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_BODY_012_units() {
        let p = prog(
            "fn k.l2(x: f16[64] @dram)\n    par n in 0..2 on node[any]\n        copy x <- x\n",
        );
        let e = p.body().unwrap_err();
        assert_eq!(
            (e.clause.as_str(), e.message.as_str()),
            ("L2.BODY.012", "nodes are named: node[EXPR]")
        );
        let p =
            prog("fn k.l2(x: f16[64] @dram)\n    par n in 0..2 on sr[n]\n        copy x <- x\n");
        assert_eq!(
            p.body().unwrap_err().message,
            "a par runs on node[i], mg[i], vc[i]"
        );
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_BODY_014_pipe() {
        let p = prog("fn k.l2(x: f16[64] @dram)\n    for q in 0..2 pipe=0\n        copy x <- x\n");
        assert_eq!(
            p.body().unwrap_err().message,
            "pipe=K: K iterations in flight, K >= 1"
        );
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_BODY_019_node_phase() {
        let p = prog(concat!(
            "fn k.l2(x: f16[64, 128] @dram)\n",
            "    par n in 0..2 on node[n]\n",
            "        par c in 0..2 on vc[c]\n",
            "            xl = load x[n*32 + c*16 : +16, :] : f16[16, 128] @l1\n",
            "    for q in 0..2 pipe=2\n",
            "        copy x <- x\n",
        ));
        let b = p.body().unwrap();
        let st = instances(&b).unwrap();
        let phases: Vec<i64> = st.iter().map(|s| s.phase).collect();
        assert_eq!(phases, vec![0, 0, 1, 2]);
        assert_eq!((st[0].node, st[1].node), (0, 1));
        assert_eq!(st[3].tag.map(|t| (t.1, t.2)), Some((1, 2)));
    }
}
