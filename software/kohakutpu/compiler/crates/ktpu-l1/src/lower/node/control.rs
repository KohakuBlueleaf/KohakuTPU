//! The node's own statements: register ALU ops, branches, the ring, writes, pushes, receives, queries, waits and fetches (L1.NODE.*).

use super::*;

impl<'a> NodeReader<'a> {
    pub(super) fn register_op(&mut self, st: &Stmt) -> Result<()> {
        let rd = self.reg(st, st.target.as_ref().unwrap())?;
        let pos = st.positional();
        if st.op == "load" {
            if pos.len() != 1 {
                return Err(self.fail(st, "L1.NODE.026", "rN = load ADDR [+ rM]"));
            }
            let (c, index) = self.val(st, pos[0])?;
            let index = index.filter(|_| !on("l1.node/load-index-dropped"));
            self.p.load(rd, c as u64, index, false);
            return Ok(());
        }
        if st.op == "ctrl" {
            let t = self.one(st)?;
            let off = self.offset(st, t)?;
            self.p.load(rd, off, None, true);
            return Ok(());
        }
        let op = alu_of(&st.op).unwrap();
        let (a, b) = if op == Alu::Mov {
            if pos.len() != 1 {
                return Err(self.fail(st, "L1.NODE.025", "rN = set VALUE | rM"));
            }
            (0, pos[0])
        } else {
            if pos.len() != 2 {
                return Err(self.fail(st, "L1.NODE.025", format!("rN = {} rA, B", st.op)));
            }
            (self.reg(st, pos[0])?, pos[1])
        };
        if Self::is_reg(b) {
            let rb = self.reg(st, b)?;
            self.p.alu(rd, op, a, rb, false);
        } else if let Term::Int(v, _) = b {
            self.p.alu(rd, op, a, (v & MASK64) as u64, true);
        } else {
            return Err(self.fail(
                st,
                "L1.NODE.025",
                format!("an ALU operand is a constant or a register, got {}", fmt(b)),
            ));
        }
        if on("l1.lower/alu-early") {
            let sends = self
                .p
                .steps
                .iter()
                .rposition(|s| matches!(s, PStmt::Send { .. }));
            if let Some(at) = sends {
                let alu = self.p.steps.pop().unwrap();
                self.p.steps.insert(at, alu);
            }
        }
        Ok(())
    }

    pub(super) fn goto(&mut self, st: &Stmt) -> Result<()> {
        let all = st.positional();
        let signed = all.iter().any(|p| p.as_name() == Some("signed"));
        let pos: Vec<&Term> = all
            .into_iter()
            .filter(|p| p.as_name() != Some("signed"))
            .collect();
        let target = match pos.first() {
            Some(t) => Some(self.name(st, t)?.to_string()),
            None => None,
        };
        if pos.len() == 1 {
            self.p
                .jump(target.as_deref().unwrap(), Cond::Always, 0, 0, true);
            return Ok(());
        }
        let form = "goto LABEL [if rA CMP B] [signed]";
        let cmp = match pos.as_slice() {
            [_, Term::Name(i), c @ Term::Compare(..)] if i == "if" => c,
            _ => return Err(self.fail(st, "L1.NODE.028", form)),
        };
        let Term::Compare(op, ca, cb) = cmp else {
            unreachable!()
        };
        let a = self.reg(st, ca)?;
        let signed = signed && !on("l1.node/compare-unsigned");
        let (lt, ge) = if signed || on("l1.node/goto-signed") {
            (Cond::Lt, Cond::Ge)
        } else {
            (Cond::Ltu, Cond::Geu)
        };
        let target = target.unwrap();
        if Self::is_reg(cb) {
            let rb = self.reg(st, cb)?;
            let (cond, ra, rb) = match op.as_str() {
                "==" => (Cond::Eq, a, rb),
                "!=" => (Cond::Ne, a, rb),
                "<" => (lt, a, rb),
                ">=" => (ge, a, rb),
                ">" => (lt, rb, a),
                _ => (ge, rb, a),
            };
            self.p.jump(&target, cond, ra, rb, false);
            return Ok(());
        }
        let k = self.const_(st, cb)?;
        let (cond, k) = match op.as_str() {
            "==" => (Cond::Eq, k),
            "!=" => (Cond::Ne, k),
            "<" => (lt, k),
            ">=" => (ge, k),
            ">" => (ge, k + 1),
            _ => (lt, k + 1),
        };
        self.p.jump(&target, cond, a, (k & MASK64) as u64, true);
        Ok(())
    }

    // ------------------------------------------------ interlink and memory
    pub(super) fn ring(&mut self, st: &Stmt) -> Result<()> {
        let kw = st.kwargs();
        let flags: BTreeSet<&str> = st.positional().iter().filter_map(|p| p.as_name()).collect();
        let bad_kw = kw
            .iter()
            .any(|(k, _)| !matches!(*k, "mesh" | "slot" | "amount"));
        if st.kw("mesh").is_none() || flags.iter().any(|f| *f != "plain") || bad_kw {
            return Err(self.fail(st, "L1.NODE.029", "ring mesh=M [slot=S] [amount=N] [plain]"));
        }
        let mesh = self.const_(st, st.kw("mesh").unwrap())? as u64;
        let slot = self.kconst(st, "slot", 0)? as u64;
        let amount = self.kconst(st, "amount", 1)? as u64;
        let fence = !flags.contains("plain") && !on("l1.node/ring-unfenced");
        self.p.ring(mesh, slot, amount, fence);
        Ok(())
    }

    pub(super) fn offset(&self, st: &Stmt, t: &Term) -> Result<u64> {
        if let Some(n) = t.as_name() {
            if let Some((_, v)) = CTRL.iter().find(|(k, _)| *k == n) {
                return Ok(*v + if on("l1.node/ctrl-offsets") { 8 } else { 0 });
            }
        }
        Ok(self.const_(st, t)? as u64)
    }

    pub(super) fn write(&mut self, st: &Stmt) -> Result<()> {
        let pos = st.positional();
        let ctrl = st.op == "ctrl";
        if pos.len() != 3 || !pos[1].is_arrow("<-") {
            let what = if ctrl { "OFFSET" } else { "ADDR" };
            return Err(self.fail(st, "L1.NODE.007", format!("{} {what} <- VALUE | rN", st.op)));
        }
        let at = if ctrl {
            self.offset(st, pos[0])?
        } else {
            self.const_(st, pos[0])? as u64
        };
        let regs = Self::is_reg(pos[2]);
        let v = if regs {
            self.reg(st, pos[2])?
        } else {
            (self.const_(st, pos[2])? & MASK64) as u64
        };
        if ctrl {
            self.p.ctrl(vec![(at, v)], regs);
        } else {
            self.p.store(vec![(at, v)], regs);
        }
        Ok(())
    }

    pub(super) fn push(&mut self, st: &Stmt) -> Result<()> {
        let all = st.positional();
        let flags: BTreeSet<&str> = all.iter().filter_map(|p| p.as_name()).collect();
        let pos: Vec<&Term> = all.into_iter().filter(|p| p.as_name().is_none()).collect();
        if pos.len() != 3 || !pos[1].is_arrow("<-") || st.kw("n").is_none() {
            return Err(self.fail(
                st,
                "L1.NODE.013",
                "push UNIT buf=B at=OFF <- ADDR n=N [signal] [ack=UNIT] [wait]",
            ));
        }
        let c = self.unit(st, pos[0])?;
        let n = self.const_(st, st.kw("n").unwrap())?;
        let burst = self.target.machine.unit.burst_words as i128;
        if !(1..=burst).contains(&n) {
            return Err(self.fail(
                st,
                "L1.NODE.032",
                format!("a CU_DATA burst is 1..{burst} words, got {n}"),
            ));
        }
        let ack = match st.kw("ack") {
            Some(t) => self.coord(st, t)?,
            None => (0, 0),
        };
        let flag = (flags.contains("signal") || flags.contains("wait")) as u64;
        let mut desc = Word::ZERO;
        for (v, bit) in [
            (self.kconst(st, "buf", 0)? as u64, CD_BUF),
            (self.kconst(st, "at", 0)? as u64, CD_OFF),
            ((n - 1) as u64, CD_LEN + on("l1.node/push-len-bit") as u32),
            (flag, CD_FLAGS),
            (ack.1 as u64, CD_ACK_Y),
            (ack.0 as u64, CD_ACK_X),
        ] {
            desc |= Word::placed(v, bit);
        }
        let addr = self.const_(st, pos[2])? as u64;
        self.p.push(c, desc, addr, n as u64, flags.contains("wait"));
        Ok(())
    }

    pub(super) fn recv(&mut self, st: &Stmt) -> Result<()> {
        let pos = st.positional();
        if pos.len() != 2 || !pos[0].is_arrow("->") || st.kw("n").is_none() {
            return Err(self.fail(st, "L1.NODE.014", "recv n=N -> ADDR [from=UNIT]"));
        }
        let src = match st.kw("from") {
            Some(t) => Some(self.unit(st, t)?),
            None => None,
        };
        let addr = self.const_(st, pos[1])? as u64;
        let n = self.const_(st, st.kw("n").unwrap())? as u64;
        self.p
            .recv(addr, n, src.filter(|_| !on("l1.node/recv-any")));
        Ok(())
    }

    pub(super) fn query(&mut self, st: &Stmt) -> Result<()> {
        let pos = st.positional();
        if pos.len() != 3 || !pos[1].is_arrow("->") || st.kw("index").is_none() {
            return Err(self.fail(st, "L1.NODE.015", "query UNIT index=I -> ADDR"));
        }
        let c = self.unit(st, pos[0])?;
        let index = self.const_(st, st.kw("index").unwrap())? as u64;
        let at = CTRL_INDEX - if on("l1.node/query-index-bit") { 8 } else { 0 };
        self.p
            .send_raw(c, CU_CTRL, vec![Word::placed(index, at)], false);
        let addr = self.const_(st, pos[2])? as u64;
        let from = (!on("l1.node/recv-any")).then_some(c);
        self.p.recv(addr, 1, from);
        Ok(())
    }

    // -------------------------------------------------------------- sends
    pub(super) fn wait(&mut self, st: &Stmt) -> Result<()> {
        let pos = st.positional();
        match pos.first().and_then(|p| p.as_name()) {
            Some("moves") => {
                let got = match pos.as_slice() {
                    [_, Term::Name(t)] => self.tokens.get(t).copied(),
                    _ => None,
                };
                let Some(Token::Moves(token)) = got else {
                    return Err(self.fail(
                        st,
                        "L1.NODE.036",
                        "wait moves T, T from `T = move posted`",
                    ));
                };
                let got = self.p.wait_moves(token).map(|_| ());
                got.map_err(|e| self.at(st, ref_err(e)))
            }
            Some("slot") => {
                let ok = matches!(pos.len(), 2 | 3)
                    && st.kw("n").is_some()
                    && (pos.len() == 2 || pos[2].as_name() == Some("consume"));
                if !ok {
                    return Err(self.fail(st, "L1.NODE.011", "wait slot S n=N [consume]"));
                }
                let slot = self.const_(st, pos[1])? as u64;
                let n = self.const_(st, st.kw("n").unwrap())? as u64;
                let consume = pos.len() == 3 && !on("l1.node/slot-keeps");
                if !on("l1.node/no-wait-slot") {
                    self.p.wait_slot(slot, n, consume);
                }
                Ok(())
            }
            Some("bell") => {
                if pos.len() != 2 {
                    return Err(self.fail(st, "L1.NODE.011", "wait bell MESH [n=N]"));
                }
                let mesh = self.const_(st, pos[1])? as u64;
                let n = self.kconst(st, "n", 1)? as u64;
                self.p.wait_bell(mesh, n);
                Ok(())
            }
            _ => {
                let t = self.one(st)?;
                let c = self.unit(st, t)?;
                let token = match st.kw("upto") {
                    None => None,
                    Some(t) => match t.as_name().and_then(|n| self.tokens.get(n)) {
                        Some(Token::Mark(k)) => Some(*k),
                        _ => {
                            return Err(self.fail(
                                st,
                                "L1.NODE.034",
                                format!("upto= names a mark, got {}", fmt(t)),
                            ))
                        }
                    },
                };
                if on("l1.node/no-wait") {
                    return Ok(());
                }
                let token = token.filter(|_| !on("l1.node/wait-ignores-mark"));
                let got = self.p.wait(c, token).map(|_| ());
                got.map_err(|e| self.at(st, ref_err(e)))
            }
        }
    }

    pub(super) fn fetch(&mut self, st: &Stmt) -> Result<()> {
        let pos = st.positional();
        if pos.len() != 3 || pos[1].as_name() != Some("from") || st.kw("n").is_none() {
            return Err(self.fail(st, "L1.NODE.016", "fetch UNIT from ADDR n=N"));
        }
        let c = self.unit(st, pos[0])?;
        let (k, reg) = self.val(st, pos[2])?;
        let bits = self.target.machine.die.noc.addr_bits;
        let patches = match reg {
            None => Vec::new(),
            Some(r) => vec![Patch {
                word: 0,
                segments: vec![(64, bits, 0)],
                reg: r,
                konst: k as u64,
            }],
        };
        let n = self.const_(st, st.kw("n").unwrap())? as u64;
        self.p.fetch(c, k as u64, n, patches);
        Ok(())
    }

    // ------------------------------------------------------------ terms
}
