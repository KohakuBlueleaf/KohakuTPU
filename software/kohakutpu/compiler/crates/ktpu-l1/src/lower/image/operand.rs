//! Operand readers shared by the ops: integers, names, pairs, numbered registers, queues and L1 words.

use super::*;

pub(super) fn xb_word(xb: [u32; 6]) -> VWord {
    VWord::Xb {
        xm: xb[0],
        xk: xb[1],
        sh: xb[2],
        xc: xb[3] != 0,
        pm: xb[4],
        pr: xb[5],
    }
}

pub(super) fn stride_word(sel: u64, (s0, s1): (i64, i64)) -> VWord {
    VWord::Stride {
        reg: if sel == CFG_USTR {
            StrideReg::Ustr
        } else {
            StrideReg::Pstr
        },
        s0: s0 as i32,
        s1: s1 as i32,
    }
}

impl<'a> Encoder<'a> {
    pub(super) fn one<'s>(&self, st: &'s Stmt, form: &str) -> Result<&'s Term> {
        let pos = st.positional();
        if pos.len() != 1 || !st.kwargs().is_empty() {
            return Err(self.fail(st, "L1.IMG.034", format!("wanted `{form}`")));
        }
        Ok(pos[0])
    }

    /// The `q=` queue; queue 1 only on a core with a second queue (RUL.003).
    pub(super) fn queue(&self, st: &Stmt) -> Result<u32> {
        let q = match st.kw("q") {
            Some(t) => self.int(st, t)? as u32,
            None => 0,
        };
        if q != 0 && !self.facts.dualq && !crate::mutant::on("rul.003/q1") {
            return Err(self.fail(
                st,
                "RUL.003",
                format!("q={q}: this core has no second queue (vc.dualq 0)"),
            ));
        }
        Ok(q)
    }

    /// A sampled parameter retains its identity through expansion and state tracking.
    pub(super) fn configuration_int(&self, st: &Stmt, t: &Term) -> Result<i128> {
        if let Term::Call(name, args) = t {
            if name == "parameter" {
                if let [Term::Str(id), value] = args.as_slice() {
                    if !id.is_empty() {
                        return self.int(st, value);
                    }
                }
                return Err(self.fail(
                    st,
                    "L1.IMG.001",
                    "parameter wants a nonempty identity and sampled integer",
                ));
            }
        }
        self.int(st, t)
    }

    pub(super) fn int(&self, st: &Stmt, t: &Term) -> Result<i128> {
        t.as_int().ok_or_else(|| {
            self.fail(
                st,
                "L1.IMG.001",
                format!("wanted a constant integer, got {}", fmt(t)),
            )
        })
    }

    pub(super) fn name<'t>(&self, st: &Stmt, t: &'t Term) -> Result<&'t str> {
        t.as_name()
            .ok_or_else(|| self.fail(st, "L1.IMG.029", format!("wanted a name, got {}", fmt(t))))
    }

    pub(super) fn pair(&self, st: &Stmt, t: &Term) -> Result<(i128, i128)> {
        match t {
            Term::Tuple(items) if items.len() == 2 => {
                Ok((self.int(st, &items[0])?, self.int(st, &items[1])?))
            }
            _ => Err(self.fail(st, "L1.IMG.013", format!("wanted (A, B), got {}", fmt(t)))),
        }
    }

    pub(super) fn numbered(&self, st: &Stmt, t: &Term, prefix: &str, limit: u32) -> Result<u32> {
        let limit = limit + crate::mutant::on("isa.vector/limits") as u32;
        if let Term::Name(text) = t {
            if let Some(rest) = text.strip_prefix(prefix) {
                if !rest.is_empty() && rest.chars().all(|c| c.is_ascii_digit()) {
                    let n: u64 = rest.parse().unwrap_or(u64::MAX);
                    if n < limit as u64 {
                        return Ok(n as u32);
                    }
                }
            }
        }
        Err(self.fail(
            st,
            "L1.IMG.008",
            format!("wanted {prefix}0..{prefix}{}, got {}", limit - 1, fmt(t)),
        ))
    }

    pub(super) fn sreg(&self, st: &Stmt, t: &Term) -> Result<u32> {
        self.numbered(st, t, "s", self.facts.sregs)
    }

    pub(super) fn kind(&self, st: &Stmt, t: &Term) -> Result<u32> {
        t.as_name()
            .and_then(|n| lookup(&kinds(), n))
            .ok_or_else(|| self.fail(st, "L1.IMG.016", format!("on=KIND: {}", names(&KINDS))))
    }

    pub(super) fn l1(&self, st: &Stmt, t: &Term) -> Result<i128> {
        match t {
            Term::View(n, s) if n == "l1" && s.len() == 1 => self.int(st, &s[0]),
            _ => Err(self.fail(
                st,
                "L1.IMG.013",
                format!("wanted l1[OFFSET], got {}", fmt(t)),
            )),
        }
    }
}
