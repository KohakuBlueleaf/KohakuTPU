//! `send` blocks: words to a unit, and a vector core's images and descriptors (L1.VC.*).

use super::*;

/// DESC words for `d` (sorted by descriptor and field); a held base patch
/// rides the word it patches.
pub(super) fn emit_descs(
    isa: &VecIsa,
    d: &Descs,
    held: &mut BTreeMap<(u32, u32), Patch>,
    words: &mut Vec<Word>,
    fixes: &mut Vec<Patch>,
) -> std::result::Result<(), ktpu_machine::Refusal> {
    for (key, v) in d {
        if let Some(p) = held.remove(key) {
            fixes.push(Patch {
                word: words.len(),
                ..p
            });
        }
        let w = VecCu::Desc {
            ad: key.0,
            fld: key.1,
            value: *v,
        }
        .encode(isa)?;
        words.push(word(w));
    }
    Ok(())
}

impl<'a> NodeReader<'a> {
    pub(super) fn send(&mut self, st: &Stmt) -> Result<()> {
        if on("l1.vc/forget-resident") {
            self.resident.clear();
        }
        let t = self.one(st)?;
        let c = self.unit(st, t)?;
        if (st.kw("repeats").is_some() || st.kw("increments").is_some())
            && !self.target.units(UnitKind::Vc).contains(&c)
        {
            return Err(self.fail(
                st,
                "L1.VC.006",
                "repeat send currently requires a vector core",
            ));
        }
        if self.target.units(UnitKind::Mg).contains(&c) {
            let mut ops = Vec::new();
            for s in &st.body {
                ops.push(self.cluster(s)?);
            }
            for (s, (op, _)) in st.body.iter().zip(&ops) {
                self.collect(c, s, op)?;
            }
            let mut words = Vec::new();
            let mut patches = Vec::new();
            for (s, (op, fix)) in st.body.iter().zip(ops) {
                let w = encoded_as(&op)
                    .encode(&self.isa.cluster)
                    .map_err(|e| self.at(s, e))?;
                for p in fix {
                    patches.push(Patch {
                        word: words.len() + p.word,
                        ..p
                    });
                }
                words.push(word(w));
            }
            if st.kw("advance").is_some() {
                self.p.steps.push(PStmt::SendAdvance {
                    coord: c,
                    words,
                    patches,
                });
            } else {
                self.p.send(c, words, patches);
            }
            return Ok(());
        }
        if self.target.units(UnitKind::Sr).contains(&c) {
            let held = self.configs.remove(&c).unwrap_or_default();
            let mut rows = RowReader::new(self, held)?;
            let ops = rows.ops_of(&st.body);
            let held = std::mem::take(&mut rows.held);
            self.configs.insert(c, held);
            let mut words = Vec::new();
            for (s, inst) in ops? {
                for w in inst.encode(&self.isa.sru).map_err(|e| self.at(&s, e))? {
                    words.push(word(w));
                }
            }
            self.p.send(c, words, Vec::new());
            return Ok(());
        }
        self.send_vector(st, c)
    }

    pub(super) fn send_vector(&mut self, st: &Stmt, c: Xy) -> Result<()> {
        let isa: &'a Isa = self.isa;
        let vec: &'a VecIsa = &isa.vector;
        let mut words: Vec<Word> = Vec::new();
        let mut fixes: Vec<Patch> = Vec::new();
        let mut descs = Descs::new();
        let mut held: BTreeMap<(u32, u32), Patch> = BTreeMap::new();
        let mut advances = Vec::new();
        for s in &st.body {
            match s.op.as_str() {
                "desc" => {
                    let (got, fix) = self.desc(s)?;
                    descs.extend(got);
                    held.extend(fix);
                }
                "run" => {
                    let call = self.one(s)?;
                    let Term::Call(name, args) = call else {
                        return Err(self.fail(s, "L1.VC.002", "run IMAGE(args)"));
                    };
                    let mut vals = Vec::new();
                    for a in args {
                        vals.push(self.const_(s, a)?);
                    }
                    let (img, own) = self.images.call(s, name, &vals)?;
                    for key in own.keys() {
                        held.remove(key);
                    }
                    let mut all = std::mem::take(&mut descs);
                    all.extend(own);
                    if let Some(Term::Tuple(strides)) = st.kw("advances") {
                        let bases: Vec<_> = all
                            .keys()
                            .enumerate()
                            .filter(|(_, key)| key.1 == 0)
                            .collect();
                        if bases.len() != strides.len() {
                            return Err(self.fail(
                                st,
                                "L1.VC.006",
                                "one advance for every descriptor base",
                            ));
                        }
                        let (bit, width) = vec.desc.span("value");
                        for ((index, _), stride) in bases.into_iter().zip(strides) {
                            let stride = self.const_(st, stride)?;
                            if !(0..=u64::MAX as i128).contains(&stride) {
                                return Err(self.fail(
                                    st,
                                    "L0.STEP.041",
                                    "descriptor advance must be nonnegative",
                                ));
                            }
                            advances.push((words.len() + index, bit, width, stride as u64));
                        }
                    }
                    emit_descs(vec, &all, &mut held, &mut words, &mut fixes)
                        .map_err(|e| self.at(s, e))?;
                    let same = self
                        .resident
                        .get(&c)
                        .is_some_and(|r| std::rc::Rc::ptr_eq(r, &img));
                    if !same {
                        for (i, w) in img.iter().enumerate() {
                            let flit = VecCu::Imem {
                                addr: i as u32,
                                word: *w,
                            }
                            .encode(vec)
                            .map_err(|e| self.at(s, e))?;
                            words.push(word(flit));
                        }
                        self.resident.insert(c, img);
                    }
                    let run = VecCu::Run { pc: 0 }
                        .encode(vec)
                        .map_err(|e| self.at(s, e))?;
                    words.push(word(run));
                }
                other => {
                    return Err(self.fail(
                        s,
                        "L1.VC.006",
                        format!("a vector core's send holds desc and run, not '{other}'"),
                    ))
                }
            }
        }
        emit_descs(vec, &descs, &mut held, &mut words, &mut fixes).map_err(|e| self.at(st, e))?;
        if let Some(repeats) = st.kw("repeats") {
            if !fixes.is_empty() {
                return Err(self.fail(
                    st,
                    "L1.VC.006",
                    "repeated vector sends cannot contain register patches",
                ));
            }
            let count = self.const_(st, repeats)?;
            if !(1..=65535).contains(&count) {
                return Err(self.fail(st, "L0.STEP.041", "send repeats must be in 1..65535"));
            }
            let mut increments = advances;
            if let Some(term) = st.kw("increments") {
                let Term::Tuple(entries) = term else {
                    return Err(self.fail(
                        st,
                        "L1.VC.006",
                        "increments is a tuple of (word, bit, width, delta)",
                    ));
                };
                for entry in entries {
                    let Term::Tuple(fields) = entry else {
                        return Err(self.fail(
                            st,
                            "L1.VC.006",
                            "an increment is (word, bit, width, delta)",
                        ));
                    };
                    if fields.len() != 4 {
                        return Err(self.fail(st, "L1.VC.006", "an increment has four fields"));
                    }
                    let v = fields
                        .iter()
                        .map(|v| self.const_(st, v))
                        .collect::<Result<Vec<_>>>()?;
                    if v.iter().any(|v| *v < 0)
                        || v[0] >= words.len() as i128
                        || v[1] > 255
                        || !(1..=64).contains(&v[2])
                        || v[1] + v[2] > 256
                        || v[3] > u64::MAX as i128
                    {
                        return Err(self.fail(st, "L0.STEP.041", "increment field out of range"));
                    }
                    increments.push((v[0] as usize, v[1] as u32, v[2] as u32, v[3] as u64));
                }
            }
            self.p.repeat(c, words, count as u64, increments);
            self.resident.remove(&c);
        } else {
            if st.kw("increments").is_some() || st.kw("advances").is_some() {
                return Err(self.fail(st, "L1.VC.006", "increments requires repeats"));
            }
            self.p.send(c, words, fixes);
        }
        Ok(())
    }

    /// A descriptor's fields; a base written `rN` or `C + rN` is patched
    /// into DESC's `value` and `value_hi` (L1.VC.004).
    pub(super) fn desc(&self, st: &Stmt) -> Result<(Descs, HeldPatches)> {
        let heads: Vec<usize> = st
            .args
            .iter()
            .enumerate()
            .filter(|(_, a)| matches!(a.as_kwarg(), Some((n, _)) if n != "walk"))
            .map(|(i, _)| i)
            .collect();
        let mut fix = BTreeMap::new();
        let mut st = st.clone();
        if let [h] = heads.as_slice() {
            let Term::Assign(lhs, rhs) = &st.args[*h] else {
                unreachable!()
            };
            if Self::is_reg(rhs) || matches!(rhs.as_ref(), Term::Offset(..)) {
                let (c, reg) = self.val(&st, rhs)?;
                let lhs = lhs.clone();
                let ad: u32 = lhs.as_name().unwrap()[1..].parse().map_err(|_| {
                    self.fail(&st, "L1.VC.003", "desc aN = BASE walk=(STRIDE, BOUND)")
                })?;
                st.args[*h] = Term::Assign(lhs, Box::new(Term::int(c)));
                let v = &self.isa.vector.desc;
                let (lo, wlo) = v.span("value");
                let (hi, whi) = v.span("value_hi");
                fix.insert(
                    (ad, 0),
                    Patch {
                        word: 0,
                        segments: vec![(lo, wlo, 0), (hi, whi, wlo)],
                        reg: reg.unwrap(),
                        konst: c as u64,
                    },
                );
            }
        }
        let units = self.images.units;
        Ok((image::desc(&self.r, &st, &self.images.facts, units)?, fix))
    }

    // ---------------------------------------------------------- clusters
}
