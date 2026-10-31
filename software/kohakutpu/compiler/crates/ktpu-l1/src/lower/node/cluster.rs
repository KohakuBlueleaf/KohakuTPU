//! Matmul cluster statements: gemm, fill, drain and their collection (L1.MG.*).

use super::*;

impl<'a> NodeReader<'a> {
    pub(super) fn cluster(&self, st: &Stmt) -> Result<(ClusterInst, Vec<Patch>)> {
        let pos = st.positional();
        let flags: BTreeSet<&str> = pos.iter().filter_map(|p| p.as_name()).collect();
        if !st.body.is_empty() || st.target.is_some() {
            return Err(self.fail(
                st,
                "L1.MG.001",
                format!("`{}` takes no block and binds no name", st.op),
            ));
        }
        let mut patches = Vec::new();
        match st.op.as_str() {
            "fill" => {
                let form = "fill A0|A1|B0|B1 <- ADDR n=ENTRIES [at=ENTRY] [peers=(UNIT, ...)] | fill A0|A1|B0|B1 recv n=ENTRIES";
                let kws = st.kwargs();
                if pos.len() == 2
                    && pos[1].as_name() == Some("recv")
                    && kws.len() == 1
                    && kws[0].0 == "n"
                {
                    let (sel, bank) = self.side(st, pos[0])?;
                    let n = self.field(st, kws[0].1, "n", &mut patches)?;
                    let f = Fill {
                        addr: 0,
                        n: n as u32,
                        sel,
                        fbank: bank,
                        recv: true,
                        ..Fill::default()
                    };
                    return Ok((ClusterInst::Fill(f), patches));
                }
                if pos.len() != 3 || !pos[1].is_arrow("<-") || st.kw("n").is_none() {
                    return Err(self.fail(st, "L1.MG.002", form));
                }
                let (sel, bank) = self.side(st, pos[0])?;
                let addr = self.field(st, pos[2], "addr", &mut patches)?;
                let n = self.field(st, st.kw("n").unwrap(), "n", &mut patches)?;
                let mut peers = Vec::new();
                if let Some(t) = st.kw("peers") {
                    let items: Vec<&Term> = match t {
                        Term::Tuple(items) => items.iter().collect(),
                        t => vec![t],
                    };
                    for u in items {
                        peers.push(coord(self.unit(st, u)?));
                    }
                }
                let f = Fill {
                    addr: addr as u64,
                    n: n as u32,
                    sel,
                    eoff: match on("isa.cluster/node") {
                        true => 0,
                        false => self.kconst(st, "at", 0)? as u32,
                    },
                    fbank: bank,
                    peers,
                    recv: false,
                };
                Ok((ClusterInst::Fill(f), patches))
            }
            "gemm" => {
                let dims = match pos.first() {
                    Some(Term::Dims(d)) if d.len() == 3 => d.clone(),
                    _ => {
                        return Err(self.fail(
                            st,
                            "L1.MG.006",
                            "gemm GMxGNxNK a=A0 b=B0 [acc] [aoff=] [boff=] [anchor=] [emit=ADDR | emit to=UNIT l1=GRANULE [buf=] [count] [credit]]",
                        ))
                    }
                };
                if flags
                    .iter()
                    .any(|f| !matches!(*f, "acc" | "emit" | "count" | "credit" | "signal"))
                {
                    return Err(self.fail(
                        st,
                        "L1.MG.018",
                        format!("gemm flags: acc emit count credit signal; got {flags:?}"),
                    ));
                }
                let acc = flags.contains("acc")
                    || match st.kw("acc") {
                        Some(value) => self.field(st, value, "acc", &mut patches)? != 0,
                        None => false,
                    };
                let a0 = Term::name("A0");
                let b0 = Term::name("B0");
                let (sa, abank) = self.side(st, st.kw("a").unwrap_or(&a0))?;
                let (sb, bbank) = self.side(st, st.kw("b").unwrap_or(&b0))?;
                if (sa, sb) != (0, 1) {
                    return Err(self.fail(st, "L1.MG.018", "a= names an A bank, b= a B bank"));
                }
                let emit = st.kw("emit").is_some() || flags.contains("emit");
                let mut to = None;
                let addr = if flags.contains("emit") {
                    if st.kw("to").is_none() {
                        return Err(self.fail(
                            st,
                            "L1.MG.008",
                            "gemm ... emit to=UNIT l1=GRANULE: emit names its sink",
                        ));
                    }
                    let mut dflags: BTreeSet<&str> = flags
                        .iter()
                        .copied()
                        .filter(|f| *f != "acc" && *f != "emit")
                        .collect();
                    dflags.insert("fused");
                    let (d, _) = self.drain(st, &[], &dflags)?;
                    let a = d.addr;
                    to = Some(d);
                    a
                } else if emit {
                    self.field(st, st.kw("emit").unwrap(), "addr", &mut patches)? as u64
                } else {
                    0
                };
                let g = Gemm {
                    gm: dims[0] as u32,
                    gn: dims[1] as u32,
                    nk: dims[2] as u32,
                    acc,
                    aoff: self.kconst(st, "aoff", 0)? as u32,
                    boff: self.kconst(st, "boff", 0)? as u32,
                    abank,
                    bbank,
                    emit,
                    addr: if emit { addr } else { 0 },
                    anchor: self.kconst(st, "anchor", self.isa.cluster.cfg.anchor as i128)? as u32,
                    to,
                };
                let inst = ClusterInst::Gemm(g);
                inst.encode(&self.isa.cluster).map_err(|e| self.at(st, e))?;
                Ok((inst, patches))
            }
            "drain" => {
                if st.kw("n").is_none() {
                    return Err(self.fail(st, "L1.MG.009", "drain ... n=SUBTILES"));
                }
                let (d, p) = self.drain(st, &pos, &flags)?;
                Ok((ClusterInst::Drain(d), p))
            }
            "post" => {
                let kws = st.kwargs();
                let keys: BTreeSet<&str> = kws.iter().map(|(k, _)| *k).collect();
                if !pos.is_empty() || keys != BTreeSet::from(["n", "to"]) {
                    return Err(self.fail(st, "L1.MG.012", "post n=CREDITS to=UNIT"));
                }
                let (mesh, dst) = self.resolve(st, st.kw("to").unwrap())?;
                if mesh != self.target.mesh && !on("isa.cluster/node") {
                    return Err(self.fail(
                        st,
                        "L1.MG.012",
                        "a POST is a CU_SIGNAL inside this mesh",
                    ));
                }
                let n = self.const_(st, st.kw("n").unwrap())? as u32;
                Ok((ClusterInst::Post(Post { dst: coord(dst), n }), patches))
            }
            other => Err(self.fail(
                st,
                "L1.MG.014",
                format!("a cluster's send holds fill, gemm, drain, post; not '{other}'"),
            )),
        }
    }

    pub(super) fn drain(
        &self,
        st: &Stmt,
        pos: &[&Term],
        flags: &BTreeSet<&str>,
    ) -> Result<(Drain, Vec<Patch>)> {
        if flags
            .iter()
            .any(|f| !matches!(*f, "fused" | "signal" | "count" | "credit"))
        {
            return Err(self.fail(
                st,
                "L1.MG.020",
                "drain ADDR n=N [fused] [mesh=M] | drain n=N to=UNIT l1=GRANULE [buf=0|1|2] [signal] [count] [credit] [ack=UNIT]",
            ));
        }
        let mut patches = Vec::new();
        let n = match st.kw("n") {
            Some(t) => self.field(st, t, "n", &mut patches)?,
            None => 0,
        } as u32;
        let to = st.kw("to");
        let counted = flags.contains("count") || flags.contains("credit");
        if to.is_none() && counted && !on("isa.cluster/node") {
            return Err(self.fail(
                st,
                "L1.MG.020",
                "count and credit are a drain to a unit's (to=UNIT)",
            ));
        }
        let fuse = flags.contains("fused");
        let Some(to) = to else {
            let addrs: Vec<&&Term> = pos.iter().filter(|p| p.as_name().is_none()).collect();
            if addrs.len() != 1 {
                return Err(self.fail(st, "L1.MG.019", "drain ADDR n=N [fused] [mesh=M]"));
            }
            let addr = self.field(st, addrs[0], "addr", &mut patches)? as u64;
            let mut d = Drain {
                addr,
                n,
                fuse,
                ..Drain::default()
            };
            if let Some(m) = st.kw("mesh") {
                let mesh = self.const_(st, m)? as u32;
                d.mesh = mesh;
                d.fin = Some(coord(self.port(st, mesh)?));
            }
            return Ok((d, patches));
        };
        let here = self.target.mesh;
        let (mesh, mut dst) = if to.as_name() == Some("node") {
            (here, node_xy())
        } else {
            self.resolve(st, to)?
        };
        let mut dflags = self.kconst(st, "flags", 0)? as u32;
        if flags.contains("signal") {
            dflags |= DFLAG_SIGNAL;
        }
        if flags.contains("count") {
            dflags |= DFLAG_COUNT;
        }
        let mut ack = match st.kw("ack") {
            Some(t) if on("l1.mg/ack-swapped") => {
                let (x, y) = self.coord(st, t)?;
                (y, x)
            }
            Some(t) => self.coord(st, t)?,
            None => (0, 0),
        };
        let mut far = None;
        if mesh != here {
            far = Some((mesh, dst));
            dst = self.port(st, here)?;
            if ack == (0, 0) {
                ack = self.target.agent(mesh)?.unwrap_or((0, 0));
            }
        }
        let mut d = Drain {
            addr: match on("l1.facts/word-32") {
                true => 32,
                false => self.target.machine.die.word_bytes as u64,
            } * self.kconst(st, "l1", 0)? as u64,
            n,
            fuse,
            dst: Some(coord(dst)),
            dflags,
            ack: coord(ack),
            buf: self.kconst(st, "buf", BUF_L1A as i128)? as u32,
            credit: flags.contains("credit"),
            ..Drain::default()
        };
        if let Some((m, fin)) = far {
            d.mesh = m;
            d.fin = Some(coord(fin));
        }
        Ok((d, patches))
    }

    /// A cluster field's constant; a register adds a patch.
    pub(super) fn field(
        &self,
        st: &Stmt,
        t: &Term,
        name: &str,
        patches: &mut Vec<Patch>,
    ) -> Result<i128> {
        let (c, reg) = self.val(st, t)?;
        if let Some(reg) = reg {
            let segments = self
                .isa
                .cluster
                .segments(name)
                .into_iter()
                .map(|s| (s.bit, s.width, s.shift))
                .collect();
            patches.push(Patch {
                word: 0,
                segments,
                reg,
                konst: c as u64,
            });
        }
        Ok(c)
    }

    /// A fused DRAIN takes the sub-tiles an emitting GEMM hands out, in order.
    pub(super) fn collect(&mut self, c: Xy, st: &Stmt, op: &ClusterInst) -> Result<()> {
        let isa = &self.isa.cluster;
        match op {
            ClusterInst::Gemm(g) if g.emit => {
                let sink =
                    g.to.as_ref()
                        .map(|d| d.node_fields(isa))
                        .unwrap_or_default();
                self.emits
                    .entry(c)
                    .or_default()
                    .push((g.addr, g.gm * g.gn, sink, st.clone()));
            }
            ClusterInst::Drain(d) if d.fuse => {
                let pending = self.emits.entry(c).or_default();
                if pending.is_empty() {
                    return Err(self.r.fail(
                        st,
                        "L1.MG.021",
                        "a fused drain collects an emitting gemm's sub-tiles; none is open",
                    ));
                }
                let (addr, n, sink, _) = if on("l1.mg/collect-newest") {
                    pending.pop().unwrap()
                } else {
                    pending.remove(0)
                };
                let sink_ok = on("l1.mg/sink-unchecked") || d.node_fields(isa) == sink.clone();
                if (d.addr, d.n) != (addr, n) || !sink_ok {
                    return Err(self.r.fail(
                        st,
                        "L1.MG.021",
                        format!("the open emit streams {n} sub-tiles to {addr:#x} {sink:?}"),
                    ));
                }
            }
            _ => {}
        }
        Ok(())
    }
}
