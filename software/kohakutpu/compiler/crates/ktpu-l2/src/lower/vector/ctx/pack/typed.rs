//! Typed packs and compact statistics.

use super::*;
use crate::lower::vector::ctx::emit::typed::{symbol, Emitter};
use crate::lower::vector::gen::typed::{self as ir, Select, Symbol};
use crate::lower::vector::tree::typed as tt;
use crate::lower::vector::{mx7_pack, zero_lift_value};

// Tree helpers retain their own generator; move events without cloning or parsing.
fn with_tree<T>(g: &mut Emitter, f: impl FnOnce(&mut tt::Gen) -> T) -> T {
    let mut tree = tt::Gen {
        n: g.n,
        code: std::mem::take(&mut g.code),
    };
    let result = f(&mut tree);
    g.n = tree.n;
    g.code = tree.code;
    result
}

impl Ctx<'_> {
    pub(in crate::lower::vector::ctx) fn packs_typed(
        &mut self,
        g: &mut Emitter,
        ops: &[Arc<Node>],
        rows: &[i64],
        local: &mut IndexMap<String, Val>,
        parity: usize,
    ) -> VResult<()> {
        for n in ops {
            let o = op(n);
            if o.op != "pack" {
                continue;
            }
            let arg = o.st.positional()[0].clone();
            let stat = self.get(o, &arg, None, Some(local))?;
            if stat.kind == "stat" {
                self.pack_stat_typed(g, o, ops, &stat, rows, local, parity)?;
                continue;
            }
            let src = self.get(o, &arg, Some("row"), Some(local))?;
            let target = o.target.clone().unwrap();
            let dests = self.store_dests(ops, &target, true);
            if dests.is_empty() {
                return Err(self.fail(&o.st, "L2.OP.025", "a packed value is stored or sent"));
            }
            let buf = self.descs[dests[0]].buf[parity];
            let t = ty(o);
            if t.dtype == "mx7a" || t.dtype == "mx7b" {
                if src.mode != "gt4" {
                    return Err(self.fail(
                        &o.st,
                        "L2.OP.026",
                        "an MXFP7 pack takes 4 x 32 blocks: a @reg(gt4) value",
                    ));
                }
                let only = read_once(ops, o);
                let lift = mx7_pack(o, ops, self.body);
                for &r in rows {
                    let tsym = g.sym();
                    let reg = symbol(&src.regs[&r]);
                    // Zeros lift in the move to quantiser order, or in place.
                    let ordered = only && with_tree(g, |g| tt::quantiser_order(g, &reg, &tsym));
                    if !ordered {
                        for i in 0..4 {
                            for q in 0..2 {
                                let (s, d) = (Select::Stride(4 * q + i), Select::Stride(2 * i + q));
                                let mut syms = vec![tsym.clone(), reg.clone()];
                                let args = if lift {
                                    let z = self.constant_typed(
                                        g,
                                        zero_lift_value(),
                                        &mut syms,
                                        &[reg.operand(s)],
                                        s,
                                    )?;
                                    vec![reg.operand(s), z, reg.operand(s)]
                                } else {
                                    vec![reg.operand(s)]
                                };
                                g.op(
                                    ir::math(
                                        if lift { "vsel" } else { "vmov" },
                                        tsym.operand(d),
                                        args,
                                        vec![ir::kw("vl", Term::int(16))],
                                    ),
                                    syms,
                                );
                            }
                        }
                    } else if lift {
                        let mut syms = vec![tsym.clone()];
                        let w = tsym.operand(Select::Whole);
                        let z = self.constant_typed(
                            g,
                            zero_lift_value(),
                            &mut syms,
                            std::slice::from_ref(&w),
                            Select::Whole,
                        )?;
                        g.op(
                            ir::math("vsel", w.clone(), vec![w.clone(), z, w], vec![]),
                            syms,
                        );
                    }
                    for &d in &dests {
                        let at = self.descs[d].buf[parity] + r * 4;
                        g.op(
                            ir::memory(
                                ir::Memory::Pack,
                                tsym.operand(Select::Whole),
                                at,
                                Some(&t.dtype),
                                false,
                                vec![],
                            )
                            .unwrap(),
                            vec![tsym.clone()],
                        );
                    }
                }
                let mut v = Val::new("l1", count_of(t, self.t.vlmax)).with_mode(&t.dtype);
                v.buf = buf;
                local.insert(target, v);
                continue;
            }
            let mode = mode_of(Some(t));
            let packed = if crate::mutant::on("l2.vector/pack-first-dest") {
                &dests[..1]
            } else {
                &dests[..]
            };
            for &d in packed {
                for (_, i) in self.once(&src, rows)? {
                    let s = symbol(&src.regs[&i]);
                    let line = pack_line_typed(
                        &self.descs[d],
                        self.descs[d].buf[parity],
                        i,
                        mode,
                        &t.dtype,
                        &s,
                    );
                    g.op(line, vec![s]);
                }
            }
            let mut v = Val::new("l1", count_of(t, self.t.vlmax)).with_mode(mode);
            v.buf = buf;
            local.insert(target, v);
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn pack_stat_typed(
        &mut self,
        g: &mut Emitter,
        o: &Op,
        ops: &[Arc<Node>],
        stat: &Val,
        rows: &[i64],
        local: &mut IndexMap<String, Val>,
        parity: usize,
    ) -> VResult<()> {
        let t = ty(o);
        if t.shape.len() != 1 {
            if !crate::mutant::on("l2.vector/statistic-band-pack") {
                return Err(self.fail(&o.st, "RUL.030", crate::lower::vector::RUL_030));
            }
            let target = o.target.clone().unwrap();
            let dests = self.store_dests(ops, &target, false);
            let bands: std::collections::BTreeSet<_> =
                rows.iter().map(|r| r / self.per_band).collect();
            for &d in &dests {
                for &b in &bands {
                    let s = symbol(&stat.regs[&b]);
                    g.op(
                        pack_line_typed(
                            &self.descs[d],
                            self.descs[d].buf[parity],
                            b,
                            "gt4",
                            "f16",
                            &s,
                        ),
                        vec![s],
                    );
                }
            }
            let mut v = Val::new("l1", bands.len() as i64).with_mode("gt4");
            v.buf = self.descs[dests[0]].buf[parity];
            local.insert(target, v);
            return Ok(());
        }
        if self.per_band == 0 {
            return Err(self.fail(
                &o.st,
                "L2.STAT.008",
                "a statistic packs from GT4 rows as f16[R] @l1",
            ));
        }
        let target = o.target.clone().unwrap();
        let dests = self.store_dests(ops, &target, false);
        if dests.is_empty() {
            return Err(self.fail(&o.st, "L2.OP.025", "a packed value is stored"));
        }
        let buf = self.descs[dests[0]].buf[parity];
        let (nb, rows_out) = (self.tile / self.per_band, t.shape[0]);
        if rows_out % 16 != 0 || (rows_out != 4 * nb && rows_out != 16 * (nb == 1) as i64) {
            return Err(self.fail(
                &o.st,
                "L2.STAT.003",
                "a statistic packs 16 rows (four bands, or one band every row's) a word",
            ));
        }
        if rows.iter().any(|r| r / self.per_band == nb - 1) {
            for w in 0..rows_out / 16 {
                let regs: Vec<Symbol> = (0..4)
                    .map(|k| symbol(&stat.regs[&((4 * w + k) % nb)]))
                    .collect();
                let zero_sign = self.zero_sign;
                let word = with_tree(g, |g| tt::compact(g, &regs, zero_sign));
                for &d in &dests {
                    g.op(
                        ir::memory(
                            ir::Memory::Pack,
                            word.operand(Select::Whole),
                            self.descs[d].buf[parity] + w,
                            Some("f16"),
                            false,
                            vec![ir::kw("n", Term::int(1))],
                        )
                        .unwrap(),
                        vec![word.clone()],
                    );
                }
            }
        }
        let mut v = Val::new("l1", rows_out / 16);
        v.buf = buf;
        local.insert(target, v);
        Ok(())
    }
}
