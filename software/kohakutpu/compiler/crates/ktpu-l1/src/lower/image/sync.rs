//! Synchronisation and control words: sync, wait, post, mark, barriers, pointers, seti, vl, halt, config and raw words.

use super::*;

impl<'a> Encoder<'a> {
    pub(super) fn vsync(&self, st: &Stmt) -> Result<Words> {
        let pos = st.positional();
        let waiter = match (pos.len(), pos.first().and_then(|p| name_of(p))) {
            (1, Some(n)) => lookup(&waiters(), n),
            _ => None,
        };
        let Some(waiter) = waiter else {
            return Err(self.fail(
                st,
                "L1.IMG.016",
                format!("vsync WAITER ...: {}", names(&WAITERS)),
            ));
        };
        let q = self.queue(st)?;
        let (on, mark) = (st.kw("on"), st.kw("mark"));
        if on.is_some() == mark.is_some() {
            return Err(self.fail(st, "L1.IMG.044", "vsync waits on=KIND or mark=mN"));
        }
        if let Some(m) = mark {
            if st.kw("slack").is_some() {
                return Err(self.fail(st, "L1.IMG.044", "a mark's slack is the vmark's"));
            }
            let mark = self.numbered(st, m, "m", self.facts.marks)?;
            return Ok(vec![self.word(
                st,
                VWord::Sync {
                    waiter,
                    on: 0,
                    slack: 0,
                    mark: Some(mark),
                    q,
                },
            )?]);
        }
        let slack = match st.kw("slack") {
            Some(t) => self.int(st, t)? as u32,
            None => 0,
        };
        let on = self.kind(st, on.unwrap())?;
        Ok(vec![self.word(
            st,
            VWord::Sync {
                waiter,
                on,
                slack,
                mark: None,
                q,
            },
        )?])
    }

    pub(super) fn vwait(&self, st: &Stmt) -> Result<Words> {
        let pos = st.positional();
        let form = "vwait WAITER rx|cr n=N [q=Q]";
        let kws = st.kwargs();
        let ok = pos.len() == 2
            && pos[0]
                .as_name()
                .is_some_and(|n| lookup(&waiters(), n).is_some())
            && matches!(pos[1].as_name(), Some("rx" | "cr"))
            && st.kw("n").is_some()
            && kws.iter().all(|(k, _)| *k == "n" || *k == "q");
        if !ok && !on("l1.img/vwait-any") {
            return Err(self.fail(st, "L1.IMG.017", form));
        }
        let waiter = lookup(&waiters(), pos[0].as_name().unwrap()).unwrap();
        if waiter == W_MK {
            return Err(self.fail(st, "L1.IMG.045", "a count is waited on, not marked"));
        }
        let counter = if pos[1].as_name() == Some("cr") {
            CR
        } else {
            RX
        };
        let n = self.int(st, st.kw("n").unwrap())? as u32;
        let q = self.queue(st)?;
        Ok(vec![self.word(
            st,
            VWord::Wait {
                waiter,
                counter,
                n,
                q,
            },
        )?])
    }

    pub(super) fn vpost(&self, st: &Stmt) -> Result<Words> {
        let pos = st.positional();
        let kws = st.kwargs();
        if pos.len() != 1 || kws.len() != 1 || kws[0].0 != "n" {
            return Err(self.fail(st, "L1.IMG.045", "vpost UNIT n=CREDITS"));
        }
        let (x, y) = coord(self.reader, st, pos[0], self.units, true)?;
        let n = self.int(st, kws[0].1)? as u32;
        Ok(vec![self.word(
            st,
            VWord::Post {
                dst: Coord(x, y),
                n,
            },
        )?])
    }

    pub(super) fn vmark(&self, st: &Stmt) -> Result<Words> {
        let pos = st.positional();
        let kws = st.kwargs();
        if pos.len() != 1
            || st.kw("on").is_none()
            || kws.iter().any(|(k, _)| *k != "on" && *k != "slack")
        {
            return Err(self.fail(st, "L1.IMG.019", "vmark mN on=KIND [slack=N]"));
        }
        let mark = self.numbered(st, pos[0], "m", self.facts.marks)?;
        let on = self.kind(st, st.kw("on").unwrap())?;
        let slack = match st.kw("slack") {
            Some(t) => self.int(st, t)? as u32,
            None => 0,
        };
        Ok(vec![self.word(st, VWord::Mark { mark, on, slack })?])
    }

    pub(super) fn vbar(&self, st: &Stmt) -> Result<Words> {
        if !st.positional().is_empty() {
            return Err(self.fail(st, "L1.IMG.046", "vbar [q=1]"));
        }
        let q = self.queue(st)?;
        Ok(vec![self.word(st, VWord::Bar { q })?])
    }

    pub(super) fn ainc(&self, st: &Stmt) -> Result<Words> {
        let (lhs, rhs) = match st.args.as_slice() {
            [Term::AddAssign(a, b)] => (a.as_ref(), b.as_ref()),
            _ => return Err(self.fail(st, "L1.IMG.020", "ainc aN += BYTES")),
        };
        let inc = self.int(st, rhs)?;
        let half = 1i128 << (self.facts.ainc_bits - 1);
        if !(-half..half).contains(&inc) {
            return Err(self.fail(
                st,
                "L1.IMG.047",
                format!("ainc {inc}: signed {} bits", self.facts.ainc_bits),
            ));
        }
        let ad = self.numbered(st, lhs, "a", self.facts.descriptors)?;
        Ok(vec![self.word(
            st,
            VWord::Ainc {
                ad,
                inc: inc as i32,
            },
        )?])
    }

    pub(super) fn pointer(&self, st: &Stmt) -> Result<Words> {
        let swap = on("l1.img/pointers");
        let reg = match st.op.as_str() {
            "uptr" if swap => PtrReg::Uinc,
            "uinc" if swap => PtrReg::Uptr,
            "uptr" => PtrReg::Uptr,
            "uinc" => PtrReg::Uinc,
            "pptr" => PtrReg::Pptr,
            _ => PtrReg::Pinc,
        };
        let t = self.one(st, &format!("{} N", st.op))?;
        let value = self.int(st, t)? as u32;
        Ok(vec![self.word(st, VWord::Ptr { reg, value })?])
    }

    pub(super) fn seti(&self, st: &Stmt) -> Result<Words> {
        let (lhs, v) = match st.args.as_slice() {
            [Term::Assign(a, b)] if a.as_name().is_some() => (a.as_ref(), b.as_ref()),
            [Term::Assign(a, b), Term::Assign(k, v)]
                if a.as_name().is_some()
                    && k.as_name() == Some("classed")
                    && v.as_int() == Some(1) =>
            {
                (a.as_ref(), b.as_ref())
            }
            _ => return Err(self.fail(st, "L1.IMG.023", "seti sN = VALUE / seti k3 = VALUE")),
        };
        let (sreg, to_k) = if lhs.as_name() == Some("k3") {
            (0, true)
        } else {
            (self.numbered(st, lhs, "s", self.facts.sregs)?, false)
        };
        let value = match v {
            Term::Float(f) => e8m15(*f) as u64 ^ on("l1.img/e8m15") as u64,
            Term::Int(i, _) => (*i as u64) & 0xFF_FFFF,
            _ => {
                return Err(self.fail(
                    st,
                    "L1.IMG.049",
                    format!("seti takes a constant, got {}", fmt(v)),
                ))
            }
        };
        Ok(vec![self.word(st, VWord::Seti { sreg, to_k })?, value])
    }

    pub(super) fn setvl(&self, st: &Stmt) -> Result<Words> {
        let t = self.one(st, "setvl sN")?;
        let sreg = self.sreg(st, t)?;
        Ok(vec![self.word(st, VWord::Setvl { sreg })?])
    }

    pub(super) fn getstk(&self, st: &Stmt) -> Result<Words> {
        let t = self.one(st, "getstk sN")?;
        let sreg = self.sreg(st, t)?;
        Ok(vec![self.word(st, VWord::Getstk { sreg })?])
    }

    pub(super) fn halt(&self, st: &Stmt) -> Result<Words> {
        if !st.args.is_empty() {
            return Err(self.fail(st, "L1.IMG.031", "halt takes nothing"));
        }
        Ok(vec![self.word(st, VWord::Halt)?])
    }

    pub(super) fn vcfg(&self, st: &Stmt) -> Result<Words> {
        let pos = st.positional();
        if pos.len() != 2 {
            return Err(self.fail(st, "L1.IMG.029", "vcfg SEL PAYLOAD"));
        }
        let sel = self.int(st, pos[0])? as u32;
        let payload = self.int(st, pos[1])? as u32;
        Ok(vec![self.word(st, VWord::Vcfg { sel, payload })?])
    }

    pub(super) fn raw_word(&self, st: &Stmt) -> Result<Words> {
        let t = self.one(st, "word W")?;
        let w = self.int(st, t)?;
        if !(0..1i128 << 32).contains(&w) {
            return Err(self.fail(st, "L1.IMG.029", format!("word {w:#x}: 32 bits")));
        }
        Ok(vec![w as u64])
    }

    // ------------------------------------------------------------ terms
}
