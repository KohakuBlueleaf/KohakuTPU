//! A cluster's operands into its banks: fills from memory (L2.MG.005-.006,
//! .017-.019) and receives from a core (L2.MG.015, .024).

use super::*;

impl<'a> Ctx<'a> {
    pub(super) fn fill(&mut self, i: i64, op: &Op, env: &Env, seg: usize) -> Result<()> {
        let body = self.body;
        let st = &op.st;
        let bank_n = self.t.bank;
        let mut pos: Vec<Term> = st.positional().into_iter().cloned().collect();
        let kw = st.kwargs();
        if pos.len() == 1 {
            let v = value(body, st, &pos[0], env)?;
            pos = vec![match v {
                Term::Name(n) => Term::View(n, vec![]),
                other => other,
            }];
        }
        let form = "NAME = fill VIEW [share=port|all|cross] : mx7a[M, K] @A | mx7b[N, K] @B";
        if pos.len() != 1
            || !matches!(pos[0], Term::View(..))
            || kw.iter().any(|(k, _)| *k != "share")
        {
            return Err(fail(body, st, "L2.MG.017", form));
        }
        let mut share = None;
        if let Some(sv) = st.kw("share") {
            match sv.as_name() {
                Some(n) if SHARE.contains(&n) => share = Some(n.to_string()),
                _ => {
                    return Err(fail(
                        body,
                        st,
                        "L2.MG.019",
                        format!("share= {}", SHARE.join(" | ")),
                    ))
                }
            }
        }
        let side = op.ty.as_ref().map(|t| t.space.as_str()).unwrap_or("");
        let t = self.ty(
            op,
            if side == "A" || side == "B" {
                side
            } else {
                "A"
            },
        )?;
        let want = if t.space == "A" { "mx7a" } else { "mx7b" };
        if t.dtype != want {
            return Err(fail(
                body,
                st,
                "L2.SPACE.012",
                format!("@{} holds {want}, not {}", t.space, t.dtype),
            ));
        }
        let (rows, k) = (t.shape[0], t.shape[1]);
        let (g, kb) = (self.t.mg_lanes, self.t.kblock);
        if rows % g != 0 || k % kb != 0 {
            return Err(fail(
                body,
                st,
                "L2.SPACE.003",
                format!("{want}[{rows}, {k}]: rows whole groups of {g}, K whole blocks of {kb}"),
            ));
        }
        let nk = k / kb;
        let entries = (rows / g) * nk;
        if entries > bank_n {
            return Err(fail(
                body,
                st,
                "L2.SPACE.003",
                format!("{want}[{rows}, {k}] is {entries} entries; a bank holds {bank_n}"),
            ));
        }
        if share.is_some() && t.space == "B" && entries > self.t.shared_b_max {
            return Err(fail(
                body,
                st,
                "L2.MG.006",
                format!(
                    "a shared B fill of {entries} entries: a follower's B entries past {} come \
                     back wrong (Verilated card); fill it unshared",
                    self.t.shared_b_max
                ),
            ));
        }
        let view = value(body, st, &pos[0], env)?;
        let vname = match &view {
            Term::View(n, _) => n.clone(),
            other => repr(other),
        };
        let Some(buf) = body.buffers.get(&vname) else {
            return Err(fail(body, st, "L2.MG.017", format!("no buffer {vname}")));
        };
        let runs = (|| {
            let ax = M::axes_of(&view, &buf.shape)?;
            let got = M::shape_of(&ax);
            if got != t.shape {
                return Err(format!(
                    "the view is {}, the value {}",
                    super::super::mover::py_tuple(&got),
                    super::super::mover::py_tuple(&t.shape)
                ));
            }
            M::fill_runs(buf, &ax, self.t)
        })()
        .map_err(|e| fail(body, st, "L2.MG.017", e))?;
        let si = side_ix(&t.space);
        let reading = if si == 0 {
            self.s.reading.0
        } else {
            self.s.reading.1
        };
        let live: Vec<i64> = self
            .vals
            .values()
            .filter(|v| v.kind == t.space && self.uses.get(&v.at).copied().unwrap_or(-1) > i)
            .map(|v| v.bank)
            .collect();
        let free: Vec<i64> = [0, 1]
            .into_iter()
            .filter(|b| !live.contains(b) && Some(*b) != reading)
            .collect();
        if free.is_empty() {
            return Err(fail(
                body,
                st,
                "L2.MG.018",
                format!("both {} banks hold values still read", t.space),
            ));
        }
        let prev: Vec<i64> = self
            .vals
            .values()
            .filter(|v| v.kind == t.space)
            .map(|v| v.bank)
            .collect();
        let bank = match prev.last() {
            None => free[0],
            Some(p) if !free.contains(p) || free.len() == 1 => free[0],
            Some(p) => 1 - p,
        };
        let key = share.as_ref().map(|_| (vname.clone(), runs.clone()));
        let unit = self.s.unit.clone();
        self.s.accesses.push(Access::new(
            &unit,
            true,
            (&unit, &t.space),
            bank * bank_n,
            bank * bank_n + entries,
        ));
        self.s
            .accesses
            .push(Access::new(&unit, true, (&unit, "rx"), 0, 1));
        let e = M::entry(self.t);
        for &(off0, n0, eoff0) in &runs {
            self.s
                .accesses
                .push(mem(&unit, false, &vname, off0, off0 + n0 * e));
            let (mut off, mut n, mut eoff) = (off0, n0, eoff0);
            let mf = self.t.max_fill;
            while n > 0 {
                let part = if n <= mf {
                    n
                } else if n <= 2 * mf {
                    (n + 1) / 2
                } else {
                    mf
                };
                let at = if eoff != 0 {
                    format!(" at={eoff}")
                } else {
                    String::new()
                };
                let mut ln = Line::new(
                    format!("fill {}{bank} <- {vname} + {off} n={part}{at}", t.space),
                    seg,
                );
                ln.share = share.clone();
                ln.key = key.clone();
                ln.fill = true;
                self.s.lines.push(ln);
                off += part * e;
                n -= part;
                eoff += part;
            }
        }
        let target = op.target.clone().unwrap();
        self.vals.shift_remove(&target);
        let mut v = Val::new(if t.space == "A" { "A" } else { "B" }, (rows, k));
        v.bank = bank;
        v.nk = nk;
        v.at = i;
        self.vals.insert(target, v);
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub(super) fn recv_stream(
        &mut self,
        op: &Op,
        env: &Env,
        links: &mut Links,
        i: i64,
        seg: usize,
        stream_n: &IndexMap<String, i64>,
        stream_k: &mut IndexMap<String, i64>,
    ) -> Result<Option<(i64, String, i64)>> {
        let body = self.body;
        let st = &op.st;
        let pos = st.positional();
        let form = "NAME = recv vc[H] : mx7a[M, K] @A | mx7b[N, K] @B";
        let t = match &op.ty {
            Some(t) if (t.space == "A" || t.space == "B") && pos.len() == 1 => t.clone(),
            _ => return Err(fail(body, st, "L2.MG.024", form)),
        };
        let src = value(body, st, pos[0], env)?;
        if !matches!(&src, Term::View(n, s) if n == "vc" && s.len() == 1) {
            return Err(fail(
                body,
                st,
                "L2.MG.024",
                "a cluster receives from a core: recv vc[H]",
            ));
        }
        let sender = unit_key(&src);
        if links.stream(&sender, &self.s.unit, &t.space).is_none() {
            return Ok(None);
        }
        let want = if t.space == "A" { "mx7a" } else { "mx7b" };
        if t.dtype != want {
            return Err(fail(
                body,
                st,
                "L2.SPACE.012",
                format!("@{} holds {want}, not {}", t.space, t.dtype),
            ));
        }
        let (rows, k) = (t.shape[0], t.shape[1]);
        let entries = self.t.entries(rows, k);
        let bank_n = self.t.bank;
        if entries > bank_n || bank_n % entries != 0 {
            return Err(fail(
                body,
                st,
                "L2.MG.024",
                format!("{entries} entries a recv: a bank holds a whole number of them"),
            ));
        }
        if stream_n.get(&sender).copied().unwrap_or(0) < 2 {
            return Err(fail(
                body,
                st,
                "L2.MG.024",
                "a stage receives a stream at least twice (its two slots)",
            ));
        }
        let unit = self.s.unit.clone();
        let x = links.take_stream(body, st, &sender, &unit, &t.space, entries)?;
        let k_in = stream_k.get(&sender).copied().unwrap_or(0);
        stream_k.insert(sender.clone(), k_in + 1);
        if k_in == 0 {
            let total = links.stream(&sender, &unit, &t.space).unwrap().granules();
            for c in super::super::counts(total) {
                self.s
                    .lines
                    .push(Line::new(format!("post n={c} to={sender}"), seg));
            }
        }
        let (bank, entry) = (x.at / bank_n, x.at % bank_n);
        self.s.lines.push(Line::new(
            format!("fill {}{bank} recv n={entries}", t.space),
            seg,
        ));
        let mut v = Val::new(if t.space == "A" { "A" } else { "B" }, (rows, k));
        v.bank = bank;
        v.nk = k / self.t.kblock;
        v.at = i;
        v.entry = entry;
        v.flow = true;
        let (lo, hi) = v.span(bank_n, self.t.mg_lanes);
        self.vals.insert(op.target.clone().unwrap(), v);
        self.s
            .accesses
            .push(Access::new(&unit, false, (&unit, &t.space), lo, hi).flowing(true));
        Ok(Some((self.t.entry_words() * entries, sender, k_in)))
    }

    pub(super) fn recv(
        &mut self,
        op: &Op,
        env: &Env,
        links: &mut Links,
        i: i64,
        seg: usize,
    ) -> Result<()> {
        let body = self.body;
        let st = &op.st;
        let pos = st.positional();
        let form = "NAME = recv vc[H] : mx7a[M, K] @A | mx7b[N, K] @B";
        let t = match &op.ty {
            Some(t) if (t.space == "A" || t.space == "B") && pos.len() == 1 => t.clone(),
            _ => return Err(fail(body, st, "L2.MG.015", form)),
        };
        let src = value(body, st, pos[0], env)?;
        let ok = matches!(&src, Term::View(n, s) if n == "vc" && s.len() == 1 && matches!(s[0], Term::Int(..)));
        if !ok {
            return Err(fail(
                body,
                st,
                "L2.MG.015",
                "a cluster receives from a core: recv vc[H]",
            ));
        }
        let unit = self.s.unit.clone();
        let x = links
            .take(body, st, &unit_key(&src), &unit, &t.space)?
            .expect("a node's links");
        let k = t.shape[1];
        let want = if t.space == "A" { "mx7a" } else { "mx7b" };
        if t.dtype != want || x.shape != t.shape {
            return Err(fail(
                body,
                st,
                "L2.MG.015",
                format!(
                    "core sent {} into @{}; this receives {}{}",
                    super::super::mover::py_tuple(&x.shape),
                    t.space,
                    t.dtype,
                    py_list(&t.shape)
                ),
            ));
        }
        let bank_n = self.t.bank;
        let (bank, entry) = (x.at / bank_n, x.at % bank_n);
        self.s.lines.push(Line::new(
            format!("fill {}{bank} recv n={}", t.space, x.size),
            seg,
        ));
        let mut v = Val::new(if t.space == "A" { "A" } else { "B" }, (t.shape[0], k));
        v.bank = bank;
        v.nk = k / self.t.kblock;
        v.at = i;
        v.entry = entry;
        let (lo, hi) = v.span(bank_n, self.t.mg_lanes);
        self.vals.insert(op.target.clone().unwrap(), v);
        self.s
            .accesses
            .push(Access::new(&unit, false, (&unit, &t.space), lo, hi));
        Ok(())
    }
}
