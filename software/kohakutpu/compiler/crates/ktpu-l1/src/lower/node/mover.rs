//! Memory-mover moves: walks, gathers, fills and their register patches (L1.MOV.*).

use super::*;

impl<'a> NodeReader<'a> {
    pub(super) fn move_(&mut self, st: &Stmt) -> Result<()> {
        let pos = st.positional();
        let posted = pos.len() == 1 && pos[0].as_name() == Some("posted");
        if (!pos.is_empty() && !posted) || st.body.is_empty() {
            return Err(self.fail(st, "L1.NODE.035", "move [posted] (a block of mover ops)"));
        }
        if posted != st.target.is_some() {
            return Err(self.fail(
                st,
                "L1.NODE.035",
                "t = move posted (a posted move's token), or move",
            ));
        }
        let mut writes: Vec<(u64, u64)> = Vec::new();
        let mut patches: Vec<Patch> = Vec::new();
        for s in &st.body {
            let (w, ps) = self.mover_op(s)?;
            for p in ps {
                patches.push(Patch {
                    word: writes.len() + p.word,
                    ..p
                });
            }
            writes.extend(w);
        }
        if st.kw("advance").is_some() {
            if posted
                || patches
                    .iter()
                    .any(|p| p.segments.iter().any(|s| s.1 != 40 || s.2 != 0))
            {
                return Err(self.fail(
                    st,
                    "L1.MOV.016",
                    "advance requires blocking moves with whole 40-bit base patches",
                ));
            }
            self.p.steps.push(PStmt::MoveAdvance { writes, patches });
        } else if posted && on("l1.node/post-blocks") {
            let token = self
                .p
                .steps
                .iter()
                .filter(|s| matches!(s, PStmt::Post { .. }))
                .count();
            self.p.move_(writes, patches);
            let name = st.target_name().unwrap_or_default().to_string();
            self.tokens.insert(name, Token::Moves(token));
        } else if posted {
            let token = self.p.post(writes, patches);
            let name = st.target_name().unwrap_or_default().to_string();
            self.tokens.insert(name, Token::Moves(token));
        } else {
            self.p.move_(writes, patches);
        }
        Ok(())
    }

    pub(super) fn writes_of(&self, st: &Stmt, op: &MoverOp) -> Result<Vec<(u64, u64)>> {
        let w = self.isa.mover.writes(op).map_err(|e| self.at(st, e))?;
        Ok(w.into_iter().map(|(r, v)| (r as u64, v)).collect())
    }

    pub(super) fn mover_op(&self, st: &Stmt) -> Result<(Writes, Vec<Patch>)> {
        let all = st.positional();
        let flags: BTreeSet<&str> = all.iter().filter_map(|p| p.as_name()).collect();
        let pos: Vec<&Term> = all.into_iter().filter(|p| p.as_name().is_none()).collect();
        let kws = st.kwargs();
        let legacy = match st.op.as_str() {
            "copy" => Some("bytes"),
            "quant" => Some("entries"),
            "gt4" => Some("groups"),
            _ => None,
        };
        if let Some(lg) = legacy {
            if st.kw(lg).is_some() || pos.first().is_some_and(|p| !matches!(p, Term::Call(..))) {
                let form = pos.len() == 3 && pos[1].is_arrow("->") && kws.len() == 1;
                if !(form && kws[0].0 == lg || on("l1.mov/plain-unchecked")) {
                    return Err(self.fail(
                        st,
                        "L1.MOV.009",
                        format!("{} SRC -> DST {lg}=N [bpack], or WALK -> WALK", st.op),
                    ));
                }
                let src = self.const_(st, pos[0])? as i64;
                let dst = self.const_(st, pos[2])? as i64;
                let n = self.const_(st, kws[0].1)? as i64;
                let op = match st.op.as_str() {
                    "quant" => MoverOp::Quantise {
                        src,
                        dst,
                        entries: n,
                        b: flags.contains("bpack"),
                    },
                    "copy" => MoverOp::Copy {
                        src,
                        dst,
                        nbytes: n,
                    },
                    _ => MoverOp::Transpose4 {
                        src,
                        dst,
                        groups: n,
                    },
                };
                return Ok((self.writes_of(st, &op)?, Vec::new()));
            }
        }
        let rotated = on("l1.mov/ew");
        let ewidth = match self.kconst(st, "ew", 32)? {
            8 if rotated => W16,
            16 if rotated => W32,
            32 if rotated => W8,
            8 => W8,
            16 => W16,
            32 => W32,
            _ => return Err(self.fail(st, "L1.MOV.013", "ew= 8, 16 or 32")),
        };
        let burst = self.kconst(st, "burst", 0)? as i64;
        let fl = self
            .isa
            .mover
            .flag_byte(!flags.contains("nocoal"), burst)
            .map_err(|e| self.at(st, e))?;
        let blank = |mode| Move {
            mode,
            src: None,
            dst: None,
            ewidth,
            flags: fl,
            xform: (0, 0),
            imm: None,
            seed: None,
            index: None,
            row: None,
            fp: false,
        };
        let mut fix: Vec<(String, u64, i128)> = Vec::new();
        let mv = match st.op.as_str() {
            "copy" | "xform" | "quant" | "gt4" => {
                if pos.len() != 3 || !pos[1].is_arrow("->") {
                    return Err(self.fail(st, "L1.MOV.003", format!("{} WALK -> WALK", st.op)));
                }
                let (src, sfix) = self.walk(st, pos[0], "src")?;
                let (dst, dfix) = self.walk(st, pos[2], "dst")?;
                let mut mv = blank(MoveMode::Copy);
                mv.src = Some(src);
                mv.dst = Some(dst);
                if st.op != "copy" {
                    mv.mode = MoveMode::Xform;
                    let (quant, gt4) = if on("l1.mov/xform-ids") {
                        (XF_GT4, XF_QUANT)
                    } else {
                        (XF_QUANT, XF_GT4)
                    };
                    let xid = match st.op.as_str() {
                        "quant" => xform_id(quant) as i128,
                        "gt4" => xform_id(gt4) as i128,
                        _ => match st.kw("id") {
                            Some(t) => self.const_(st, t)?,
                            None => {
                                return Err(self.fail(
                                    st,
                                    "L1.MOV.012",
                                    "xform WALK -> WALK id=I [mode=M]",
                                ))
                            }
                        },
                    };
                    let xmode = self.kconst(st, "mode", flags.contains("bpack") as i128)?;
                    mv.xform = (xid as i64, xmode as i64);
                    mv.ewidth = W16;
                }
                let imm = match st.kw("pad") {
                    Some(t) => Some(self.val(st, t)?),
                    None => None,
                };
                mv.imm = imm.map(|(c, _)| c as i64);
                fix.extend(sfix);
                fix.extend(dfix);
                if let Some((c, Some(r))) = imm {
                    fix.push(("imm".into(), r, c));
                }
                mv
            }
            "fill" | "gen" => {
                if pos.len() != 1 {
                    return Err(self.fail(
                        st,
                        "L1.MOV.005",
                        format!("{} WALK value=V | seed=S", st.op),
                    ));
                }
                let (dst, dfix) = self.walk(st, pos[0], "dst")?;
                fix.extend(dfix);
                let key = if st.op == "fill" { "value" } else { "seed" };
                let zero = Term::int(0);
                let (c, r) = self.val(st, st.kw(key).unwrap_or(&zero))?;
                let mut mv = blank(if st.op == "fill" || on("l1.mov/gen-is-fill") {
                    MoveMode::Fill
                } else {
                    MoveMode::Generate
                });
                mv.dst = Some(dst);
                if st.op == "fill" {
                    mv.imm = Some(c as i64);
                } else {
                    if !(0..=u64::MAX as i128).contains(&c) {
                        return Err(self.fail(
                            st,
                            "L1.MOV.006",
                            format!("mover seed {c:#x} does not fit 64 bits"),
                        ));
                    }
                    mv.seed = Some(c as u64);
                }
                if let Some(r) = r {
                    fix.push((if st.op == "fill" { "imm" } else { "seed" }.into(), r, c));
                }
                mv
            }
            "gather" | "scatter" => {
                let need = ["index", "n", "pitch", "words"];
                if pos.len() != 3
                    || !pos[1].is_arrow("->")
                    || need.iter().any(|k| st.kw(k).is_none())
                {
                    return Err(self.fail(
                        st,
                        "L1.MOV.017",
                        format!("{} SRC -> DST index=ADDR n=N pitch=P words=W [fp]", st.op),
                    ));
                }
                let (ic, ireg) = self.val(st, st.kw("index").unwrap())?;
                let (n, nreg) = self.val(st, st.kw("n").unwrap())?;
                let (pitch, preg) = self.val(st, st.kw("pitch").unwrap())?;
                let (words, wreg) = self.val(st, st.kw("words").unwrap())?;
                let wb = self.target.machine.die.word_bytes as i64;
                let mut mv = blank(if st.op == "gather" {
                    MoveMode::Gather
                } else {
                    MoveMode::Scatter
                });
                if st.op == "gather" {
                    let (base, bfix) = self.base(st, pos[0], "src")?;
                    let (walk, wfix) = self.walk(st, pos[2], "dst")?;
                    mv.src = Some(Walk::new(base as i64, &[(1, wb)]));
                    mv.dst = Some(walk);
                    fix.extend(bfix);
                    fix.extend(wfix);
                } else {
                    let (walk, wfix) = self.walk(st, pos[0], "src")?;
                    let (base, bfix) = self.base(st, pos[2], "dst")?;
                    mv.src = Some(walk);
                    mv.dst = Some(Walk::new(base as i64, &[(1, wb)]));
                    fix.extend(bfix);
                    fix.extend(wfix);
                }
                mv.index = Some((ic as i64, n.max(1) as i64));
                mv.row = Some((pitch as i64, words.max(1) as i64));
                mv.fp = flags.contains("fp") && !on("l1.mov/gather-fp");
                for (k, r, c) in [
                    ("index", ireg, ic),
                    ("count", nreg, n),
                    ("pitch", preg, pitch),
                    ("words", wreg, words),
                ] {
                    if let Some(r) = r {
                        fix.push((k.into(), r, c));
                    }
                }
                mv
            }
            other => {
                return Err(self.fail(
                    st,
                    "L1.MOV.012",
                    format!(
                        "a move holds copy quant gt4 xform fill gen gather scatter; not '{other}'"
                    ),
                ))
            }
        };
        let mut mv = mv;
        if on("l1.mov/seed-by-base") && mv.mode == MoveMode::Generate {
            if let (Some(s), Some(d)) = (mv.seed, &mv.dst) {
                let word = self.target.machine.die.word_bytes as i64;
                mv.seed = Some((s as i64).wrapping_add(d.base.div_euclid(word)) as u64);
            }
        }
        if on("l1.mov/row-pitch") {
            mv.row = mv.row.map(|(pitch, words)| (2 * pitch, words));
        }
        let writes = self.writes_of(st, &MoverOp::Move(mv))?;
        let patches = self.mover_patches(st, &writes, &fix)?;
        Ok((writes, patches))
    }

    /// Patches for `fix` (`(field, register, constant)`) in `writes`.
    pub(super) fn mover_patches(
        &self,
        st: &Stmt,
        writes: &[(u64, u64)],
        fix: &[(String, u64, i128)],
    ) -> Result<Vec<Patch>> {
        let regs: Vec<(u32, u64)> = writes.iter().map(|(r, v)| (*r as u32, *v)).collect();
        let at = self.isa.mover.fields(&regs);
        let mut out = Vec::new();
        for (name, reg, c) in fix {
            let key: (u32, String) = match name.as_str() {
                "imm" => (R_IMM, "imm".into()),
                "seed" => (R_SEED, "seed".into()),
                "index" => (R_INDEX, "index".into()),
                "count" => (R_INDEX, "count".into()),
                "pitch" => (R_ROW, "pitch".into()),
                "words" => (R_ROW, "words".into()),
                n if n.ends_with("base") => (R_HDR, n.to_string()),
                n => (R_DIM, n.to_string()),
            };
            let Some((_, (i, bit, width))) = at.iter().find(|(k, _)| *k == key) else {
                return Err(self.fail(
                    st,
                    "L1.MOV.016",
                    format!("no mover field {name} to set from a register"),
                ));
            };
            out.push(Patch {
                word: *i,
                segments: vec![(*bit, *width, 0)],
                reg: *reg,
                konst: *c as u64,
            });
        }
        Ok(out)
    }

    pub(super) fn base(&self, st: &Stmt, t: &Term, side: &str) -> Result<(i128, MoverFix)> {
        let (c, reg) = self.val(st, t)?;
        Ok((
            c,
            reg.map(|r| vec![(format!("{side}.base"), r, c)])
                .unwrap_or_default(),
        ))
    }

    /// `w(BASE, (C, S[, AXIS, ASTEP]), ..., win1=(B, E), win2=(B, E))`; a base
    /// or a count may be a register (L1.MOV.001, .004, .010, .011).
    pub(super) fn walk(&self, st: &Stmt, t: &Term, side: &str) -> Result<(Walk, MoverFix)> {
        let args = match t {
            Term::Call(n, args) if n == "w" && !args.is_empty() => args,
            _ => {
                return Err(self.fail(
                    st,
                    "L1.MOV.001",
                    format!(
                        "wanted a walk w(BASE, (COUNT, STRIDE), ...), got {}",
                        fmt(t)
                    ),
                ))
            }
        };
        let plain: Vec<&Term> = args.iter().filter(|a| !matches!(a, Term::Kw(..))).collect();
        let kws: HashMap<&str, &Term> = args
            .iter()
            .filter_map(|a| match a {
                Term::Kw(n, v) => Some((n.as_str(), v.as_ref())),
                _ => None,
            })
            .collect();
        let (base, mut fix) = self.base(st, plain[0], side)?;
        let mut dims = Vec::new();
        for (i, d) in plain[1..].iter().enumerate() {
            let items = match d {
                Term::Tuple(items) if items.len() == 2 || items.len() == 4 => items,
                _ => {
                    return Err(self.fail(
                        st,
                        "L1.MOV.011",
                        format!(
                            "a level is (COUNT, STRIDE) or (COUNT, STRIDE, AXIS, ASTEP), got {}",
                            fmt(d)
                        ),
                    ))
                }
            };
            let (c, reg) = self.val(st, &items[0])?;
            if let Some(r) = reg {
                fix.push((format!("{side}.count{i}"), r, c));
            }
            let rest: Vec<i128> = items[1..]
                .iter()
                .map(|x| self.const_(st, x))
                .collect::<Result<_>>()?;
            let mut level = Level::new(c as i64, rest[0] as i64);
            if rest.len() == 3 {
                level.axis = rest[1] as i64;
                level.astep = rest[2] as i64;
            }
            dims.push(level);
        }
        if dims.is_empty() {
            return Err(self.fail(st, "L1.MOV.011", "a walk has at least one level"));
        }
        let mut windows = [(0i64, 0i64); 2];
        for (slot, k) in ["win1", "win2"].iter().enumerate() {
            match kws.get(k) {
                None => {}
                Some(Term::Tuple(items)) if items.len() == 2 => {
                    windows[slot] = (
                        self.const_(st, &items[0])? as i64,
                        self.const_(st, &items[1])? as i64,
                    );
                }
                Some(_) => return Err(self.fail(st, "L1.MOV.004", format!("{k}=(BASE, EXTENT)"))),
            }
        }
        if on("l1.mov/levels-reversed") {
            dims.reverse();
        }
        if on("l1.mov/windows-dropped") {
            windows = [(0, 0); 2];
        }
        Ok((
            Walk {
                base: base as i64,
                dims,
                windows,
            },
            fix,
        ))
    }

    // ------------------------------------------------------------- waits
}
