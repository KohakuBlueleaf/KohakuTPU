//! Typed instruction constructors and compatibility printing.
//! The template generator is the feature-only witness reference.

pub mod predicate;
pub mod print;
pub mod typed;

#[cfg(test)]
mod tests;

#[cfg(feature = "witness")]
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Code {
    Raw(String),
    Op(String, Vec<String>),
}

#[cfg(feature = "witness")]
impl Code {
    pub fn is_open(&self) -> bool {
        matches!(self, Code::Raw(s) if s == "{")
    }

    pub fn is_block_edge(&self) -> bool {
        matches!(self, Code::Raw(s) if s == "{" || s == "}")
    }
}

#[cfg(feature = "witness")]
#[derive(Default, Debug)]
pub struct Gen {
    pub n: usize,
    pub code: Vec<Code>,
}

#[cfg(feature = "witness")]
impl Gen {
    /// A fresh symbol, `%N`.
    pub fn sym(&mut self) -> String {
        self.n += 1;
        format!("%{}", self.n)
    }

    pub fn emit(&mut self, tmpl: impl Into<String>, syms: &[&str]) {
        self.emit_owned(tmpl, syms.iter().map(|s| s.to_string()).collect());
    }

    pub fn emit_owned(&mut self, tmpl: impl Into<String>, syms: Vec<String>) {
        let tmpl = tmpl.into();
        if crate::mutant::on("isa.vector/lower") && tmpl.contains("if=p1") {
            return;
        }
        self.code.push(Code::Op(tmpl, syms));
    }

    pub fn raw(&mut self, text: impl Into<String>) {
        let text = text.into();
        if crate::mutant::on("isa.vector/lower") && text.starts_with("getstk") {
            return;
        }
        self.code.push(Code::Raw(text));
    }
}

/// The `%N` symbols a line names.
#[cfg(feature = "witness")]
fn symbols(line: &str) -> std::collections::BTreeSet<String> {
    let b = line.as_bytes();
    let mut out = std::collections::BTreeSet::new();
    let mut i = 0;
    while i < b.len() {
        let j = (i + 1..=b.len())
            .find(|&j| j == b.len() || !b[j].is_ascii_digit())
            .unwrap();
        if b[i] == b'%' && j > i + 1 {
            out.insert(line[i..j].to_string());
            i = j;
        } else {
            i += 1;
        }
    }
    out
}

/// `op A, B, ... opts`: the operands split at top-level commas, and the
/// options after the last operand.
#[cfg(feature = "witness")]
pub(crate) fn operands<'a>(line: &'a str, op: &str) -> Option<(Vec<&'a str>, &'a str)> {
    let rest = line.strip_prefix(op)?.strip_prefix(' ')?;
    let mut args = Vec::new();
    let (mut depth, mut start) = (0, 0);
    for (i, ch) in rest.char_indices() {
        match ch {
            '(' | '[' => depth += 1,
            ')' | ']' => depth -= 1,
            ',' if depth == 0 => {
                args.push(rest[start..i].trim());
                start = i + 1;
            }
            _ => {}
        }
    }
    let last = rest[start..].trim();
    let mut depth = 0;
    let cut = last.char_indices().find(|&(_, ch)| {
        match ch {
            '(' | '[' => depth += 1,
            ')' | ']' => depth -= 1,
            _ => {}
        }
        ch == ' ' && depth == 0
    });
    let (arg, options) = match cut {
        Some((at, _)) => (&last[..at], last[at..].trim()),
        None => (last, ""),
    };
    args.push(arg);
    Some((args, options))
}

/// A line as a template over its destination, then its other symbols in
/// name order.
#[cfg(feature = "witness")]
fn templated(op: &str, dst: &str, rest: &str) -> Code {
    let mut syms = vec![dst.to_string()];
    syms.extend(symbols(rest));
    let mut tmpl = format!("{op} {{0}}, ");
    let b = rest.as_bytes();
    let mut i = 0;
    while i < b.len() {
        let j = (i + 1..=b.len())
            .find(|&j| j == b.len() || !b[j].is_ascii_digit())
            .unwrap();
        if b[i] == b'%' && j > i + 1 {
            let at = syms.iter().position(|s| s == &rest[i..j]).unwrap();
            tmpl.push_str(&format!("{{{at}}}"));
            i = j;
        } else {
            tmpl.push_str(match b[i] {
                b'{' => "{{",
                b'}' => "}}",
                _ => &rest[i..i + 1],
            });
            i += 1;
        }
    }
    Code::Op(tmpl, syms)
}

/// `vcmp p1; vmov M, k0; vmov M, k1 if=p1; vsel D, A, B, M` with M used
/// nowhere else, as `vmov D, B; vcmp p1; vmov D, A if=p1`.
#[cfg(feature = "witness")]
pub fn fuse(code: Vec<Code>) -> Vec<Code> {
    let line = |c: &Code| match c {
        Code::Op(t, s) => Some(super::pat::format(t, s)),
        Code::Raw(_) => None,
    };
    let lines: Vec<Option<String>> = code.iter().map(line).collect();
    let mut uses = std::collections::HashMap::<String, usize>::new();
    for l in lines.iter().flatten() {
        for s in symbols(l) {
            *uses.entry(s).or_default() += 1;
        }
    }
    let is_sym =
        |t: &str| t.len() > 1 && t.starts_with('%') && t[1..].bytes().all(|b| b.is_ascii_digit());
    let mut out = Vec::new();
    let mut i = 0;
    while i < code.len() {
        let found = (|| {
            let cmp = lines.get(i)?.as_deref()?;
            let zero = lines.get(i + 1)?.as_deref()?;
            let one = lines.get(i + 2)?.as_deref()?;
            let sel = lines.get(i + 3)?.as_deref()?;
            if !["vcmpeq ", "vcmplt ", "vcmpgt "]
                .iter()
                .any(|p| cmp.starts_with(p))
            {
                return None;
            }
            let (z, zo) = operands(zero, "vmov")?;
            let (o, oo) = operands(one, "vmov")?;
            let (s, _) = operands(sel, "vsel")?;
            if z.len() != 2 || o.len() != 2 || s.len() != 4 {
                return None;
            }
            let (mask, dst) = (z[0], s[0]);
            if !is_sym(mask)
                || !is_sym(dst)
                || uses[mask] != 3
                || o[0] != mask
                || s[3] != mask
                || z[1] != "k0"
                || o[1] != "k1"
                || zo.split(' ').any(|w| w.starts_with("if="))
                || !oo.split(' ').any(|w| w == "if=p1")
                || symbols(s[1]).contains(mask)
                || symbols(s[2]).contains(mask)
                || symbols(cmp).contains(dst)
                || symbols(s[1]).contains(dst)
            {
                return None;
            }
            Some([
                templated("vmov", dst, s[2]),
                code[i].clone(),
                templated("vmov", dst, &format!("{} if=p1", s[1])),
            ])
        })();
        if let Some(seq) = found {
            out.extend(seq);
            i += 4;
        } else {
            out.push(code[i].clone());
            i += 1;
        }
    }
    out
}
