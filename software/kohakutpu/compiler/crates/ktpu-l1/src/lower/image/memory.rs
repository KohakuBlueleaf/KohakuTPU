//! Walks, fills and drains between L1 and the register file (L1.IMG.020-.029).

use super::*;

impl<'a> Encoder<'a> {
    pub(super) fn walk(&mut self, st: &Stmt, state: State) -> Result<(Words, State)> {
        let pos = st.positional();
        let unpack = st.op == "vunpk";
        let arrow = if unpack { "<-" } else { "->" };
        if pos.len() < 4 || !pos[1].is_arrow(arrow) || pos[3].as_name().is_none() {
            return Err(self.fail(
                st,
                "L1.IMG.013",
                format!("{} vN {arrow} l1[OFF] MODE ...", st.op),
            ));
        }
        let flags: BTreeSet<&str> = pos[4..].iter().filter_map(|p| name_of(p)).collect();
        if flags.iter().any(|f| *f != "rel") || pos.len() - 4 != flags.len() {
            let rest: Vec<String> = pos[4..].iter().map(|p| fmt(p)).collect();
            return Err(self.fail(
                st,
                "L1.IMG.040",
                format!("{} takes the flag rel; got [{}]", st.op, rest.join(", ")),
            ));
        }
        let (reg, nib) = self.dest(st, pos[0], false)?;
        let off = self.l1(st, pos[2])? as u32;
        let mode = pos[3].as_name().unwrap();
        let q = self.queue(st)?;
        let rel = flags.contains("rel");
        let walk = match st.kw("walk") {
            Some(t) => {
                let (a, b) = self.pair(st, t)?;
                (a as i64, b as i64)
            }
            None => FLAT,
        };
        let want = if unpack {
            State {
                ustr: Some(walk),
                ..UNKNOWN
            }
        } else {
            State {
                pstr: Some(walk),
                ..UNKNOWN
            }
        };
        let (mut pre, state) = self.setup(st, state, want)?;
        let word = if mode == "mx7a" || mode == "mx7b" {
            if unpack || nib.is_some() || st.kw("n").is_some() {
                return Err(self.fail(
                    st,
                    "L1.IMG.041",
                    "an MX7 pack takes a whole register: vpack vN -> l1[OFF] mx7a",
                ));
            }
            VWord::PackMx7 {
                vs: reg,
                off,
                b_layout: mode == "mx7b",
                rel,
                q,
            }
        } else {
            let m = match mode {
                "f16" if crate::mutant::on("num.image/modes-swapped") => WalkMode::Gt4,
                "gt4" if crate::mutant::on("num.image/modes-swapped") => WalkMode::F16,
                "f16" => WalkMode::F16,
                "gt4" => WalkMode::Gt4,
                "f32" => WalkMode::F32,
                _ => {
                    return Err(self.fail(
                        st,
                        "L1.IMG.013",
                        format!("mode {mode}: f16, gt4, f32 (packs also mx7a mx7b)"),
                    ))
                }
            };
            if let Some((_, s)) = nib {
                if s != 1 {
                    return Err(self.fail(st, "L1.IMG.040", "a walk starts at a chunk: vN[c:]"));
                }
            }
            let cb = nib.map(|(b, _)| b).unwrap_or(0);
            let n = match st.kw("n") {
                Some(t) => self.int(st, t)? as u32,
                None => self.facts.chunks - cb,
            };
            if unpack {
                VWord::Unpk {
                    vd: reg,
                    off,
                    n,
                    mode: m,
                    cb,
                    rel,
                    q,
                }
            } else {
                VWord::Pack {
                    vs: reg,
                    off,
                    n,
                    mode: m,
                    cb,
                    rel,
                    q,
                }
            }
        };
        pre.push(self.word(st, word)?);
        Ok((pre, state))
    }

    // -------------------------------------------------------- the rest
    pub(super) fn vfill(&self, st: &Stmt) -> Result<Words> {
        let (ad, off, flags) = self.dma(st, "->", &[])?;
        Ok(vec![self.word(
            st,
            VWord::Fill {
                ad,
                l1off: off,
                rel: flags.contains("rel"),
            },
        )?])
    }

    pub(super) fn vdrain(&self, st: &Stmt) -> Result<Words> {
        let (ad, off, flags) = self.dma(st, "<-", &["signal"])?;
        let rel = flags.contains("rel");
        let Some(to) = st.kw("to") else {
            if st.kw("buf").is_some() || flags.contains("signal") {
                return Err(self.fail(
                    st,
                    "L1.IMG.043",
                    "buf= and signal are a peer drain's (to=(x, y))",
                ));
            }
            return Ok(vec![self.word(
                st,
                VWord::Drain {
                    ad,
                    l1off: off,
                    rel,
                    node: None,
                    buf_id: 0,
                    signal: false,
                },
            )?]);
        };
        let (x, y) = coord(self.reader, st, to, self.units, true)?;
        let buf = match st.kw("buf") {
            Some(t) => self.int(st, t)? as u32,
            None => 0,
        };
        Ok(vec![self.word(
            st,
            VWord::Drain {
                ad,
                l1off: off,
                rel,
                node: Some(Coord(x, y)),
                buf_id: buf,
                signal: flags.contains("signal"),
            },
        )?])
    }

    pub(super) fn dma(
        &self,
        st: &Stmt,
        arrow: &str,
        extra: &[&str],
    ) -> Result<(u32, u32, BTreeSet<String>)> {
        let pos = st.positional();
        if pos.len() < 3 || !pos[1].is_arrow(arrow) {
            return Err(self.fail(
                st,
                "L1.IMG.015",
                format!("{} aN {arrow} l1[OFF] [rel]", st.op),
            ));
        }
        let flags: Vec<String> = pos[3..]
            .iter()
            .filter_map(|p| name_of(p))
            .map(String::from)
            .collect();
        if flags.len() != pos.len() - 3
            || flags
                .iter()
                .any(|f| f != "rel" && !extra.contains(&f.as_str()))
        {
            return Err(self.fail(
                st,
                "L1.IMG.015",
                format!("{} flags: rel {}", st.op, extra.join(" ")),
            ));
        }
        let ad = self.numbered(st, pos[0], "a", self.facts.descriptors)?;
        let off = self.l1(st, pos[2])? as u32;
        Ok((ad, off, flags.into_iter().collect()))
    }
}
