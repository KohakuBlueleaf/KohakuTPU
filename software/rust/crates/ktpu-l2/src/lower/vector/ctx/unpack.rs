//! A band's unpacks: row values, loaded statistics (L2.STAT), and values
//! unpacked again past a reduction or from their kept L1 bytes (L2.VC.016).

use super::*;

mod typed;

#[cfg(feature = "witness")]
impl<'a> Ctx<'a> {
    pub(super) fn unpacks(
        &mut self,
        g: &mut Gen,
        ops: &[Arc<Node>],
        rows: &[i64],
        local: &mut IndexMap<String, Val>,
        only: Option<&str>,
    ) -> VResult<()> {
        for n in ops {
            let o = op(n);
            if o.op != "unpack" || (only.is_some() && o.target.as_deref() != only) {
                continue;
            }
            let target = o.target.clone().unwrap();
            if let Some((uid, rname, _, _)) = &self.rel {
                if *uid == o.uid {
                    let mode = self.vals[rname].mode.clone();
                    let rows_n = count_of(ty(o), self.t.vlmax);
                    let v = local
                        .entry(target.clone())
                        .or_insert_with(|| Val::new("row", rows_n).with_mode(&mode));
                    for &r in rows {
                        let s = g.sym();
                        v.regs.insert(r, s.clone());
                        g.emit(format!("vunpk {{0}} <- l1[0] {mode} rel"), &[&s]);
                    }
                    continue;
                }
            }
            let src = self.get(o, o.st.positional()[0], Some("l1"), Some(local))?;
            if ty(o).shape.len() == 1 {
                self.unpack_stat(g, o, &src, rows, local)?;
                continue;
            }
            if mode_of(o.ty.as_ref()) != src.mode {
                return Err(self.fail(
                    &o.st,
                    "L2.OP.025",
                    format!("an @l1({}) value unpacks to @reg({})", src.mode, src.mode),
                ));
            }
            let rows_n = count_of(ty(o), self.t.vlmax);
            let cols = *ty(o).shape.last().unwrap();
            if !local.contains_key(&target) {
                let mut v = Val::new("row", rows_n).with_mode(&src.mode);
                v.cols = cols;
                local.insert(target.clone(), v);
            }
            let once = self.once(&local[&target], rows)?;
            let fixed = crate::mutant::on("l2.vector/pinned-row-fixed")
                && self.pre.iter().any(|u| u.target() == Some(target.as_str()));
            let first = once.first().map(|x| x.1);
            for (_, i) in once {
                let s = g.sym();
                local.get_mut(&target).unwrap().regs.insert(i, s.clone());
                let from = if fixed { first.unwrap_or(i) } else { i };
                g.emit(unpack_line(&src, from), &[&s]);
            }
        }
        Ok(())
    }

    /// A row statistic loaded from memory (16 rows a word): each GT4 band's
    /// register, chunk 4q + i holding row i's value in every lane.
    fn unpack_stat(
        &mut self,
        g: &mut Gen,
        o: &Op,
        src: &Val,
        rows: &[i64],
        local: &mut IndexMap<String, Val>,
    ) -> VResult<()> {
        let target = o.target.clone().unwrap();
        if let Some(lane) = o.st.kw("lane") {
            let k = value(self.body, &o.st, lane, &self.env)?;
            let kv = match k {
                Term::Int(v, _) if (0..16).contains(&v) => v,
                _ => {
                    return Err(self.fail(
                        &o.st,
                        "L2.STAT.002",
                        "unpack X lane=L: a lane 0..15 of the word",
                    ))
                }
            };
            let mode = if self.per_band != 0 { "gt4" } else { "f16" };
            if !local.contains_key(&target) {
                local.insert(
                    target.clone(),
                    Val::new("stat", ty(o).shape[0]).with_mode(mode),
                );
            }
            let (w, d) = (g.sym(), g.sym());
            g.emit(format!("vunpk {{0}} <- l1[{}] f16 n=1", src.buf), &[&w]);
            g.emit(
                format!("vmul {{0}}, k1, bcast({{1}}[0], lane={kv}, sh=3)"),
                &[&d, &w],
            );
            let per = if self.per_band != 0 { self.per_band } else { 8 };
            let mut bs: Vec<i64> = rows.iter().map(|r| r / per).collect();
            bs.sort();
            bs.dedup();
            let v = local.get_mut(&target).unwrap();
            for b in bs {
                v.regs.insert(b, d.clone());
            }
            return Ok(());
        }
        if self.per_band == 0 {
            return Err(self.fail(
                &o.st,
                "L2.STAT.007",
                "a loaded row statistic is a GT4 band's (rows of @reg(gt4))",
            ));
        }
        if !local.contains_key(&target) {
            local.insert(
                target.clone(),
                Val::new("stat", ty(o).shape[0]).with_mode("gt4"),
            );
        }
        let mut words: IndexMap<i64, String> = IndexMap::new();
        let mut bs: Vec<i64> = rows.iter().map(|r| r / self.per_band).collect();
        bs.sort();
        bs.dedup();
        for b in bs {
            let (w, lane) = (4 * b / 16, 4 * b % 16);
            if !words.contains_key(&w) {
                let s = g.sym();
                g.emit(format!("vunpk {{0}} <- l1[{}] f16 n=1", src.buf + w), &[&s]);
                words.insert(w, s);
            }
            let d = g.sym();
            g.emit(
                format!("vmul {{0}}[0:], k1, bcast({{1}}[0], lane={lane}, sh=0) vl=64"),
                &[&d, &words[&w]],
            );
            g.emit("vmov {0}[4:], {0}[0:] vl=64", &[&d]);
            local.get_mut(&target).unwrap().regs.insert(b, d);
        }
        Ok(())
    }

    /// Past a reduction, an unpacked row value read again is unpacked again.
    pub(super) fn reunpack(
        &mut self,
        g: &mut Gen,
        ops: &[Arc<Node>],
        o: &Op,
        rows: &[i64],
        local: &mut IndexMap<String, Val>,
        again: &mut HashSet<String>,
    ) -> VResult<()> {
        let rel_uid = self.rel.as_ref().map(|r| r.0);
        let unpacked: HashSet<String> = ops
            .iter()
            .map(|u| op(u))
            .filter(|u| u.op == "unpack" && Some(u.uid) != rel_uid)
            .filter_map(|u| u.target.clone())
            .collect();
        for a in o.st.positional() {
            if let Some(n) = value_name(a) {
                if unpacked.contains(n) && !again.contains(n) {
                    again.insert(n.to_string());
                    self.unpacks(g, ops, rows, local, Some(n))?;
                }
            }
        }
        Ok(())
    }

    /// A row value unpacked before the loop and not pinned: unpacked from its
    /// kept L1 bytes before the first op of the band that reads it.
    pub(super) fn unpin(
        &mut self,
        g: &mut Gen,
        o: &Op,
        rows: &[i64],
        local: &mut IndexMap<String, Val>,
        again: &mut HashSet<String>,
        fused: Option<&IndexMap<String, (Term, Term)>>,
    ) -> VResult<()> {
        let pre: Vec<Arc<Node>> = self
            .pre
            .iter()
            .filter(|u| matches!(u.as_ref(), Node::Op(x) if x.op == "unpack" && x.ty.as_ref().map(|t| t.shape.len() == 2).unwrap_or(false)))
            .cloned()
            .collect();
        let pre_t: HashSet<String> = pre
            .iter()
            .filter_map(|u| u.target().map(str::to_string))
            .collect();
        let mut names: Vec<Option<String>> = Vec::new();
        for a in o.st.positional() {
            let n = value_name(a).map(str::to_string);
            names.push(n.clone());
            if let (Some(f), Some(n)) = (fused, &n) {
                if let Some((x, y)) = f.get(n) {
                    names.push(value_name(x).map(str::to_string));
                    names.push(value_name(y).map(str::to_string));
                }
            }
        }
        for n in names.into_iter().flatten() {
            if pre_t.contains(&n) && !again.contains(&n) {
                again.insert(n.clone());
                self.unpacks(g, &pre, rows, local, Some(&n))?;
            }
        }
        Ok(())
    }
}
