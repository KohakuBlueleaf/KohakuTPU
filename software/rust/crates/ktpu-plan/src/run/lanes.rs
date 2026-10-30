//! Element-granular relayouts (`kernels/lanes.py` `plan`, `forms.relayout`,
//! `run._gather`, L2.REL.001, TG.RUN.008): f16 elements walked into any
//! other order by tap classes; the plan's index lists and lane masks
//! (`ktpu_kernels::gen::lanes::plan`, sized by the machine) are the tables
//! the planner uploads.

use std::collections::BTreeMap;

use ktpu_kernels::gen::relayout;
use ktpu_machine::Refusal;
use serde_json::json;

use super::{item, Bufs, Runner};
use crate::exec::View;
use crate::lower::{Access, Plan};
use crate::program::{Addr, Call};
use crate::uop::Id;

impl Runner<'_> {
    /// `forms.relayout`: `m` f16 values at `dst` from the `n` at `src`,
    /// element `at` walked by `walk` into the elements `dwalk` reaches from
    /// `dat`; any other 0, or with `keep` as `dst` held it. One kernel of
    /// `gen::relayout`'s forms, its destination port from `dst`'s line (the
    /// words before kept) when its lines end within the allocation's words
    /// (API.MEM.002); else, and for a walk pair none takes, `gen::lanes`.
    #[allow(clippy::too_many_arguments)]
    pub fn relayout(
        &mut self,
        src: Addr,
        n: i64,
        walk: &[(i64, i64)],
        at: i64,
        dwalk: &[(i64, i64)],
        m: i64,
        dst: Addr,
        dat: i64,
        keep: bool,
    ) -> Result<(), Refusal> {
        let hw = relayout::Hw::of(self.m);
        // Buffers start on a line: the offset's place in its line is the
        // address's.
        let pad = dst.offset.rem_euclid(hw.line * hw.word) / 2;
        let tree = relayout::PULL_TREE;
        let planned = if ktpu_kernels::mutants::on(relayout::LANES_MUTANT) {
            Err(relayout::Unplanned("the lanes-only mutant".into()))
        } else {
            relayout::plan(n, walk, at, dwalk, dat + pad, m + pad, keep, &hw, pad, tree)?
        };
        let words = (self.x.allocated(&dst) + hw.word - 1) / hw.word * hw.word;
        let fits = |rp: &relayout::Plan| {
            dst.offset - 2 * pad + rp.dst_words / hw.line * hw.line * hw.word <= words
                || ktpu_kernels::mutants::on(relayout::LINES_MUTANT)
        };
        if let Some(rp) = planned.ok().filter(fits) {
            let (index, mask) = self.relayout_tables(&rp);
            let call = Call {
                gen: "relayout.relayout_kernel".into(),
                args: json!({
                    "pl": {"made_by": {"gen": "relayout.plan", "args": {
                        "src_n": n, "walk": walk, "at": at, "dwalk": dwalk,
                        "dat": dat + pad, "m": m + pad, "keep": keep, "z0": pad,
                        "tree": tree,
                    }}},
                }),
            };
            let line_start = Addr {
                offset: dst.offset - 2 * pad,
                ..dst
            };
            let binds = BTreeMap::from([
                ("src".to_string(), src),
                ("dst".to_string(), line_start),
                ("index".to_string(), index),
                ("mask".to_string(), mask),
            ]);
            self.x.launch(call, "convert", json!({}), binds);
            return Ok(());
        }
        let pl = ktpu_kernels::gen::lanes::plan(n, walk, at, dwalk, dat, m, keep, self.m)?;
        let mut ibytes = Vec::with_capacity(4 * pl.index.len());
        for v in &pl.index {
            ibytes.extend(v.to_le_bytes());
        }
        let index = self.table_bytes(&ibytes, &[pl.index.len()], "float32");
        let mut mbytes = Vec::with_capacity(2 * pl.masks.len());
        for v in &pl.masks {
            mbytes.extend(v.to_le_bytes());
        }
        let shape = [pl.mask_rows as usize, self.hw.vlmax as usize];
        let mask = self.table_bytes(&mbytes, &shape, "float16");
        let call = Call {
            gen: "lanes.relayout_kernel".into(),
            args: json!({
                "pl": {"made_by": {"gen": "lanes.plan", "args": {
                    "src_n": n, "walk": walk, "at": at, "dwalk": dwalk,
                    "dat": dat, "m": m, "keep": keep,
                }}},
                "cores": self.cores(),
            }),
        };
        let binds = BTreeMap::from([
            ("src".to_string(), src),
            ("dst".to_string(), dst),
            ("index".to_string(), index),
            ("mask".to_string(), mask),
        ]);
        self.x.launch(call, "convert", json!({}), binds);
        Ok(())
    }

    /// A `gen::relayout` plan's tables uploaded (`forms.table`): its index
    /// lists (f32), then its mask rows (f16).
    pub fn relayout_tables(&mut self, rp: &relayout::Plan) -> (Addr, Addr) {
        let mut ibytes = Vec::with_capacity(4 * rp.index.len());
        for v in &rp.index {
            ibytes.extend(v.to_le_bytes());
        }
        let index = self.table_bytes(&ibytes, &[rp.index.len()], "float32");
        let mut mbytes = Vec::with_capacity(2 * rp.masks.len() * rp.masks[0].len());
        for v in rp.mask_bits() {
            mbytes.extend(v.to_le_bytes());
        }
        let shape = [rp.masks.len(), rp.masks[0].len()];
        let mask = self.table_bytes(&mbytes, &shape, "float16");
        (index, mask)
    }

    /// `run._gather_to`: `n` f16 values at `dst` from `acc`'s buffer as f16,
    /// element `first` of `acc`'s view walked by `walk` into the elements
    /// `dwalk` reaches from `dat`. False when the buffer has no f16 form.
    #[allow(clippy::too_many_arguments)]
    pub fn gather_to(
        &mut self,
        acc: &Access,
        bufs: &Bufs,
        walk: &[(i64, i64)],
        first: i64,
        dwalk: &[(i64, i64)],
        dat: i64,
        n: i64,
        dst: Addr,
        keep: bool,
    ) -> Result<bool, Refusal> {
        let (st, off) = self.base(bufs, &acc.slot);
        let it = item(&acc.dtype);
        let nbytes = self.x.store(&st).nbytes;
        let total = nbytes / it;
        let first = first + off.div_euclid(it);
        let whole = View {
            base: st,
            offset: 0,
            nbytes,
        };
        let src = match self.f16_of(&whole, &acc.dtype, total) {
            Ok(a) => a,
            Err(e) if e.clause.starts_with("API.FORM") => return Ok(false),
            Err(e) => return Err(e),
        };
        self.relayout(src, total, walk, first, dwalk, n, dst, dat, keep)?;
        Ok(true)
    }

    /// `run._gather`: any read into an f16 output, element by element.
    pub(super) fn gather(&mut self, plan: &Plan, bufs: &Bufs) -> Result<bool, Refusal> {
        let acc = &plan.accesses[0];
        if !bufs.contains_key(&acc.slot)
            || plan.out_dtype != "half"
            || crate::mutant::on("tg.run/no_gather")
        {
            return Ok(false);
        }
        let live = self.live(&plan.out_ranges);
        let n = self.size(&live);
        let it = item(&acc.dtype);
        if self.base(bufs, &acc.slot).1.div_euclid(it) + acc.off < 0 {
            return Ok(false);
        }
        let oc = self.out_coef(plan);
        let walk: Vec<(i64, i64)> = live.iter().map(|r| (self.extent(*r), acc.c(*r))).collect();
        let dwalk: Vec<(i64, i64)> = live
            .iter()
            .map(|r| (self.extent(*r), super::coef_of(&oc, *r)))
            .collect();
        let view = bufs[&plan.out_slot].clone();
        let (dst, doff) = (view.base.clone(), view.offset);
        let dense: Vec<(Id, i64)> = self.dense(&live);
        let (word, h) = (self.hw.word, self.hw.halves);
        let words = self.hw.whole_halves(n);
        let whole = dense.iter().all(|(r, s)| super::coef_of(&oc, *r) == *s)
            && doff.rem_euclid(word) == 0
            && (n % h == 0
                || view.nbytes >= 2 * words
                || (doff == 0 && self.x.store(&dst).nbytes == 2 * n));
        let at = self.x.addr(&dst, doff.div_euclid(word) * word);
        let dat = doff.rem_euclid(word) / 2;
        let m = if whole {
            n
        } else {
            let span = dat + 1 + dwalk.iter().map(|(e, s)| (e - 1) * s).sum::<i64>();
            self.hw.whole_halves(span)
        };
        if !whole {
            self.window_ok(&dst, plan)?;
        }
        if !self.gather_to(acc, bufs, &walk, acc.off, &dwalk, dat, m, at, !whole)? {
            return Ok(false);
        }
        self.wrote_flat(&dst, plan);
        Ok(true)
    }
}
