//! A bare read into the output (`run._bare`, TG.RUN.005-.008): the mover's
//! word walk, a transpose, a flip, else element by element.

use std::collections::BTreeMap;

use ktpu_machine::Refusal;
use serde_json::{json, Value};

use super::{coef_of, item, np_name, refused, Bufs, Runner};
use crate::exec::{flat, View};
use crate::expr;
use crate::lower::{Access, Plan, Slot};
use crate::mutant;
use crate::program::{Addr, Call};
use crate::uop::Id;

/// `(base address, bytes, element size, byte offset)` a mover walks.
type Source = (Addr, i64, i64, i64);

impl Runner<'_> {
    /// `_out_coef`: the output's element stride on each of its ranges.
    pub fn out_coef(&self, plan: &Plan) -> Vec<(Id, i64)> {
        let strides = plan
            .out_strides
            .clone()
            .unwrap_or_else(|| self.mult(&plan.out_ranges));
        plan.out_ranges.iter().copied().zip(strides).collect()
    }

    /// `_out_access`.
    pub fn out_access(&self, plan: &Plan) -> Access {
        Access {
            slot: plan.out_slot.clone(),
            dtype: plan.out_dtype.clone(),
            coef: self.out_coef(plan),
            off: 0,
            table: None,
            gate: false,
        }
    }

    /// `_walk`: `[(count, byte stride)]` of strides `coef` over the output's
    /// ranges, adjacent levels merged, the last whole memory words.
    pub fn walk(&self, plan: &Plan, coef: &[(Id, i64)], it: i64) -> Option<Vec<(i64, i64)>> {
        let word = self.hw.word;
        let lv: Vec<(i64, i64)> = plan
            .out_ranges
            .iter()
            .filter(|r| self.extent(**r) > 1)
            .map(|r| (self.extent(*r), coef_of(coef, *r) * it))
            .collect();
        let mut merged: Vec<(i64, i64)> = Vec::new();
        for (n, s) in lv.into_iter().rev() {
            match merged.last() {
                Some(&(m, t)) if s == m * t => {
                    merged.pop();
                    merged.push((n * m, t));
                }
                _ => merged.push((n, s)),
            }
        }
        merged.reverse();
        if merged.is_empty() {
            merged.push((1, it));
        }
        let (n, s) = *merged.last().unwrap();
        if s != it
            || (n * it) % word != 0
            || merged[..merged.len() - 1]
                .iter()
                .any(|(_, t)| t.rem_euclid(word) != 0)
        {
            return None;
        }
        let mut out = merged[..merged.len() - 1].to_vec();
        out.push((n * it / word, word));
        Some(out)
    }

    /// `_words`: whether the mover walks `acc` into the output in words.
    pub fn words(&self, plan: &Plan, acc: &Access, it: i64, at: i64) -> bool {
        at.rem_euclid(self.hw.word) == 0
            && at >= 0
            && self.walk(plan, &acc.coef, it).is_some()
            && self.walk(plan, &self.out_coef(plan), it).is_some()
    }

    /// `_source`: the values a mover walks for `acc`.
    fn source(
        &mut self,
        plan: &Plan,
        acc: &Access,
        bufs: &Bufs,
    ) -> Result<Option<Source>, Refusal> {
        let Some(view) = bufs.get(&acc.slot).cloned() else {
            return Ok(None);
        };
        let (st, off) = (view.base.clone(), view.offset);
        let it = item(&acc.dtype);
        let s = self.x.store(&st).clone();
        if acc.dtype == plan.out_dtype && s.has(&flat()) {
            return Ok(Some((self.x.addr(&st, 0), s.nbytes, it, off)));
        }
        if plan.out_dtype != "half" || !super::is_float(&acc.dtype) {
            return Ok(None);
        }
        let n = s.nbytes / it;
        let addr = match self.f16_of(&view, &acc.dtype, n) {
            Ok(a) => a,
            Err(e) if e.clause.starts_with("API.FORM") => return Ok(None),
            Err(e) => return Err(e),
        };
        if acc.dtype == "half" {
            return Ok(Some((self.x.addr(&st, 0), s.nbytes, 2, off)));
        }
        Ok(Some((addr, 2 * n, 2, 0)))
    }

    /// `_window_ok`: a plan writing part of `dst` keeps the rest current.
    pub fn window_ok(&mut self, dst: &str, plan: &Plan) -> Result<(), Refusal> {
        let it = item(&plan.out_dtype);
        let s = self.x.store(dst).clone();
        let n: i64 = plan.out_shape.iter().product();
        if s.has(&flat()) || (plan.out_strides.is_none() && n * it == s.nbytes) {
            return Ok(());
        }
        if s.dtype.is_none() || s.dtype.as_deref() == Some("float16") {
            let whole = View {
                base: dst.into(),
                offset: 0,
                nbytes: s.nbytes,
            };
            self.f16_of(&whole, "half", s.nbytes / 2)?;
            return Ok(());
        }
        refused(
            "TG.RUN.023",
            format!(
                "part of a {} buffer written while its bytes are not current (its f16 form is)",
                s.dtype.unwrap()
            ),
        )
    }

    pub fn wrote_flat(&mut self, dst: &str, plan: &Plan) {
        self.x.wrote(dst, &flat(), false);
        self.x.store_mut(dst).dtype = Some(np_name(&plan.out_dtype));
    }

    /// `_move`: one mover copy, walked in whole words.
    pub fn move_(&mut self, plan: &Plan, bufs: &Bufs) -> Result<bool, Refusal> {
        if mutant::on("tg.run/no_move") {
            return Ok(false);
        }
        let acc = plan.accesses[0].clone();
        let Some((base, nbytes, it, soff)) = self.source(plan, &acc, bufs)? else {
            return Ok(false);
        };
        let (dst, doff) = self.base(bufs, &plan.out_slot);
        let at = soff + acc.off * it;
        if doff.rem_euclid(self.hw.word) != 0 || !self.words(plan, &acc, it, at) {
            return Ok(false);
        }
        let levels = self.walk(plan, &acc.coef, it).unwrap();
        let dlevels = self.walk(plan, &self.out_coef(plan), it).unwrap();
        self.window_ok(&dst, plan)?;
        let call = Call {
            gen: "convert.walk_kernel".into(),
            args: json!({
                "src_bytes": nbytes,
                "levels": levels,
                "at": at,
                "dst_bytes": self.x.store(&dst).nbytes,
                "dlevels": dlevels,
                "dat": doff,
            }),
        };
        let binds = BTreeMap::from([
            ("src".to_string(), base),
            ("dst".to_string(), self.x.addr(&dst, 0)),
        ]);
        self.x
            .launch(call, "move", json!({"plan": plan.name}), binds);
        self.wrote_flat(&dst, plan);
        Ok(true)
    }

    /// A range `r` of extent `n` (`r.replace(src=(src0.replace(arg=n), ..))`).
    pub fn resized(&mut self, r: Id, n: i64) -> Id {
        let c = self.g.src(r)[0];
        let k = self.g.with_arg(c, crate::uop::Arg::Int(n));
        let mut src = self.g.src(r).to_vec();
        src[0] = k;
        self.g.with_src(r, src)
    }

    /// `_coalesce`: adjacent output ranges every read and the output walk
    /// as one run merged into one range.
    pub fn coalesce(&mut self, plan: &Plan) -> Plan {
        let mut out = self.out_coef(plan);
        let mut rs = self.live(&plan.out_ranges);
        let n0 = rs.len();
        let mut accs = plan.accesses.clone();
        let mut i = rs.len() as i64 - 2;
        while i >= 0 {
            let (r1, r2) = (rs[i as usize], rs[i as usize + 1]);
            let e2 = self.extent(r2);
            if accs.iter().all(|a| a.c(r1) == a.c(r2) * e2)
                && coef_of(&out, r1) == coef_of(&out, r2) * e2
            {
                let e1 = self.extent(r1);
                let r = self.resized(r2, e1 * e2);
                accs = accs
                    .into_iter()
                    .map(|a| Access {
                        coef: a
                            .coef
                            .iter()
                            .filter(|(q, _)| *q != r1)
                            .map(|(q, c)| (if *q == r2 { r } else { *q }, *c))
                            .collect(),
                        ..a
                    })
                    .collect();
                out = out
                    .into_iter()
                    .filter(|(q, _)| *q != r1)
                    .map(|(q, c)| (if q == r2 { r } else { q }, c))
                    .collect();
                rs.splice(i as usize..i as usize + 2, [r]);
            }
            i -= 1;
        }
        if rs.len() == n0 {
            return plan.clone();
        }
        let strides: Vec<i64> = rs.iter().map(|r| coef_of(&out, *r)).collect();
        let dense = strides == self.mult(&rs);
        Plan {
            out_shape: rs.iter().map(|r| self.extent(*r)).collect(),
            out_ranges: rs,
            accesses: accs,
            out_strides: if dense { None } else { Some(strides) },
            ..plan.clone()
        }
    }

    /// `_bare`: the mover's walk, a transpose, a flip, else a gather.
    pub fn bare(&mut self, plan: &Plan, bufs: &Bufs) -> Result<bool, Refusal> {
        if mutant::on("tg.run/no_bare") {
            return Ok(false);
        }
        let one = self.coalesce(plan);
        Ok(self.move_(&one, bufs)?
            || self.transpose(&one, bufs)?
            || self.flip(&one, bufs)?
            || self.gather(&one, bufs)?)
    }

    fn same(&self, coef: &[(Id, i64)], dense: &[(Id, i64)]) -> bool {
        let live: Vec<(Id, i64)> = coef
            .iter()
            .copied()
            .filter(|(r, _)| self.extent(*r) > 1)
            .collect();
        let d: Vec<(Id, i64)> = dense
            .iter()
            .copied()
            .filter(|(r, _)| self.extent(*r) > 1)
            .collect();
        live.len() == d.len() && live.iter().all(|x| d.contains(x))
    }

    /// `kernels.transpose.masks`: [swaps, vlmax] f16 lane masks.
    fn masks(&self) -> Result<crate::lower::Table, Refusal> {
        let bits = ktpu_kernels::gen::transpose::masks(self.m)?;
        let vlmax = self.hw.vlmax as usize;
        Ok(crate::lower::Table {
            shape: vec![bits.len() / vlmax, vlmax],
            bits,
        })
    }

    /// `_transpose`: the two axes last by the mover, a lane transpose on the
    /// cores, the result moved into place.
    fn transpose(&mut self, plan: &Plan, bufs: &Bufs) -> Result<bool, Refusal> {
        if mutant::on("tg.run/no_transpose") {
            return Ok(true);
        }
        let acc = plan.accesses[0].clone();
        if plan.out_dtype != "half" {
            return Ok(false);
        }
        let live = self.live(&plan.out_ranges);
        if live.len() < 2 {
            return Ok(false);
        }
        let r_in = *live.last().unwrap();
        let Some(r_s) = live.iter().copied().find(|r| acc.c(*r) == 1) else {
            return Ok(false);
        };
        if r_s == r_in {
            return Ok(false);
        }
        let (a, b) = (self.extent(r_in), self.extent(r_s));
        let lanes = self.hw.lanes;
        if a % lanes != 0 {
            return Ok(false);
        }
        let bp = (b + lanes - 1) / lanes * lanes;
        let rsp = if bp == b { r_s } else { self.resized(r_s, bp) };
        let others: Vec<Id> = live
            .iter()
            .copied()
            .filter(|r| *r != r_in && *r != r_s)
            .collect();
        let nb = self.size(&others);
        let n = nb * a * bp;
        let Some((base, nbytes, it, off)) = self.source(plan, &acc, bufs)? else {
            return Ok(false);
        };
        if bp != b {
            let mut last = off / it + acc.off + bp - 1;
            last += acc
                .coef
                .iter()
                .filter(|(r, c)| *r != r_s && *c > 0)
                .map(|(r, c)| c * (self.extent(*r) - 1))
                .sum::<i64>();
            if last >= nbytes / it {
                return Ok(false);
            }
        }
        let read1 = Access {
            coef: acc
                .coef
                .iter()
                .map(|(r, c)| (if *r == r_s { rsp } else { *r }, *c))
                .collect(),
            ..acc.clone()
        };
        let mut order1 = others.clone();
        order1.extend([r_in, rsp]);
        let key1 = self.key("tr1");
        let sub = Plan {
            name: format!("{}_p", plan.name),
            out_slot: key1.clone(),
            out_dtype: "half".into(),
            out_shape: order1.iter().map(|r| self.extent(*r)).collect(),
            out_ranges: order1.clone(),
            out_strides: None,
            accesses: vec![read1.clone()],
            ..plan.clone()
        };
        let staged = !(bp == b
            && self.same(&acc.coef, &self.dense(&order1))
            && it == 2
            && (off + 2 * acc.off).rem_euclid(self.hw.word) == 0);
        let mut order2 = others.clone();
        order2.extend([r_s, r_in]);
        let (dst, doff) = self.base(bufs, &plan.out_slot);
        let direct = bp == b
            && self.same(&self.out_coef(plan), &self.dense(&order2))
            && doff.rem_euclid(self.hw.word) == 0;
        let mut o3 = others.clone();
        o3.extend([rsp, r_in]);
        let coef2: Vec<(Id, i64)> = self
            .dense(&o3)
            .into_iter()
            .map(|(r, c)| (if r == rsp { r_s } else { r }, c))
            .collect();
        let key2 = self.key("tr2");
        let read = Access {
            slot: key2.clone(),
            dtype: "half".into(),
            coef: coef2,
            off: 0,
            table: None,
            gate: false,
        };
        let post = Plan {
            expr: Some(expr::load(0)),
            accesses: vec![read.clone()],
            ..plan.clone()
        };
        if staged && !self.words(&sub, &read1, it, off + it * acc.off) {
            return Ok(false);
        }
        if !direct && (doff.rem_euclid(self.hw.word) != 0 || !self.words(&post, &read, 2, 0)) {
            return Ok(false);
        }
        let mut tmps = Vec::new();
        let src = if !staged {
            Addr {
                offset: base.offset + off + 2 * acc.off,
                ..base
            }
        } else {
            let t1 = self.x.storage(2 * n);
            tmps.push(t1.clone());
            let mut b2 = bufs.clone();
            b2.insert(
                key1,
                View {
                    base: t1.clone(),
                    offset: 0,
                    nbytes: 2 * n,
                },
            );
            assert!(self.move_(&sub, &b2)?);
            self.x.addr(&t1, 0)
        };
        let out = if direct {
            self.window_ok(&dst, plan)?;
            self.x.addr(&dst, doff)
        } else {
            let t2 = self.x.storage(2 * n);
            tmps.push(t2.clone());
            self.x.addr(&t2, 0)
        };
        let mask = self.masks()?;
        let mask = self.table_of(&mask);
        let call = Call {
            gen: "transpose.transpose_kernel".into(),
            args: json!({"nb": nb, "A": a, "B": bp, "cores": self.cores()}),
        };
        let binds = BTreeMap::from([
            ("x".to_string(), src),
            ("mask".to_string(), mask),
            ("y".to_string(), out),
        ]);
        self.x
            .launch(call, "transpose", json!({"plan": plan.name}), binds);
        if direct {
            self.wrote_flat(&dst, plan);
        } else {
            let mut b2 = bufs.clone();
            let t = tmps.last().unwrap().clone();
            b2.insert(
                key2,
                View {
                    base: t,
                    offset: 0,
                    nbytes: 2 * n,
                },
            );
            assert!(self.move_(&post, &b2)?);
        }
        for t in tmps {
            self.x.free(&t);
        }
        Ok(true)
    }

    /// `_flip`: rows walked last word first, each word's lanes reversed.
    fn flip(&mut self, plan: &Plan, bufs: &Bufs) -> Result<bool, Refusal> {
        if mutant::on("tg.run/no_flip") {
            return Ok(false);
        }
        let acc = plan.accesses[0].clone();
        if acc.slot == Slot::Table
            || acc.dtype != plan.out_dtype
            || acc.dtype != "half"
            || !bufs.contains_key(&acc.slot)
        {
            return Ok(false);
        }
        let live = self.live(&plan.out_ranges);
        if live.is_empty() || acc.c(*live.last().unwrap()) != -1 {
            return Ok(false);
        }
        let c = self.extent(*live.last().unwrap());
        let (st, off) = self.base(bufs, &acc.slot);
        let (h, word) = (self.hw.halves, self.hw.word);
        let start = off + 2 * (acc.off - (h - 1));
        let others = &live[..live.len() - 1];
        if c % h != 0
            || start.rem_euclid(word) != 0
            || start < 0
            || others.iter().any(|r| (2 * acc.c(*r)).rem_euclid(word) != 0)
            || !self.x.store(&st).has(&flat())
        {
            return Ok(false);
        }
        let mut levels: Vec<Value> = others
            .iter()
            .map(|r| json!([self.extent(*r), 2 * acc.c(*r)]))
            .collect();
        levels.push(json!([c / h, -word]));
        let n = self.size(&live);
        let (dst, doff) = self.base(bufs, &plan.out_slot);
        let whole = self.same(&self.out_coef(plan), &self.dense(&live))
            && doff == 0
            && 2 * n == self.x.store(&dst).nbytes;
        let key = self.key("flip");
        let post = Plan {
            accesses: vec![Access {
                slot: key.clone(),
                dtype: "half".into(),
                coef: self.dense(&live),
                off: 0,
                table: None,
                gate: false,
            }],
            ..plan.clone()
        };
        if !whole && (doff.rem_euclid(word) != 0 || !self.words(&post, &post.accesses[0], 2, 0)) {
            return Ok(false);
        }
        let t = (!whole).then(|| self.x.storage(2 * n));
        let call = Call {
            gen: "transpose.reverse_kernel".into(),
            args: json!({"src_bytes": self.x.store(&st).nbytes, "levels": levels, "at": start, "n": n, "cores": self.cores()}),
        };
        let out = match &t {
            None => self.x.addr(&dst, 0),
            Some(t) => self.x.addr(t, 0),
        };
        let binds = BTreeMap::from([
            ("x".to_string(), self.x.addr(&st, 0)),
            ("y".to_string(), out),
        ]);
        self.x
            .launch(call, "flip", json!({"plan": plan.name}), binds);
        match t {
            None => self.wrote_flat(&dst, plan),
            Some(t) => {
                let mut b2 = bufs.clone();
                b2.insert(
                    key,
                    View {
                        base: t.clone(),
                        offset: 0,
                        nbytes: 2 * n,
                    },
                );
                assert!(self.move_(&post, &b2)?);
                self.x.free(&t);
            }
        }
        Ok(true)
    }
}
