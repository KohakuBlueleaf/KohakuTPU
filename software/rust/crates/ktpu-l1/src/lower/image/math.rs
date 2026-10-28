//! Math ops and their operands: destinations, sources, chunk selects, predicates and vl (L1.IMG.030-.040).

use super::*;

/// A math source: register `reg` of `sel` (V/S/K), its chunk select, and a
/// crossbar `(xm, xk, sh)` on it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct Src {
    reg: u32,
    sel: Sel,
    nib: Option<Nib>,
    xbar: Option<(u32, u32, u32)>,
    parameter_xb: u8,
    parameter_ids: [Option<String>; 2],
}

fn parameter_id(t: &Term) -> Option<String> {
    if let Term::Call(name, args) = t {
        if name == "parameter" {
            if let Some(Term::Str(id)) = args.first() {
                return Some(id.clone());
            }
        }
    }
    None
}

fn is_parameter(t: &Term) -> bool {
    matches!(t, Term::Call(name, _) if name == "parameter")
}

impl<'a> Encoder<'a> {
    pub(super) fn explicit(&mut self, st: &Stmt, mut state: State) -> Result<(Words, State)> {
        let pos = st.positional();
        match st.op.as_str() {
            "chk" | "chkvl" => {
                let mut nibs = [DEF_NIB; 4];
                for (i, x) in ["a", "b", "c", "d"].iter().enumerate() {
                    if let Some(t) = st.kw(x) {
                        let (p, q) = self.pair(st, t)?;
                        nibs[i] = (p as u32, q as u32);
                    }
                }
                let [a, b, c, d] = nibs.map(|(x, y)| Chunk(x, y));
                state.chk = nibs.map(Some);
                if st.op == "chk" {
                    return Ok((vec![self.word(st, VWord::Chk { a, b, c, d })?], state));
                }
                let vl = match st.kw("vl") {
                    Some(t) => self.int(st, t)? as u32,
                    None => self.facts.vlmax,
                };
                state.vl = Some(vl);
                Ok((vec![self.word(st, VWord::Chkvl { vl, a, b, c, d })?], state))
            }
            "xb" => {
                let flags: BTreeSet<&str> = pos.iter().filter_map(|p| name_of(p)).collect();
                let xm = match st.kw("mode") {
                    Some(t) => {
                        let n = self.name(st, t)?;
                        lookup(&xmodes(), n).ok_or_else(|| {
                            self.fail(
                                st,
                                "L1.IMG.029",
                                format!("xb mode=none|{}", names(&XMODES).replace(", ", "|")),
                            )
                        })?
                    }
                    None => XM_NONE,
                };
                let (pm, pr) = self.pred(st)?;
                let k = match st.kw("k") {
                    Some(t) => self.int(st, t)? as u32,
                    None => 0,
                };
                let sh = match st.kw("sh") {
                    Some(t) => self.int(st, t)? as u32,
                    None => 0,
                };
                let xb = [xm, k, sh, flags.contains("xc") as u32, pm, pr];
                state.xb = xb.map(Some);
                Ok((vec![self.word(st, xb_word(xb))?], state))
            }
            _ => {
                if pos.len() != 2 {
                    return Err(self.fail(st, "L1.IMG.029", format!("{} S0 S1", st.op)));
                }
                let walk = (self.int(st, pos[0])? as i64, self.int(st, pos[1])? as i64);
                let sel = if st.op == "ustr" { CFG_USTR } else { CFG_PSTR };
                if st.op == "ustr" {
                    state.ustr = Some(walk);
                } else {
                    state.pstr = Some(walk);
                }
                Ok((vec![self.word(st, stride_word(sel, walk))?], state))
            }
        }
    }

    // -------------------------------------------------------------- math
    pub(super) fn math(&mut self, st: &Stmt, state: State) -> Result<(Words, State)> {
        let op = st.op.as_str();
        let code = MathOp::parse(op)
            .ok_or_else(|| self.fail(st, "L1.IMG.034", format!("no V2 instruction '{op}'")))?;
        let pos = st.positional();
        let mut fields = vec!["a"];
        if ops::reads_b(op) {
            fields.push("b");
        }
        if ops::reads_c(op) {
            fields.push("c");
        }
        if pos.len() != 1 + fields.len() {
            return Err(self.fail(
                st,
                "L1.IMG.034",
                format!(
                    "{op} d, {}: {} operands",
                    fields.join(", "),
                    1 + fields.len()
                ),
            ));
        }
        let compare = ops::is_compare(op) && !on("isa.vector/compare");
        let (mut dreg, dnib) = self.dest(st, pos[0], compare)?;
        let mut srcs: Vec<(&str, Src)> = Vec::new();
        for (f, t) in fields.iter().zip(&pos[1..]) {
            srcs.push((f, self.src(st, t)?));
        }
        let mut reg: HashMap<&str, (u32, Sel)> =
            [("a", (0, Sel::V)), ("b", (0, Sel::V)), ("c", (0, Sel::V))].into();
        let mut nib: HashMap<&str, Option<Nib>> = [
            ("a", None),
            ("b", None),
            ("c", None),
            ("d", Some(dnib.unwrap_or(DEF_NIB))),
        ]
        .into();
        let mut xbar: Option<(u32, u32, u32)> = None;
        let mut xc = 0u32;
        let mut parameter_ids = [None, None];
        for (f, s) in &srcs {
            if let Some(x) = s.xbar {
                if *f == "a" {
                    return Err(self.fail(
                        st,
                        "L1.IMG.037",
                        "the crossbar is on b (and c with b's input)",
                    ));
                }
                if xbar.is_some()
                    && ((xbar, reg["b"]) != (s.xbar, (s.reg, s.sel))
                        || parameter_ids != s.parameter_ids)
                {
                    return Err(self.fail(
                        st,
                        "L1.IMG.037",
                        "c through the crossbar reads b's input: name it twice",
                    ));
                }
                xbar = Some(x);
                parameter_ids = s.parameter_ids.clone();
                if *f == "c" {
                    xc = 1;
                }
                reg.insert("b", (s.reg, s.sel));
                nib.insert("b", Some(s.nib.unwrap_or(DEF_NIB)));
                if *f == "c" {
                    reg.insert("c", (s.reg, s.sel));
                }
                continue;
            }
            reg.insert(f, (s.reg, s.sel));
            if s.sel == Sel::V {
                nib.insert(f, Some(s.nib.unwrap_or(DEF_NIB)));
            }
        }
        let has = |f: &str| srcs.iter().any(|(g, _)| *g == f);
        if xc == 1 && has("b") && srcs.iter().any(|(g, s)| *g == "b" && s.xbar.is_none()) {
            return Err(self.fail(
                st,
                "L1.IMG.037",
                "c through the crossbar while b is not: b is the crossbar's",
            ));
        }
        let (pm, mut pr) = self.pred(st)?;
        if compare {
            if pm != 0 {
                return Err(self.fail(
                    st,
                    "L1.IMG.039",
                    "a compare writes its predicate unpredicated",
                ));
            }
            pr = dreg;
            dreg = 0;
        }
        let selects = srcs.iter().any(|(_, s)| s.nib.is_some()) || dnib.is_some();
        let om = selects || xbar.is_some() || pm != 0 || compare;
        let vl = self.vl(st)?;
        let want = if om {
            let (xm, xk, sh) = match xbar {
                Some((m, k, s)) => (Some(m), Some(k), Some(s)),
                None => (Some(XM_NONE), None, None),
            };
            State {
                vl,
                chk: [nib["a"], nib["b"], nib["c"], nib["d"]],
                xb: [
                    xm,
                    xk,
                    if xm == Some(XM_BCAST) { sh } else { None },
                    if has("c") { Some(xc) } else { None },
                    Some(pm),
                    if pm != 0 || compare { Some(pr) } else { None },
                ],
                ustr: None,
                pstr: None,
            }
        } else {
            State { vl, ..UNKNOWN }
        };
        let parameter_xb = if on("l1.img/parameter-elision") {
            0
        } else {
            srcs.iter().fold(0, |bits, (_, s)| bits | s.parameter_xb)
        };
        let mut state = state;
        for (i, slot) in state.xb.iter_mut().enumerate() {
            if parameter_xb & (1 << i) != 0 {
                *slot = None;
            }
        }
        let (mut words, mut state) = self.setup(st, state, want)?;
        for (i, slot) in state.xb.iter_mut().enumerate() {
            if parameter_xb & (1 << i) != 0 {
                *slot = None;
            }
        }
        let (va, sa) = reg["a"];
        let (vb, sb) = reg["b"];
        let (vc, sc) = reg["c"];
        words.push(self.word(
            st,
            VWord::Math {
                op: code,
                vd: dreg,
                va,
                vb,
                vc,
                sa,
                sb,
                sc,
                om,
            },
        )?);
        Ok((words, state))
    }

    pub(super) fn vl(&self, st: &Stmt) -> Result<Option<u32>> {
        let Some(v) = st.kw("vl") else {
            if on("l1.img/vl-default") {
                return Ok(Some(64));
            }
            return Ok(Some(self.facts.vlmax));
        };
        if v.as_name() == Some("keep") {
            return Ok(None);
        }
        let n = self.int(st, v)?;
        if !(1..=self.facts.vlmax as i128).contains(&n) {
            return Err(self.fail(
                st,
                "L1.IMG.038",
                format!("vl={n} is not 1..{}", self.facts.vlmax),
            ));
        }
        Ok(Some(n as u32))
    }

    pub(super) fn pred(&self, st: &Stmt) -> Result<(u32, u32)> {
        let (pm, pr) = self.pred_of(st)?;
        Ok((
            if pm == 1 && on("isa.vector/pred") {
                2
            } else {
                pm
            },
            pr,
        ))
    }

    pub(super) fn pred_of(&self, st: &Stmt) -> Result<(u32, u32)> {
        let got: Vec<(&str, &Term)> = ["if", "unless"]
            .iter()
            .filter_map(|k| st.kw(k).map(|t| (*k, t)))
            .collect();
        match got.len() {
            0 => Ok((0, 0)),
            1 => {
                let (k, t) = got[0];
                let p = self.numbered(st, t, "p", self.facts.preds)?;
                if on("l1.img/pred-unless") {
                    return Ok((2, p));
                }
                Ok((if k == "if" { 1 } else { 2 }, p))
            }
            _ => Err(self.fail(st, "L1.IMG.039", "one of if= / unless=")),
        }
    }

    pub(super) fn dest(&self, st: &Stmt, t: &Term, compare: bool) -> Result<(u32, Option<Nib>)> {
        let (prefix, limit) = if compare {
            ("p", self.facts.preds)
        } else {
            ("v", self.facts.regs)
        };
        if let Term::View(n, _) = t {
            let r = self.numbered(st, &Term::name(n.clone()), prefix, limit)?;
            return Ok((r, Some(self.nib(st, t)?)));
        }
        Ok((self.numbered(st, t, prefix, limit)?, None))
    }

    pub(super) fn src(&self, st: &Stmt, t: &Term) -> Result<Src> {
        if let Term::Call(name, args) = t {
            if let Some(xm) = lookup(&xmodes(), name) {
                let Some(first) = args.first() else {
                    return Err(self.fail(st, "L1.IMG.009", format!("{name}(REGISTER, ...)")));
                };
                let inner = self.src(st, first)?;
                if inner.sel != Sel::V || inner.xbar.is_some() {
                    return Err(self.fail(
                        st,
                        "L1.IMG.009",
                        format!("{name} takes a vector register"),
                    ));
                }
                let pos: Vec<&Term> = args[1..]
                    .iter()
                    .filter(|a| !matches!(a, Term::Kw(..)))
                    .collect();
                let kw: Vec<(&str, &Term)> = args[1..]
                    .iter()
                    .filter_map(|a| match a {
                        Term::Kw(n, v) => Some((n.as_str(), v.as_ref())),
                        _ => None,
                    })
                    .collect();
                let get = |k: &str| kw.iter().rev().find(|(n, _)| *n == k).map(|(_, v)| *v);
                let (xk, sh) = if xm == XM_BCAST {
                    if !pos.is_empty() {
                        return Err(self.fail(st, "L1.IMG.009", "bcast(REG, lane=L, sh=S)"));
                    }
                    let xk = match get("lane") {
                        Some(v) => self.configuration_int(st, v)?,
                        None => 0,
                    };
                    let sh = match get("sh") {
                        Some(v) => self.configuration_int(st, v)?,
                        None => 0,
                    };
                    (xk, sh)
                } else {
                    if pos.len() != 1 || !kw.is_empty() {
                        return Err(self.fail(st, "L1.IMG.009", format!("{name}(REG, K)")));
                    }
                    (self.configuration_int(st, pos[0])?, 0)
                };
                return Ok(Src {
                    xbar: Some((xm, xk as u32, sh as u32)),
                    parameter_ids: if xm == XM_BCAST {
                        [
                            get("lane").and_then(parameter_id),
                            get("sh").and_then(parameter_id),
                        ]
                    } else {
                        [parameter_id(pos[0]), None]
                    },
                    parameter_xb: if xm == XM_BCAST {
                        u8::from(get("lane").is_some_and(is_parameter)) << 1
                            | u8::from(get("sh").is_some_and(is_parameter)) << 2
                    } else {
                        u8::from(is_parameter(pos[0])) << 1
                    },
                    ..inner
                });
            }
        }
        if let Term::View(n, _) = t {
            let src = self.src(st, &Term::name(n.clone()))?;
            if src.sel != Sel::V {
                return Err(self.fail(st, "L1.IMG.035", "a chunk select is on a vector register"));
            }
            return Ok(Src {
                nib: Some(self.nib(st, t)?),
                ..src
            });
        }
        if let Term::Name(text) = t {
            if text.len() > 1 && text[1..].chars().all(|c| c.is_ascii_digit()) {
                let kind = &text[..1];
                let limit = match kind {
                    "v" => Some((self.facts.regs, Sel::V)),
                    "s" => Some((self.facts.sregs, Sel::S)),
                    "k" => Some((self.facts.kregs, Sel::K)),
                    _ => None,
                };
                if let Some((limit, sel)) = limit {
                    let n: u64 = text[1..].parse().unwrap_or(u64::MAX);
                    // One past a scalar file reads as its register 0.
                    let past = n == limit as u64 && sel != Sel::V;
                    if past && on("isa.vector/limits") {
                        return Ok(Src {
                            reg: 0,
                            sel,
                            nib: None,
                            xbar: None,
                            parameter_xb: 0,
                            parameter_ids: [None, None],
                        });
                    }
                    if n >= limit as u64 {
                        return Err(self.fail(
                            st,
                            "L1.IMG.008",
                            format!("{text}: {kind}0..{kind}{}", limit - 1),
                        ));
                    }
                    return Ok(Src {
                        reg: n as u32,
                        sel,
                        nib: None,
                        xbar: None,
                        parameter_xb: 0,
                        parameter_ids: [None, None],
                    });
                }
            }
        }
        Err(self.fail(
            st,
            "L1.IMG.008",
            format!(
                "wanted vN, sN, kN, a chunk select or a crossbar, got {}",
                fmt(t)
            ),
        ))
    }

    pub(super) fn nib(&self, st: &Stmt, t: &Term) -> Result<Nib> {
        let Term::View(n, slices) = t else {
            unreachable!("nib of a view")
        };
        if slices.len() != 1 {
            return Err(self.fail(
                st,
                "L1.IMG.008",
                format!("{n}[c:] (from chunk c) or {n}[c] (chunk c every beat)"),
            ));
        }
        let (base, stride) = match &slices[0] {
            Term::Slice(Some(lo), None) => (self.int(st, lo)?, 1),
            Term::Int(v, _) => (*v, 0),
            s => {
                return Err(self.fail(
                    st,
                    "L1.IMG.008",
                    format!("{n}[c:] or {n}[c], got {}", fmt(s)),
                ))
            }
        };
        if !(0..self.facts.chunks as i128).contains(&base) {
            return Err(self.fail(
                st,
                "L1.IMG.008",
                format!("chunk {base}: 0..{}", self.facts.chunks - 1),
            ));
        }
        if on("l1.img/chunk-stride") {
            return Ok((base as u32, 1));
        }
        Ok((base as u32, stride))
    }

    // ----------------------------------------------------- unpack / pack
}
