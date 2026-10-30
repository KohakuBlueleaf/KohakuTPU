//! `run._rows`: the plan's rows-kernel view, a reduction split in two where
//! a read is laid out, the kernels run over the bound reads.

use super::*;

impl Runner<'_> {
    /// `_wide`: W' of a rows kernel over `cols`, whole GT4 blocks.
    fn wide(&self, cols: &[Id]) -> i64 {
        if mutant::on("tg.run/wide_exact") {
            return self.size(cols);
        }
        self.hw.whole_blocks(self.size(cols))
    }

    /// `_reduce_order`: the reduced ranges, outermost first by an operand
    /// reading them all.
    pub fn reduce_order(&self, plan: &Plan) -> Vec<Id> {
        let rs = plan.reduce_ranges.clone();
        let live = self.live(&rs);
        for a in &plan.accesses {
            if live.iter().all(|r| a.c(*r) != 0) {
                let mut s = rs.clone();
                s.sort_by_key(|r| -a.c(*r));
                return s;
            }
        }
        rs
    }

    /// `_takes`: whether a rows kernel W' wide reads `o` as it is, its
    /// buffer's view `start` elements in.
    pub fn takes(&self, o: Option<&Operand>, wp: i64, start: i64) -> bool {
        let Some(o) = o else { return false };
        if mutant::on("tg.run/takes_never") && o.kind == "col" {
            return false;
        }
        let h = self.hw.halves;
        match o.kind.as_str() {
            "full" => {
                (start + o.offset).rem_euclid(h) == 0
                    && o.pitch.rem_euclid(h) == 0
                    && o.offset.rem_euclid(o.pitch) + wp <= o.pitch
            }
            "col" | "row" => (start + o.offset).rem_euclid(h) == 0,
            _ => true,
        }
    }

    /// `_laid`: the reads (and with `out` the output window) a rows kernel
    /// over [rows, cols] W' wide does not take as they are.
    fn laid(&self, plan: &Plan, rows: &[Id], cols: &[Id], wp: i64, out: bool) -> usize {
        if mutant::on("tg.run/laid_free") {
            return 0;
        }
        let mut accs = plan.accesses.clone();
        if out && plan.out_strides.is_some() {
            accs.push(self.out_access(plan));
        }
        accs.iter()
            .filter(|a| !self.takes(self.classify(a, rows, cols).as_ref(), wp, 0))
            .count()
    }

    /// `_view`: `(kind, rows, cols)` of the plan's rows kernel.
    fn view(&self, plan: &Plan) -> (String, Vec<Id>, Vec<Id>) {
        let mut views: Vec<(String, Vec<Id>, Vec<Id>)> = Vec::new();
        if !plan.reduce_ranges.is_empty() {
            let red = self.reduce_order(plan);
            let kept = plan.out_ranges.clone();
            views.push(("along".into(), kept.clone(), red.clone()));
            if !self.live(&kept).is_empty() {
                views.push(("down".into(), red, kept));
            }
        } else {
            let order = &plan.out_ranges;
            for s in (0..order.len()).rev() {
                if !self.live(&order[s..]).is_empty() {
                    views.push(("rows".into(), order[..s].to_vec(), order[s..].to_vec()));
                }
            }
            if views.is_empty() {
                views.push(("rows".into(), Vec::new(), order.clone()));
            }
        }
        let best = (0..views.len())
            .min_by_key(|i| {
                let (_, rows, cols) = &views[*i];
                (
                    self.laid(plan, rows, cols, self.wide(cols), true),
                    self.size(cols) % self.hw.kblock != 0,
                    *i,
                )
            })
            .unwrap();
        views.swap_remove(best)
    }

    /// `_split_reduction`: one reduction over ranges inside and outside the
    /// kept ones as two, the inner first.
    fn split_reduction(&self, plan: &Plan) -> Option<Plan> {
        if mutant::on("tg.run/no_split") {
            return None;
        }
        let e = plan.expr.as_ref().unwrap();
        let reds = all_reduces(e);
        if reds.len() != 1 {
            return None;
        }
        let red = reds[0].clone();
        let rs = self.live(&reductions_of(plan, &red));
        let reader = plan
            .accesses
            .iter()
            .find(|a| rs.iter().all(|r| a.c(*r) != 0))?;
        let finest = plan
            .out_ranges
            .iter()
            .filter(|r| self.extent(**r) > 1 && reader.c(**r) != 0)
            .map(|r| reader.c(*r))
            .min();
        let mut inner: Vec<Id> = rs
            .iter()
            .copied()
            .filter(|r| finest.is_none_or(|f| reader.c(*r) < f))
            .collect();
        let mut outer: Vec<Id> = rs.iter().copied().filter(|r| !inner.contains(r)).collect();
        if (inner.is_empty() || outer.is_empty()) && rs.len() > 1 {
            let mut top = rs[0];
            for r in &rs[1..] {
                if reader.c(*r) > reader.c(top) {
                    top = *r;
                }
            }
            inner = rs.iter().copied().filter(|r| *r != top).collect();
            outer = vec![top];
        }
        if inner.is_empty() || outer.is_empty() {
            return None;
        }
        let Expr::Reduce(rop, rarg) = red.as_ref() else {
            unreachable!()
        };
        let first = expr::reduce(rop, rarg.clone());
        let second = expr::reduce(rop, first.clone());
        fn sw(x: &E, red: &E, second: &E) -> E {
            if Rc::ptr_eq(x, red) {
                return second.clone();
            }
            match x.as_ref() {
                Expr::Op(n, a) => expr::op(n, a.iter().map(|y| sw(y, red, second)).collect()),
                _ => x.clone(),
            }
        }
        Some(Plan {
            expr: Some(sw(e, &red, &second)),
            reductions: vec![(first, inner), (second, outer)],
            ..plan.clone()
        })
    }

    /// `_rows`: `plan` -- one reduction, or none -- on the rows kernels.
    pub(in crate::run) fn rows(&mut self, plan: &Plan, bufs: &Bufs) -> Result<(), Refusal> {
        let (kind, rows, cols) = self.view(plan);
        let wp = self.wide(&cols);
        let e = plan.expr.clone().unwrap();
        if !plan.reduce_ranges.is_empty() && self.laid(plan, &rows, &cols, wp, false) > 0 {
            if let Some(split) = self.split_reduction(plan) {
                return self.materialise(&split, bufs);
            }
        }
        if kind == "down" && all_reduces(&e).len() > 1 {
            return self.materialise(plan, bufs);
        }
        let mut tmps = Vec::new();
        let got = self.rows_bound(plan, bufs, (&kind, &rows, &cols, wp), e, &mut tmps);
        for t in tmps {
            self.free_tmp(&t);
        }
        got
    }

    /// `_rows` past its view: the reads bound, the column guard, the result
    /// placed, the kernels run, the result finished.
    fn rows_bound(
        &mut self,
        plan: &Plan,
        bufs: &Bufs,
        (kind, rows, cols, wp): (&str, &[Id], &[Id], i64),
        mut e: E,
        tmps: &mut Vec<String>,
    ) -> Result<(), Refusal> {
        let (w, r) = (self.size(cols), self.size(rows));
        let mut binds = BTreeMap::new();
        let mut ops = Vec::new();
        for (i, a) in plan.accesses.iter().enumerate() {
            let (at, o) = self.bind(plan, a, (rows, cols, wp), bufs, tmps)?;
            binds.insert(format!("in{i}"), at);
            ops.push(o);
        }
        if kind == "along" && wp != w {
            let k = ops.len();
            let bits = (0..wp).map(|i| if i < w { 0x3c00 } else { 0 }).collect();
            let mask = Table {
                shape: vec![wp as usize],
                bits,
            };
            binds.insert(format!("in{k}"), Bound::At(self.table_of(&mask)));
            ops.push(Operand::new("col"));
            e = rebuild(
                &e,
                &mut |_| None,
                &mut |x, new| match (x.as_ref(), new.as_ref()) {
                    (Expr::Reduce(op, _), Expr::Reduce(_, arg)) => {
                        let lane = expr::op(
                            "where",
                            vec![expr::load(k), arg.clone(), expr::constant(identity(op))],
                        );
                        expr::reduce(op, lane)
                    }
                    _ => new,
                },
            );
        }
        let (out_at, out, done) = self.place(plan, bufs, (kind, rows, cols, wp), tmps)?;
        let info = json!({"plan": plan.name, "view": kind});
        if kind == "down" {
            self.column(&e, &ops, &binds, r, wp, &out, &out_at, &info)?;
        } else {
            self.rows_run(&e, &ops, &binds, r, wp, &out, &out_at, &info)?;
        }
        self.finish(done)
    }
}
