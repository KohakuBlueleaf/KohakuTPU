//! Element-granular relayouts (`kernels/lanes.py` `plan`, `forms.relayout`,
//! `run._gather`, L2.REL.001, TG.RUN.008): f16 elements walked into any
//! other order by tap classes; the plan's index lists and lane masks
//! (`ktpu_kernels::gen::lanes::plan`, sized by the machine) are the tables
//! the planner uploads.

use std::collections::BTreeMap;

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
    /// `dat`; any other 0, or with `keep` as `dst` held it.
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
