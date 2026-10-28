//! Statements in order: blocks, loops, skips and the configuration set up before an op (L1.IMG.001-.012).

use super::*;

impl<'a> Encoder<'a> {
    pub fn new(
        reader: &'a Reader,
        isa: &'a VecIsa,
        facts: &'a CoreFacts,
        entry: State,
        units: Option<&'a dyn Units>,
    ) -> Encoder<'a> {
        Encoder {
            reader,
            isa,
            facts,
            entry,
            units,
            config: 0,
        }
    }

    pub(super) fn fail(&self, st: &Stmt, clause: &str, msg: impl Into<String>) -> Error {
        self.reader.fail(st, clause, msg)
    }

    pub(super) fn isa_err(&self, st: &Stmt, e: ktpu_machine::Refusal) -> Error {
        self.reader.fail(st, &e.clause, e.message)
    }

    pub(super) fn word(&self, st: &Stmt, w: VWord) -> Result<u64> {
        let mx7 = matches!(w, VWord::PackMx7 { .. });
        let got = mutated(w)
            .encode(self.isa)
            .map_err(|e| self.isa_err(st, e))?;
        if mx7 && on("num.vector/mx7-mode-2") {
            return Ok(got & !(3 << 20) | 2 << 20);
        }
        if mx7 && (on("l1.img/chunks") || on("num.vector/mx7-four-chunks")) {
            // The Python MX7 pack's walk spans CHUNKS chunks: n - 1 at bits 17..19.
            return Ok(got & !(7 << 17) | 3 << 17);
        }
        Ok(got)
    }

    /// The image's words: an `ainc aN 0` first for each stale descriptor.
    pub fn encode(&mut self, stmts: &[Stmt]) -> Result<Words> {
        let (words, _) = self.block(stmts, self.entry, 0)?;
        let mut out = Vec::new();
        let mut done = BTreeSet::new();
        let mut stale = Vec::new();
        self.stale(stmts, &mut done, &mut stale)?;
        if on("l1.img/no-stale") {
            stale.clear();
        }
        for ad in stale {
            let w = VWord::Ainc { ad, inc: 0 }.encode(self.isa)?;
            out.push(w);
        }
        out.extend(words);
        Ok(out)
    }

    pub fn block(
        &mut self,
        stmts: &[Stmt],
        mut state: State,
        depth: u32,
    ) -> Result<(Words, State)> {
        let mut out = Vec::new();
        for st in stmts {
            let (words, next) = self.stmt(st, state, depth)?;
            out.extend(words);
            state = next;
        }
        Ok((out, state))
    }

    pub fn stmt(&mut self, st: &Stmt, state: State, depth: u32) -> Result<(Words, State)> {
        if let Some(t) = &st.target {
            return Err(self.fail(
                st,
                "L1.IMG.004",
                format!("an image statement binds no name ({} = ...)", fmt(t)),
            ));
        }
        let op = st.op.as_str();
        if !st.body.is_empty() && op != "loop" && op != "skipz" {
            return Err(self.fail(st, "L1.IMG.004", format!("`{op}` takes no block")));
        }
        if ops::is_math(op) {
            return self.math(st, state);
        }
        if EXPLICIT.contains(&op) {
            return self.explicit(st, state);
        }
        match op {
            "loop" => return self.lp(st, state, depth),
            "skipz" => return self.skipz(st, state, depth),
            "vunpk" | "vpack" => return self.walk(st, state),
            _ => {}
        }
        let got = match op {
            "vfill" => self.vfill(st)?,
            "vdrain" => self.vdrain(st)?,
            "vsync" => self.vsync(st)?,
            "vwait" => self.vwait(st)?,
            "vpost" => self.vpost(st)?,
            "vmark" => self.vmark(st)?,
            "vbar" => self.vbar(st)?,
            "ainc" => self.ainc(st)?,
            "uptr" | "uinc" | "pptr" | "pinc" => self.pointer(st)?,
            "seti" => self.seti(st)?,
            "setvl" => self.setvl(st)?,
            "getstk" => self.getstk(st)?,
            "halt" => self.halt(st)?,
            "vcfg" => self.vcfg(st)?,
            "word" => self.raw_word(st)?,
            _ => {
                return Err(self.fail(st, "L1.IMG.034", format!("no V2 instruction '{op}'")));
            }
        };
        Ok(match op {
            "vcfg" | "word" => (got, UNKNOWN),
            "setvl" => (got, State { vl: None, ..state }),
            _ => (got, state),
        })
    }

    /// Descriptors a `rel` transfer steps before the image's own `ainc` sets
    /// them, in program order (L1.IMG.021).
    pub(super) fn stale(
        &self,
        stmts: &[Stmt],
        done: &mut BTreeSet<u32>,
        out: &mut Vec<u32>,
    ) -> Result<()> {
        if on("isa.vector/no_stale") {
            return Ok(());
        }
        for st in stmts {
            if st.op == "ainc" {
                if st.args.len() > 1 {
                    return Err(self.fail(st, "L1.IMG.020", "ainc aN += BYTES"));
                }
                if let Some(Term::AddAssign(lhs, _)) = st.args.first() {
                    done.insert(self.numbered(st, lhs, "a", self.facts.descriptors)?);
                }
            } else if st.op == "vfill" || st.op == "vdrain" {
                let arrow = if st.op == "vfill" { "->" } else { "<-" };
                let (ad, _, flags) = self.dma(st, arrow, &["signal"])?;
                if flags.contains("rel") && !done.contains(&ad) && !out.contains(&ad) {
                    out.push(ad);
                }
            }
            if !st.body.is_empty() {
                self.stale(&st.body, done, out)?;
            }
        }
        Ok(())
    }

    pub(super) fn lp(&mut self, st: &Stmt, state: State, depth: u32) -> Result<(Words, State)> {
        let reg = self.one(st, "loop sN")?;
        let sreg = self.sreg(st, reg)?;
        if depth > 0 {
            return Err(self.fail(
                st,
                "L1.IMG.050",
                "a loop inside a loop: VLOOP does not nest",
            ));
        }
        if st.body.is_empty() {
            return Err(self.fail(st, "L1.IMG.050", "an empty loop"));
        }
        // Entry as found, or the state the body's end leaves (set up once
        // before): the shorter body wins, then the shorter set-up.
        let hoisted = self.quiet(&st.body, UNKNOWN, depth + 1)?;
        type Best = ((usize, usize), Words, Words, State, usize);
        let mut best: Option<Best> = None;
        for (start, hoist) in [(state, false), (hoisted, true)] {
            let mut entry = start;
            for _ in 0..16 {
                let end = self.quiet(&st.body, entry, depth + 1)?;
                let next = meet(entry, end);
                if next == entry {
                    break;
                }
                entry = next;
            }
            let n = self.config;
            let pre = if hoist {
                self.setup(st, state, entry)?.0
            } else {
                Vec::new()
            };
            let (body, _) = self.block(&st.body, entry, depth + 1)?;
            let key = (body.len(), pre.len());
            if best.as_ref().is_none_or(|b| key < b.0) {
                best = Some((key, pre, body, entry, self.config));
            }
            self.config = n;
        }
        let (_, pre, body, entry, config) = best.unwrap();
        self.config = config;
        let head = self.word(
            st,
            VWord::Loop {
                sreg,
                body: body.len() as u32,
            },
        )?;
        let mut out = pre;
        out.push(head);
        out.extend(body);
        Ok((out, entry))
    }

    pub(super) fn skipz(&mut self, st: &Stmt, state: State, depth: u32) -> Result<(Words, State)> {
        let reg = self.one(st, "skipz sN")?;
        let sreg = self.sreg(st, reg)?;
        let (body, end) = self.block(&st.body, state, depth)?;
        let head = self.word(
            st,
            VWord::Skipz {
                sreg,
                n: body.len() as u32,
            },
        )?;
        let mut out = vec![head];
        out.extend(body);
        Ok((out, meet(state, end)))
    }

    /// The end state of a trial encoding, whose words are not counted.
    pub(super) fn quiet(&mut self, stmts: &[Stmt], state: State, depth: u32) -> Result<State> {
        if on("l1.img/no-hoist") {
            return Ok(UNKNOWN);
        }
        let n = self.config;
        let (_, end) = self.block(stmts, state, depth)?;
        self.config = n;
        Ok(end)
    }

    /// Words taking the core from `state` to the known fields of `want`.
    pub fn setup(&mut self, st: &Stmt, mut state: State, want: State) -> Result<(Words, State)> {
        if on("isa.vector/no_setup") {
            return Ok((Vec::new(), state));
        }
        if on("l1.img/no-tracking") {
            state = UNKNOWN;
        }
        let mut out = Vec::new();
        let mut chk = [DEF_NIB; 4];
        for (i, slot) in chk.iter_mut().enumerate() {
            *slot = want.chk[i].or(state.chk[i]).unwrap_or(DEF_NIB);
        }
        let vl_moves = want.vl.is_some() && want.vl != state.vl;
        let chk_moves = (0..4).any(|i| want.chk[i].is_some() && want.chk[i] != state.chk[i]);
        if vl_moves || chk_moves {
            let vl = want.vl.or(state.vl);
            let [a, b, c, d] = chk.map(|(x, y)| Chunk(x, y));
            let w = match vl {
                Some(vl) => VWord::Chkvl { vl, a, b, c, d },
                None => VWord::Chk { a, b, c, d },
            };
            out.push(w.encode(self.isa)?);
            state.vl = vl;
            state.chk = chk.map(Some);
        }
        if (0..6).any(|i| want.xb[i].is_some() && want.xb[i] != state.xb[i]) {
            let mut xb = [0u32; 6];
            for (i, slot) in xb.iter_mut().enumerate() {
                *slot = want.xb[i].or(state.xb[i]).unwrap_or(0);
            }
            out.push(self.word(st, xb_word(xb))?);
            state.xb = xb.map(Some);
        }
        if let Some(w) = want.ustr {
            if Some(w) != state.ustr {
                out.push(self.word(st, stride_word(CFG_USTR, w))?);
                state.ustr = Some(w);
            }
        }
        if let Some(w) = want.pstr {
            if Some(w) != state.pstr {
                out.push(self.word(st, stride_word(CFG_PSTR, w))?);
                state.pstr = Some(w);
            }
        }
        self.config += out.len();
        Ok((out, state))
    }

    // ------------------------------------------------ explicit configuration
}
