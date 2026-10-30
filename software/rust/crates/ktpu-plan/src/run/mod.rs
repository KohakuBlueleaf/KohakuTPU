//! Running a plan against the bound storages (`ktpugrad/run.py`, TG.RUN):
//! each read classified against the kernel's rows and columns, its form made
//! (`runtime/forms.py`), the generator call and its binds a launch step, the
//! output's form recorded. The paths, in the order `_vector` takes them
//! (TG.RUN.003).

use std::collections::{BTreeMap, HashMap};

use ktpu_kernels::gen::matmul::Knobs;
use ktpu_machine::{Machine, Refusal, UnitKind};
use serde_json::{json, Value};
use sha2::{Digest as _, Sha256};

use crate::exec::{f16, flat, Exec, View};
use crate::expr::{self, Expr, E};
use crate::fold::{self, Ty};
use crate::hw::Hw;
use crate::input::State;
use crate::lower::{self, hex, Access, Lowered, Plan, Slot};
use crate::mutant;
use crate::program::{Addr, Call};
use crate::uop::{Graph, Id};

mod bare;
mod boxes;
pub mod exec;
mod lanes;
pub mod matmul;
mod narrow;
pub mod rows;
mod window;

pub type Bufs = HashMap<Slot, View>;

/// A stride's coefficient on range `r` (0 when it does not walk it).
pub fn coef_of(coef: &[(Id, i64)], r: Id) -> i64 {
    coef.iter()
        .find(|(x, _)| *x == r)
        .map(|(_, c)| *c)
        .unwrap_or(0)
}

/// How a kernel reads one buffer, in elements of it (`kernels.vector.Operand`).
#[derive(Clone, Debug, PartialEq)]
pub struct Operand {
    pub kind: String,
    pub dtype: String,
    pub offset: i64,
    pub pitch: i64,
    pub rows: i64,
}

impl Operand {
    pub fn new(kind: &str) -> Operand {
        Operand {
            kind: kind.into(),
            dtype: "f16".into(),
            offset: 0,
            pitch: 0,
            rows: 0,
        }
    }

    pub fn json(&self) -> Value {
        json!({"kind": self.kind, "dtype": self.dtype, "offset": self.offset, "pitch": self.pitch, "rows": self.rows})
    }
}

pub fn refused<T>(clause: &str, msg: impl Into<String>) -> Result<T, Refusal> {
    Err(Refusal::new(clause, msg))
}

/// A path not yet ported: a refusal naming it.
pub fn unported<T>(what: &str) -> Result<T, Refusal> {
    Err(Refusal::new("TG.RUN.003", format!("not ported: {what}")))
}

/// The numpy element size of a tinygrad dtype.
pub fn item(dtype: &str) -> i64 {
    match Ty::of(dtype) {
        Some(Ty::Bool | Ty::I8 | Ty::U8) => 1,
        Some(Ty::I16 | Ty::U16 | Ty::F16) => 2,
        Some(Ty::I32 | Ty::U32 | Ty::F32) => 4,
        _ => 8,
    }
}

/// numpy's dtype name of a tinygrad dtype (`fold.np_dtype`).
pub fn np_name(dtype: &str) -> String {
    match Ty::of(dtype) {
        Some(Ty::Bool) => "bool",
        Some(Ty::I8) => "int8",
        Some(Ty::I16) => "int16",
        Some(Ty::I32) => "int32",
        Some(Ty::I64) => "int64",
        Some(Ty::U8) => "uint8",
        Some(Ty::U16) => "uint16",
        Some(Ty::U32) => "uint32",
        Some(Ty::U64) => "uint64",
        Some(Ty::F16) => "float16",
        Some(Ty::F32) => "float32",
        _ => "float64",
    }
    .into()
}

pub fn is_float(dtype: &str) -> bool {
    crate::uop::dtype::is_float(dtype)
}

/// The planner of one run: the machine, the ast's graph, the storages.
pub struct Runner<'a> {
    pub m: &'a Machine,
    /// the machine's unit facts the paths size by
    pub hw: Hw,
    pub g: Graph,
    pub x: Exec,
    /// the contraction knobs its products are planned and recorded under
    pub knobs: Knobs,
    keys: usize,
}

impl<'a> Runner<'a> {
    /// A runner under the environment's contraction knobs.
    pub fn new(m: &'a Machine, g: Graph, x: Exec) -> Runner<'a> {
        Runner {
            m,
            hw: Hw::of(m),
            g,
            x,
            knobs: Knobs::resolve(),
            keys: 0,
        }
    }

    pub fn cores(&self) -> i64 {
        self.m.units(UnitKind::Vc).len() as i64
    }

    pub fn clusters(&self) -> i64 {
        self.m.units(UnitKind::Mg).len() as i64
    }

    /// A fresh runner key (Python's `(label, id(...))` slot keys).
    pub fn key(&mut self, label: &str) -> Slot {
        self.keys += 1;
        Slot::Temp(format!("{label}:{}", self.keys))
    }

    pub fn extent(&self, r: Id) -> i64 {
        lower::extent(&self.g, r)
    }

    pub fn live(&self, rs: &[Id]) -> Vec<Id> {
        rs.iter().copied().filter(|r| self.extent(*r) > 1).collect()
    }

    pub fn mult(&self, rs: &[Id]) -> Vec<i64> {
        let mut out = vec![0; rs.len()];
        let mut step = 1;
        for (i, r) in rs.iter().enumerate().rev() {
            out[i] = step;
            step *= self.extent(*r);
        }
        out
    }

    pub fn size(&self, rs: &[Id]) -> i64 {
        rs.iter().map(|r| self.extent(*r)).product()
    }

    pub fn dense(&self, rs: &[Id]) -> Vec<(Id, i64)> {
        rs.iter().copied().zip(self.mult(rs)).collect()
    }

    /// `base_of`: `(storage, byte offset)` of a bound slot.
    pub fn base(&self, bufs: &Bufs, slot: &Slot) -> (String, i64) {
        let v = &bufs[slot];
        (v.base.clone(), v.offset)
    }

    /// `classify`: `acc` against [rows, cols].
    pub fn classify(&self, acc: &Access, rows: &[Id], cols: &[Id]) -> Option<Operand> {
        let (rows, cols) = (self.live(rows), self.live(cols));
        let coef: Vec<(Id, i64)> = acc
            .coef
            .iter()
            .copied()
            .filter(|(r, _)| self.extent(*r) > 1)
            .collect();
        if coef
            .iter()
            .any(|(r, _)| !rows.contains(r) && !cols.contains(r))
        {
            return None;
        }
        let get = |r: &Id| {
            coef.iter()
                .find(|(x, _)| x == r)
                .map(|(_, c)| *c)
                .unwrap_or(0)
        };
        let cc: Vec<i64> = cols.iter().map(get).collect();
        let rc: Vec<i64> = rows.iter().map(get).collect();
        let contig = cc == self.mult(&cols);
        let nocol = !cc.iter().any(|c| *c != 0);
        let mut pitch = None;
        if rc.iter().any(|c| *c != 0) {
            let rm = self.mult(&rows);
            let p = if rm.is_empty() {
                0
            } else {
                rc[rc.len() - 1].div_euclid(rm[rm.len() - 1])
            };
            if p != 0 && rc.iter().zip(&rm).all(|(c, m)| *c == p * m) {
                pitch = Some(p);
            }
        }
        let norow = !rc.iter().any(|c| *c != 0);
        let mk = |kind: &str, pitch: i64| Operand {
            offset: acc.off,
            pitch,
            ..Operand::new(kind)
        };
        if let (true, Some(p)) = (contig, pitch) {
            return Some(mk("full", p));
        }
        if contig && norow {
            return Some(mk("col", 0));
        }
        if nocol && pitch == Some(1) {
            return Some(mk("row", 0));
        }
        if nocol && norow {
            return Some(mk("scalar", 0));
        }
        None
    }

    // ------------------------------------------------------------- forms
    /// `forms.padded`: the valid padded-rows form when the bytes are not.
    pub fn padded(&self, st: &str) -> Option<Value> {
        let s = self.x.store(st);
        if s.has(&flat()) {
            return None;
        }
        s.valid.iter().find(|k| k[0] == "rows").cloned()
    }

    /// The f16 A a contraction kernel reads (`activation_fp16`): `src`'s
    /// rows of `cols` in `view`, dense from a word, or none where they are
    /// not.
    pub fn f16_rows(
        &mut self,
        view: &View,
        dtype: &str,
        src: &Operand,
        cols: i64,
    ) -> Result<Option<Addr>, Refusal> {
        let it = item(dtype);
        let first = src.offset + view.offset / it;
        if src.pitch != cols || first % self.hw.halves != 0 {
            return Ok(None);
        }
        let whole = View {
            base: view.base.clone(),
            offset: 0,
            nbytes: self.x.store(&view.base).nbytes,
        };
        let mut at = self.f16_of(&whole, dtype, whole.nbytes / it)?;
        at.offset += 2 * first;
        Ok(Some(at))
    }

    /// `forms.f16`: the address of `view`'s contents as f16.
    pub fn f16_of(&mut self, view: &View, dtype: &str, n: i64) -> Result<Addr, Refusal> {
        let (st, off) = (view.base.clone(), view.offset);
        let it = item(dtype);
        let is16 = Ty::of(dtype) == Some(Ty::F16);
        if off != 0 && !is16 {
            let whole = View {
                base: st.clone(),
                offset: 0,
                nbytes: self.x.store(&st).nbytes,
            };
            let total = self.x.store(&st).nbytes / it;
            let mut a = self.f16_of(&whole, dtype, total)?;
            a.offset += off / it * 2;
            return Ok(a);
        }
        if is16 {
            if self.x.store(&st).has(&flat()) {
                return Ok(self.x.addr(&st, off));
            }
            let Some(key) = self.padded(&st) else {
                return refused(
                    "API.FORM.013",
                    "an f16 buffer with no current form, read row-major",
                );
            };
            self.compact(&st, &key, 2)?;
            return Ok(self.x.addr(&st, off));
        }
        if self.x.store(&st).has(&f16()) {
            return Ok(Addr {
                buf: st,
                form: f16(),
                offset: 0,
            });
        }
        let at = self.x.form(&st, &f16(), 2 * n);
        if let Some(key) = self.padded(&st) {
            return self.compact(&st, &key, it);
        }
        let s = self.x.store(&st).clone();
        if s.host.is_some() {
            self.x.cast(at.clone(), &st, dtype, n);
        } else if s.has(&flat()) && Ty::of(dtype) == Some(Ty::F32) {
            let call = Call {
                gen: "convert.cast_kernel".into(),
                args: json!({"n": n, "src": "f32", "dst": "f16", "cores": self.cores()}),
            };
            let binds = BTreeMap::from([
                ("in0".to_string(), self.x.addr(&st, 0)),
                ("out".to_string(), at.clone()),
            ]);
            self.x.launch(call, "convert", json!({}), binds);
        } else {
            return refused(
                "API.FORM.015",
                format!(
                    "{} values with no f16 form and no host bytes",
                    np_name(dtype)
                ),
            );
        }
        self.x.made(&st, &f16());
        Ok(at)
    }

    /// `forms.compact`: a padded-rows form made row-major.
    pub fn compact(&mut self, st: &str, key: &Value, it: i64) -> Result<Addr, Refusal> {
        let (w, wp) = (key[1].as_i64().unwrap(), key[2].as_i64().unwrap());
        let rows = self.x.store(st).nbytes / (it * w);
        let (at, made) = if it == 2 {
            (self.x.addr(st, 0), flat())
        } else {
            (self.x.form(st, &f16(), 2 * rows * w), f16())
        };
        let src = Addr {
            buf: st.into(),
            form: key.clone(),
            offset: 0,
        };
        self.repitch(src, wp, at.clone(), w, rows, w)?;
        self.x.made(st, &made);
        Ok(at)
    }

    /// `forms.repitch`: rows from one pitch to another.
    pub fn repitch(
        &mut self,
        src: Addr,
        sp: i64,
        dst: Addr,
        dp: i64,
        rows: i64,
        w: i64,
    ) -> Result<(), Refusal> {
        let word = self.hw.word;
        if (2 * w) % word == 0 && (2 * sp) % word == 0 && (2 * dp) % word == 0 {
            let words = 2 * w / word;
            let call = Call {
                gen: "convert.walk_kernel".into(),
                args: json!({
                    "src_bytes": rows * 2 * sp + word,
                    "levels": [[rows, 2 * sp], [words, word]],
                    "at": 0,
                    "dst_bytes": rows * 2 * dp + word,
                    "dlevels": [[rows, 2 * dp], [words, word]],
                    "dat": 0,
                }),
            };
            let binds = BTreeMap::from([("src".to_string(), src), ("dst".to_string(), dst)]);
            self.x.launch(call, "convert", json!({}), binds);
            return Ok(());
        }
        let walk = [(rows, sp), (w, 1)];
        let dwalk = [(rows, dp), (w, 1)];
        let (n, m) = ((rows - 1) * sp + w, (rows - 1) * dp + w);
        self.relayout(src, n, &walk, 0, &dwalk, m, dst, 0, false)
    }

    // ------------------------------------------------------------- run
    /// `_reform`: reads of a buffer held as padded rows through an alias of
    /// that form, strides remapped.
    fn reform(&mut self, plan: &Plan, bufs: &Bufs) -> (Plan, Bufs, Vec<String>) {
        if mutant::on("tg.run/no_reform") {
            return (plan.clone(), bufs.clone(), Vec::new());
        }
        let mut bufs = bufs.clone();
        let mut made = Vec::new();
        let mut accesses = Vec::new();
        for a in &plan.accesses {
            let key = match &a.slot {
                Slot::Param(_) | Slot::Temp(_) if bufs.contains_key(&a.slot) => {
                    let (st, off) = self.base(&bufs, &a.slot);
                    if off == 0 {
                        self.padded(&st).map(|k| (st, k))
                    } else {
                        None
                    }
                }
                _ => None,
            };
            let out_slot = plan.out_slot.clone();
            let Some((st, key)) = key.filter(|_| a.slot != out_slot && is_float(&a.dtype)) else {
                accesses.push(a.clone());
                continue;
            };
            let (n, np) = (key[1].as_i64().unwrap(), key[2].as_i64().unwrap());
            let inner: Vec<(Id, i64)> = a
                .coef
                .iter()
                .copied()
                .filter(|(r, c)| c % n != 0 && self.extent(*r) > 1)
                .collect();
            let span = a.off.rem_euclid(n)
                + inner
                    .iter()
                    .map(|(r, c)| c * (self.extent(*r) - 1))
                    .sum::<i64>();
            if inner.iter().any(|(_, c)| *c < 0) || span >= n {
                accesses.push(a.clone());
                continue;
            }
            let coef = a
                .coef
                .iter()
                .map(|(r, c)| (*r, if c % n != 0 { *c } else { c.div_euclid(n) * np }))
                .collect();
            let off = a.off.div_euclid(n) * np + a.off.rem_euclid(n);
            let alias = Slot::Temp(format!("rows:{:?}", a.slot));
            if !bufs.contains_key(&alias) {
                let nbytes = self
                    .x
                    .store(&st)
                    .forms
                    .iter()
                    .find(|(k, _)| *k == key)
                    .map(|(_, n)| *n)
                    .unwrap();
                let at = Addr {
                    buf: st.clone(),
                    form: key.clone(),
                    offset: 0,
                };
                let name = self.x.alias(at, nbytes);
                made.push(name.clone());
                bufs.insert(
                    alias.clone(),
                    View {
                        base: name,
                        offset: 0,
                        nbytes,
                    },
                );
            }
            accesses.push(Access {
                slot: alias,
                coef,
                off,
                dtype: "half".into(),
                ..a.clone()
            });
        }
        (
            Plan {
                accesses,
                ..plan.clone()
            },
            bufs,
            made,
        )
    }

    fn execute(&mut self, plan: &Plan, bufs: &Bufs) -> Result<(), Refusal> {
        if plan.kind != "vector" && plan.kind != "copy" {
            return self.execute_kind(plan, bufs);
        }
        let (mut plan, aliased, made) = self.reform(plan, bufs);
        if plan.kind == "copy"
            && plan
                .accesses
                .iter()
                .any(|a| matches!(a.slot, Slot::Temp(_)))
        {
            plan.kind = "vector".into();
        }
        let got = self.execute_kind(&plan, &aliased);
        for name in made {
            self.x.free(&name);
        }
        got
    }

    fn execute_kind(&mut self, plan: &Plan, bufs: &Bufs) -> Result<(), Refusal> {
        let out = plan.out_slot.clone();
        match plan.kind.as_str() {
            "const" => {
                let (st, off) = self.base(bufs, &out);
                let v = plan.value.as_ref().unwrap();
                let d = lower::arr_digest(v);
                let bytes = v.len() as i64 * item(&plan.out_dtype);
                let at = self.x.addr(&st, off);
                let mut h = Sha256::new();
                h.update(raw_bytes(v));
                self.x.write(at, bytes, hex(&h.finalize()));
                self.x.wrote(&st, &flat(), false);
                let s = self.x.store_mut(&st);
                s.host = (off == 0).then(|| crate::input::Digest {
                    bytes,
                    sha256: d.sha256.clone(),
                });
                s.dtype = Some(np_name(&plan.out_dtype));
                if mutant::on("tg.run/const_no_host") {
                    s.host = None;
                }
                Ok(())
            }
            "copy" if mutant::on("tg.run/copy_as_vector") => {
                let p = Plan {
                    kind: "vector".into(),
                    ..plan.clone()
                };
                self.vector(&p, bufs)
            }
            "copy" => self.copy(plan, bufs),
            "vector" => self.vector(plan, bufs),
            "matmul" => self.matmul(plan, bufs),
            k => refused("TG.PLAN.004", format!("no plan kind {k}")),
        }
    }

    // ---------------------------------------------------------- operands
    /// `_addr`: a read in its f16 form; a value a row or one value from its
    /// base's.
    pub fn addr_of(
        &mut self,
        acc: &Access,
        o: &Operand,
        bufs: &Bufs,
    ) -> Result<(Addr, Operand), Refusal> {
        let Some(view) = bufs.get(&acc.slot).cloned() else {
            let t = acc.table.as_ref().unwrap();
            return Ok((self.table_of(t), o.clone()));
        };
        let it = item(&acc.dtype);
        let nbase = self.x.store(&view.base).nbytes / it;
        if o.kind == "row" || o.kind == "scalar" {
            let whole = View {
                base: view.base.clone(),
                offset: 0,
                nbytes: self.x.store(&view.base).nbytes,
            };
            let at = self.f16_of(&whole, &acc.dtype, nbase)?;
            let mut o = o.clone();
            o.offset += view.offset / it;
            return Ok((at, o));
        }
        let n = if view.offset == 0 {
            nbase
        } else {
            view.nbytes / it
        };
        Ok((self.f16_of(&view, &acc.dtype, n)?, o.clone()))
    }

    /// `forms.table`: a table's address, uploaded once by content.
    pub fn table_of(&mut self, t: &lower::Table) -> Addr {
        let mut bytes = Vec::with_capacity(2 * t.bits.len());
        for b in &t.bits {
            bytes.extend_from_slice(&b.to_le_bytes());
        }
        self.table_bytes(&bytes, &t.shape, "float16")
    }

    /// `forms.table` of an array's raw bytes, its shape and numpy dtype.
    pub fn table_bytes(&mut self, bytes: &[u8], shape: &[usize], dtype: &str) -> Addr {
        let dims: Vec<String> = shape.iter().map(|d| d.to_string()).collect();
        let tail = if dims.len() == 1 {
            format!("({},)", dims[0])
        } else {
            format!("({})", dims.join(", "))
        };
        let mut h = sha1::Sha1Like::new();
        h.update(bytes);
        h.update(format!("({tail}, dtype('{dtype}'))").as_bytes());
        let array = crate::program::Array {
            dtype: dtype.into(),
            shape: shape.iter().map(|d| *d as i64).collect(),
            sha256: hex(&Sha256::digest(bytes)),
        };
        self.x.table(&h.hexdigest(), array)
    }

    /// `_out`: where a kernel writes the output: an f16 buffer's bytes, else
    /// its f16 form (the rest current first).
    fn out(&mut self, plan: &Plan, bufs: &Bufs) -> Result<(Addr, Value), Refusal> {
        let (st, off) = self.base(bufs, &plan.out_slot.clone());
        let n = plan.out_shape.iter().product::<i64>();
        let it = item(&plan.out_dtype);
        if Ty::of(&plan.out_dtype) == Some(Ty::F16) || mutant::on("tg.run/out_flat") {
            return Ok((self.x.addr(&st, off), flat()));
        }
        let total = self.x.store(&st).nbytes / it;
        let s = self.x.store(&st).clone();
        if (off != 0 || n != total) && (s.dtype.is_some() || s.host.is_some()) {
            let whole = View {
                base: st.clone(),
                offset: 0,
                nbytes: s.nbytes,
            };
            self.f16_of(&whole, &plan.out_dtype, total)?;
        }
        let mut at = self.x.form(&st, &f16(), 2 * total);
        at.offset += off / it * 2;
        Ok((at, f16()))
    }

    fn wrote(&mut self, plan: &Plan, bufs: &Bufs, key: &Value) {
        let (st, _) = self.base(bufs, &plan.out_slot.clone());
        self.x.wrote(&st, key, false);
        self.x.store_mut(&st).dtype = Some(np_name(&plan.out_dtype));
    }

    // -------------------------------------------------------------- copy
    fn copy(&mut self, plan: &Plan, bufs: &Bufs) -> Result<(), Refusal> {
        let acc = &plan.accesses[0];
        let (sst, soff) = self.base(bufs, &acc.slot);
        let (dst, doff) = self.base(bufs, &plan.out_slot.clone());
        let s = self.x.store(&sst).clone();
        let key = [flat(), f16()]
            .into_iter()
            .find(|k| s.has(k))
            .or_else(|| self.padded(&sst));
        let Some(key) = key else {
            return refused("TG.RUN.022", "a copy of a buffer with no current form");
        };
        let (n, a, b, whole);
        if key == flat() {
            n = plan.out_shape.iter().product::<i64>() * item(&plan.out_dtype);
            a = self.x.addr(&sst, soff);
            b = self.x.addr(&dst, doff);
            whole = soff % self.hw.word == 0 && doff % self.hw.word == 0;
        } else {
            n = s
                .forms
                .iter()
                .find(|(k, _)| *k == key)
                .map(|(_, n)| *n)
                .unwrap();
            a = Addr {
                buf: sst.clone(),
                form: key.clone(),
                offset: 0,
            };
            b = self.x.addr(&dst, 0);
            whole = soff == 0 && doff == 0;
        }
        if !whole {
            let mut p = plan.clone();
            p.kind = "vector".into();
            return self.vector(&p, bufs);
        }
        let b = if key != flat() {
            self.x.form(&dst, &key, n)
        } else {
            b
        };
        let call = Call {
            gen: "convert.copy_kernel".into(),
            args: json!({ "nbytes": n }),
        };
        let binds = BTreeMap::from([("src".to_string(), a), ("dst".to_string(), b)]);
        self.x
            .launch(call, "copy", json!({"plan": plan.name}), binds);
        self.x.wrote(&dst, &key, false);
        self.x.store_mut(&dst).dtype = Some(np_name(&plan.out_dtype));
        if key == flat() && s.host.is_some() && soff == 0 && doff == 0 {
            self.x.store_mut(&dst).host = s.host.clone();
        }
        Ok(())
    }

    // ------------------------------------------------------------ vector
    fn vector(&mut self, plan: &Plan, bufs: &Bufs) -> Result<(), Refusal> {
        let reduce = !plan.reduce_ranges.is_empty();
        if reduce && self.window(plan, bufs)? {
            return Ok(());
        }
        if reduce && self.tabled_run(plan, bufs)? {
            return Ok(());
        }
        let phases = if reduce {
            self.table_only(plan)
        } else {
            vec![]
        };
        if !phases.is_empty() {
            return self.enumerated(plan, bufs, &phases);
        }
        if let Some(mm) = self.contraction(plan) {
            return self.matmul(&mm, bufs);
        }
        if self.epilogue_contraction(plan) {
            return self.materialise(plan, bufs);
        }
        if !self.selectors(plan).is_empty() {
            if self.boxes(plan, bufs)? {
                return Ok(());
            }
            if plan.accesses.iter().any(|a| a.gate) {
                return self.ungated(plan, bufs);
            }
        }
        if plan.accesses.len() > self.max_reads() {
            return self.narrow(plan, bufs);
        }
        if !reduce
            && matches!(plan.expr.as_deref(), Some(Expr::Load(_)))
            && self.bare(plan, bufs)?
        {
            return Ok(());
        }
        let mut sets: Vec<&Vec<Id>> = Vec::new();
        for (_, rs) in &plan.reductions {
            if !sets.contains(&rs) {
                sets.push(rs);
            }
        }
        if sets.len() > 1 {
            return self.materialise(plan, bufs);
        }
        if !reduce && plan.out_strides.is_none() && self.flat_path(plan, bufs)? {
            return Ok(());
        }
        let plan = match reduce {
            false => self.refold(plan).unwrap_or_else(|| plan.clone()),
            true => plan.clone(),
        };
        self.rows(&plan, bufs)
    }

    /// `_selectors`: `{access: gate?}` of the tables a `where` selects by.
    pub fn selectors(&self, plan: &Plan) -> Vec<(usize, bool)> {
        let mut out: Vec<(usize, bool)> = plan
            .accesses
            .iter()
            .enumerate()
            .filter(|(_, a)| a.gate)
            .map(|(i, _)| (i, true))
            .collect();
        fn walk(e: &E, plan: &Plan, out: &mut Vec<(usize, bool)>) {
            match e.as_ref() {
                Expr::Op(name, args) => {
                    if name == "where" {
                        if let Expr::Load(i) = args[0].as_ref() {
                            if plan.accesses[*i].slot == Slot::Table
                                && !out.iter().any(|(j, _)| j == i)
                            {
                                out.push((*i, false));
                            }
                        }
                    }
                    args.iter().for_each(|a| walk(a, plan, out));
                }
                Expr::Reduce(_, a) => walk(a, plan, out),
                _ => {}
            }
        }
        walk(plan.expr.as_ref().unwrap(), plan, &mut out);
        out
    }

    /// `_flat`: every read at the output's own index, or one value.
    fn flat_path(&mut self, plan: &Plan, bufs: &Bufs) -> Result<bool, Refusal> {
        if mutant::on("tg.run/no_flat") {
            return Ok(false);
        }
        let oc: Vec<(Id, i64)> = self
            .dense(&plan.out_ranges)
            .into_iter()
            .filter(|(r, _)| self.extent(*r) > 1)
            .collect();
        let mut one = Vec::new();
        for (i, a) in plan.accesses.iter().enumerate() {
            let coef: Vec<(Id, i64)> = a
                .coef
                .iter()
                .copied()
                .filter(|(r, _)| self.extent(*r) > 1)
                .collect();
            let same = coef.len() == oc.len() && coef.iter().all(|x| oc.contains(x));
            if coef.is_empty() && !oc.is_empty() {
                one.push(i);
            } else if !same || a.off % self.hw.halves != 0 {
                return Ok(false);
            }
        }
        let n: i64 = plan.out_shape.iter().product();
        let (ost, ooff) = self.base(bufs, &plan.out_slot.clone());
        let it = item(&plan.out_dtype);
        let part = ooff != 0 || n * it != self.x.store(&ost).nbytes;
        if part && (n % self.hw.vlmax != 0 || (ooff / it) % self.hw.halves != 0) {
            return Ok(false);
        }
        let mut binds = BTreeMap::new();
        let (mut dts, mut lanes) = (Vec::new(), Vec::new());
        for (i, a) in plan.accesses.iter().enumerate() {
            let mut at = match bufs.get(&a.slot).cloned() {
                None => self.table_of(a.table.as_ref().unwrap()),
                Some(v) => {
                    let total = self.x.store(&v.base).nbytes / item(&a.dtype);
                    let mut at = self.f16_of(&v, &a.dtype, total)?;
                    at.offset += a.off * 2;
                    at
                }
            };
            if one.contains(&i) {
                let o = at.offset.rem_euclid(self.hw.word);
                lanes.push(json!([i, o / 2]));
                at.offset -= o;
            }
            binds.insert(format!("in{i}"), at);
            dts.push(json!("f16"));
        }
        let (out_at, key) = self.out(plan, bufs)?;
        let call = Call {
            gen: "vector.flat_kernel".into(),
            args: json!({
                "e": expr_arg(plan.expr.as_ref().unwrap()),
                "dtypes": dts,
                "n": n,
                "out_dtype": "f16",
                "cores": self.cores(),
                "scalars": lanes,
            }),
        };
        binds.insert("out".into(), out_at);
        self.x
            .launch(call, "dsl", json!({"plan": plan.name}), binds);
        self.wrote(plan, bufs, &key);
        Ok(true)
    }
}

/// An expression as a generator argument.
pub fn expr_arg(e: &E) -> Value {
    json!({"expr": expr::table(e, &mut HashMap::new())})
}

/// An array's bytes as numpy holds them.
fn raw_bytes(a: &fold::Arr) -> Vec<u8> {
    let mut out = Vec::new();
    for i in 0..a.len() {
        match a.ty {
            Ty::Bool | Ty::I8 | Ty::U8 => out.push(a.ints[i] as u8),
            Ty::I16 | Ty::U16 => out.extend((a.ints[i] as u16).to_le_bytes()),
            Ty::I32 | Ty::U32 => out.extend((a.ints[i] as u32).to_le_bytes()),
            Ty::I64 | Ty::U64 => out.extend((a.ints[i] as u64).to_le_bytes()),
            Ty::F16 => out.extend(fold::f16_bits(a.floats[i]).to_le_bytes()),
            Ty::F32 => out.extend((a.floats[i] as f32).to_le_bytes()),
            Ty::F64 => out.extend(a.floats[i].to_le_bytes()),
        }
    }
    out
}

/// `run.execute`: the plan of `l` run with the slots `state` binds; the steps
/// and storages it leaves (a refused run's too: they are compared).
#[allow(clippy::result_large_err)]
pub fn execute(l: Lowered, state: &State, m: &Machine) -> Result<Exec, (Refusal, Exec)> {
    execute_under(l, state, m, Knobs::resolve())
}

/// [`execute`] under the contraction knobs `knobs`.
#[allow(clippy::result_large_err)]
pub fn execute_under(
    l: Lowered,
    state: &State,
    m: &Machine,
    knobs: Knobs,
) -> Result<Exec, (Refusal, Exec)> {
    let mut r = Runner::new(m, l.graph, Exec::new(state));
    r.knobs = knobs;
    let mut bufs = Bufs::new();
    for (slot, s) in &state.slots {
        let k = Slot::Param(slot.parse().unwrap_or(-1));
        bufs.insert(
            k,
            View {
                base: s.base.clone(),
                offset: s.offset,
                nbytes: s.nbytes,
            },
        );
    }
    let got = r.execute(&l.plan, &bufs);
    let compiled = crate::program::compile(&mut r.x.steps, m);
    match got.and(compiled) {
        Ok(()) => Ok(r.x),
        Err(e) => Err((e, r.x)),
    }
}

/// SHA-1 for table content keys (`forms.table`: sha1 of the bytes and the
/// array's repr of shape and dtype).
mod sha1 {
    pub struct Sha1Like {
        h: [u32; 5],
        buf: Vec<u8>,
        len: u64,
    }

    impl Sha1Like {
        pub fn new() -> Self {
            Sha1Like {
                h: [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0],
                buf: Vec::new(),
                len: 0,
            }
        }

        pub fn update(&mut self, d: &[u8]) {
            self.len += d.len() as u64;
            self.buf.extend_from_slice(d);
            while self.buf.len() >= 64 {
                let block: Vec<u8> = self.buf.drain(..64).collect();
                self.block(&block);
            }
        }

        fn block(&mut self, b: &[u8]) {
            let mut w = [0u32; 80];
            for i in 0..16 {
                w[i] = u32::from_be_bytes([b[4 * i], b[4 * i + 1], b[4 * i + 2], b[4 * i + 3]]);
            }
            for i in 16..80 {
                w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
            }
            let [mut a, mut b2, mut c, mut d, mut e] = self.h;
            for (i, wi) in w.iter().enumerate() {
                let (f, k) = match i {
                    0..=19 => ((b2 & c) | (!b2 & d), 0x5A827999),
                    20..=39 => (b2 ^ c ^ d, 0x6ED9EBA1),
                    40..=59 => ((b2 & c) | (b2 & d) | (c & d), 0x8F1BBCDC),
                    _ => (b2 ^ c ^ d, 0xCA62C1D6),
                };
                let t = a
                    .rotate_left(5)
                    .wrapping_add(f)
                    .wrapping_add(e)
                    .wrapping_add(k)
                    .wrapping_add(*wi);
                e = d;
                d = c;
                c = b2.rotate_left(30);
                b2 = a;
                a = t;
            }
            for (h, v) in self.h.iter_mut().zip([a, b2, c, d, e]) {
                *h = h.wrapping_add(v);
            }
        }

        pub fn hexdigest(mut self) -> String {
            let bits = self.len * 8;
            let mut pad = vec![0x80u8];
            while (self.buf.len() + pad.len()) % 64 != 56 {
                pad.push(0);
            }
            pad.extend_from_slice(&bits.to_be_bytes());
            let rest = std::mem::take(&mut self.buf);
            let mut all = rest;
            all.extend(pad);
            for chunk in all.chunks(64) {
                self.block(chunk);
            }
            self.h.iter().map(|x| format!("{x:08x}")).collect()
        }
    }
}
