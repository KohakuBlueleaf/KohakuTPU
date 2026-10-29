//! Parallel typed constants and prologue, exercised against template emission.

use super::*;
use crate::lower::vector::gen::typed::{self as ir, Event, Select, Symbol};

mod iteration;
#[cfg(all(test, feature = "witness"))]
mod tests;

#[derive(Default)]
pub(in crate::lower::vector::ctx) struct Emitter {
    pub(in crate::lower::vector::ctx) n: u32,
    pub(in crate::lower::vector::ctx) code: Vec<Event>,
}

impl Emitter {
    pub(in crate::lower::vector::ctx) fn sym(&mut self) -> Symbol {
        self.n += 1;
        Symbol(self.n)
    }

    pub(in crate::lower::vector::ctx) fn fixed(&mut self, st: Stmt) {
        if on("isa.vector/lower") && st.op == "getstk" {
            return;
        }
        self.code.push(Event::Fixed(st));
    }

    pub(in crate::lower::vector::ctx) fn op(&mut self, st: Stmt, syms: Vec<Symbol>) {
        if on("isa.vector/lower") && st.kw("if") == Some(&Term::name("p1")) {
            return;
        }
        self.code.push(Event::Op { stmt: st, syms });
    }
}

fn constant_seti(register: &str, value: f64) -> Stmt {
    let term = if value.is_nan() || !value.is_sign_negative() {
        Term::Float(value)
    } else {
        Term::Neg(Box::new(Term::Float(-value)))
    };
    ir::seti(register, term)
}

pub(in crate::lower::vector::ctx) fn symbol(name: &str) -> Symbol {
    Symbol(name.strip_prefix('%').unwrap().parse().unwrap())
}

impl Ctx<'_> {
    pub(in crate::lower::vector::ctx) fn constants_typed(
        &mut self,
        g: &mut Emitter,
        ops: &[Arc<Node>],
    ) {
        let fs = numbers(ops, self.body);
        let stk = self.t.stk_s() as usize;
        let kept = if fs.len() <= stk { fs.len() } else { stk - 1 };
        for (i, f) in fs[..kept].iter().enumerate() {
            let reg = format!("s{i}");
            self.scalars.insert(FKey::of(*f), reg.clone());
            g.fixed(constant_seti(&reg, *f));
        }
        if self.hold {
            let stage = stage_regs(self.t);
            for f in &fs[kept..] {
                let v = g.sym();
                self.pinned.insert(v.name());
                self.held.insert(FKey::of(*f), v.name());
                g.fixed(constant_seti(&stage[0], *f));
                g.op(
                    ir::math(
                        "vmov",
                        v.operand(Select::Whole),
                        vec![Term::name(&stage[0])],
                        vec![],
                    ),
                    vec![v],
                );
            }
        }
    }

    pub(in crate::lower::vector::ctx) fn constant_typed(
        &mut self,
        g: &mut Emitter,
        f: f64,
        syms: &mut Vec<Symbol>,
        srcs: &[Term],
        sel: Select,
    ) -> VResult<Term> {
        let k = FKey::of(f);
        if let Some(v) = self.held.get(&k) {
            let v = symbol(v);
            let operand = v.operand(sel);
            syms.push(v);
            return Ok(operand);
        }
        if let Some(s) = kreg(f) {
            return Ok(Term::name(s));
        }
        if let Some(s) = self.scalars.get(&k) {
            return Ok(Term::name(s));
        }
        for (reg, (got, at)) in &self.staged {
            let crossed = g.code[*at..].iter().any(Event::is_block_edge)
                && !crate::mutant::on("l2.vector/staged-across-blocks");
            if *got == k && !crossed {
                return Ok(Term::name(reg));
            }
        }
        let stage = stage_regs(self.t);
        let Some(reg) = stage
            .iter()
            .find(|r| !srcs.contains(&Term::name(*r)))
            .cloned()
        else {
            return Err(VErr::Pressure);
        };
        g.fixed(constant_seti(&reg, f));
        self.staged.insert(reg.clone(), (k, g.code.len()));
        Ok(Term::name(reg))
    }

    pub(in crate::lower::vector::ctx) fn prologue_typed(
        &mut self,
        g: &mut Emitter,
        n: &Node,
    ) -> VResult<()> {
        let o = op(n);
        match o.op.as_str() {
            "recv" => {
                let PlanX::T(x) = &self.plan[&o.uid] else {
                    unreachable!()
                };
                g.fixed(ir::wait("unpack", "rx", x.size));
                let mut v =
                    Val::new("l1", count_of(ty(o), self.t.vlmax)).with_mode(mode_of(o.ty.as_ref()));
                v.buf = x.at;
                self.vals.insert(o.target.clone().unwrap(), v);
            }
            "load" => {
                let d = &self.descs[self.of[&o.uid]];
                g.fixed(
                    ir::memory(
                        ir::Memory::Fill,
                        Term::name(format!("a{}", d.ad)),
                        d.buf[0],
                        None,
                        false,
                        vec![],
                    )
                    .unwrap(),
                );
                g.fixed(ir::instruction("vbar", vec![], vec![]));
                let v = l1val(Some(d), ty(o), d.buf[0], self.t);
                self.vals.insert(o.target.clone().unwrap(), v);
            }
            "unpack" => {
                let src = self.get(o, o.st.positional()[0], Some("l1"), None)?;
                if ty(o).shape.len() == 1 {
                    return Err(self.fail(
                        &o.st,
                        "L2.STAT.006",
                        "a row statistic unpacks in the stream loop",
                    ));
                }
                let mut v = Val::new("row", count_of(ty(o), self.t.vlmax)).with_mode(&src.mode);
                v.cols = *ty(o).shape.last().unwrap();
                if self.pin {
                    for r in 0..v.rows {
                        let s = g.sym();
                        v.regs.insert(r, s.name());
                        self.pinned.insert(s.name());
                        g.op(unpack_line_typed(&src, r, &s), vec![s]);
                    }
                }
                self.vals.insert(o.target.clone().unwrap(), v);
            }
            other => {
                return Err(self.fail(
                    &o.st,
                    "L2.VC.018",
                    format!("before the stream loop: load and unpack, not {other}"),
                ))
            }
        }
        Ok(())
    }
}
