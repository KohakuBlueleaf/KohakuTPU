//! Text patterns of instruction templates: a template is an image line with
//! `{N}` where symbol N's register goes (`vmul {0}, {1}[4:], k1 vl=64`).

fn is_word(c: u8) -> bool {
    c.is_ascii_alphanumeric() || c == b'_'
}

fn digits(b: &[u8], mut i: usize) -> (usize, Option<u64>) {
    let start = i;
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
    }
    if i == start {
        return (start, None);
    }
    let v = std::str::from_utf8(&b[start..i]).unwrap().parse().ok();
    (i, v)
}

fn skip_ws(b: &[u8], mut i: usize) -> usize {
    while i < b.len() && (b[i] as char).is_whitespace() {
        i += 1;
    }
    i
}

/// One `{N}` reference and the chunk select after it, `[B:]` or `[B]`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Ref {
    pub start: usize,
    pub end: usize,
    pub sym: usize,
    /// (base chunk, whether it strides: `[B:]`)
    pub sel: Option<(u64, bool)>,
}

/// Every `\{(\d+)\}(?:\[(\d+)\s*(:?)\s*\])?` match, left to right.
pub fn refs(t: &str) -> Vec<Ref> {
    let b = t.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if b[i] != b'{' {
            i += 1;
            continue;
        }
        let (j, n) = digits(b, i + 1);
        let n = match n {
            Some(n) if j < b.len() && b[j] == b'}' => n,
            _ => {
                i += 1;
                continue;
            }
        };
        let mut end = j + 1;
        let mut sel = None;
        if end < b.len() && b[end] == b'[' {
            let (k, base) = digits(b, end + 1);
            if let Some(base) = base {
                let k = skip_ws(b, k);
                let (k, colon) = if k < b.len() && b[k] == b':' {
                    (k + 1, true)
                } else {
                    (k, false)
                };
                let k = skip_ws(b, k);
                if k < b.len() && b[k] == b']' {
                    sel = Some((base, colon));
                    end = k + 1;
                }
            }
        }
        out.push(Ref {
            start: i,
            end,
            sym: n as usize,
            sel,
        });
        i = end;
    }
    out
}

/// The first `{N}` of `t`.
pub fn first_sym(t: &str) -> Option<usize> {
    refs(t).first().map(|r| r.sym)
}

/// Every `{N}` of `t`, in order.
pub fn syms(t: &str) -> Vec<usize> {
    refs(t).into_iter().map(|r| r.sym).collect()
}

/// The first `}[B :]` / `}[B]` of `t`: (B, whether it strides).
pub fn first_select(t: &str) -> Option<(u64, bool)> {
    let b = t.as_bytes();
    for i in 0..b.len() {
        if b[i] == b'}' && i + 1 < b.len() && b[i + 1] == b'[' {
            let (k, base) = digits(b, i + 2);
            if let Some(base) = base {
                let k = skip_ws(b, k);
                let (k, colon) = if k < b.len() && b[k] == b':' {
                    (k + 1, true)
                } else {
                    (k, false)
                };
                let k = skip_ws(b, k);
                if k < b.len() && b[k] == b']' {
                    return Some((base, colon));
                }
            }
        }
    }
    None
}

/// `\bKEY=(\d+)`: the first number after `KEY=` at a word boundary.
pub fn kw_num(t: &str, key: &str) -> Option<u64> {
    let b = t.as_bytes();
    let pat = format!("{key}=");
    let mut from = 0;
    while let Some(off) = t[from..].find(&pat) {
        let i = from + off;
        if i == 0 || !is_word(b[i - 1]) {
            let (_, n) = digits(b, i + pat.len());
            if n.is_some() {
                return n;
            }
        }
        from = i + 1;
    }
    None
}

/// Whether `\b(if|unless)=p` occurs.
pub fn predicated(t: &str) -> bool {
    let b = t.as_bytes();
    for key in ["if=p", "unless=p"] {
        let mut from = 0;
        while let Some(off) = t[from..].find(key) {
            let i = from + off;
            if i == 0 || !is_word(b[i - 1]) {
                return true;
            }
            from = i + 1;
        }
    }
    false
}

/// `t` without its ` vl=X`, ` if=X`, ` unless=X` options
/// (`re.sub(r"\s+(vl|if|unless)=\S+", "", t)`).
pub fn strip_options(t: &str) -> String {
    let b = t.as_bytes();
    let mut out = String::with_capacity(t.len());
    let mut i = 0;
    while i < b.len() {
        if (b[i] as char).is_whitespace() {
            let j = skip_ws(b, i);
            let rest = &t[j..];
            let key = ["vl=", "if=", "unless="]
                .iter()
                .find(|k| rest.starts_with(**k));
            if let Some(k) = key {
                let mut e = j + k.len();
                let s = e;
                while e < b.len() && !(b[e] as char).is_whitespace() {
                    e += 1;
                }
                if e > s {
                    i = e;
                    continue;
                }
            }
            out.push_str(&t[i..j]);
            i = j;
            continue;
        }
        out.push(b[i] as char);
        i += 1;
    }
    out
}

/// Top-level comma-separated operands (brackets nest).
pub fn operands(rest: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut depth = 0i32;
    let mut cur = String::new();
    for ch in rest.chars() {
        if ch == '(' || ch == '[' {
            depth += 1;
        } else if ch == ')' || ch == ']' {
            depth -= 1;
        }
        if ch == ',' && depth == 0 {
            out.push(cur.trim().to_string());
            cur.clear();
        } else {
            cur.push(ch);
        }
    }
    if !cur.trim().is_empty() {
        out.push(cur.trim().to_string());
    }
    out
}

/// The word after `l1[`: `l1\[(\d+)\]`, and where it sits.
pub fn l1_word(t: &str) -> Option<(usize, usize, u64)> {
    let b = t.as_bytes();
    let mut from = 0;
    while let Some(off) = t[from..].find("l1[") {
        let i = from + off;
        let (j, n) = digits(b, i + 3);
        if let Some(n) = n {
            if j < b.len() && b[j] == b']' {
                return Some((i, j + 1, n));
            }
        }
        from = i + 1;
    }
    None
}

/// `tmpl.format(*names)`: `{N}` takes `names[N]`, `{{` / `}}` are braces.
pub fn format(tmpl: &str, names: &[String]) -> String {
    let b = tmpl.as_bytes();
    let mut out = String::with_capacity(tmpl.len() + 8);
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'{' {
            if i + 1 < b.len() && b[i + 1] == b'{' {
                out.push('{');
                i += 2;
                continue;
            }
            let (j, n) = digits(b, i + 1);
            if let Some(n) = n {
                if j < b.len() && b[j] == b'}' {
                    out.push_str(&names[n as usize]);
                    i = j + 1;
                    continue;
                }
            }
        } else if b[i] == b'}' && i + 1 < b.len() && b[i + 1] == b'}' {
            out.push('}');
            i += 2;
            continue;
        }
        let ch = tmpl[i..].chars().next().unwrap();
        out.push(ch);
        i += ch.len_utf8();
    }
    out
}

/// An image line as `(template, registers)`: each `vN` (whole word) becomes
/// `{k}`, k its first-appearance index; braces escaped.
pub fn symbolic(text: &str) -> (String, Vec<String>) {
    let b = text.as_bytes();
    let mut regs: Vec<String> = Vec::new();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < b.len() {
        let c = b[i];
        if c == b'v' && (i == 0 || !is_word(b[i - 1])) {
            let (j, n) = digits(b, i + 1);
            if n.is_some() && (j == b.len() || !is_word(b[j])) {
                let name = &text[i..j];
                let k = match regs.iter().position(|r| r == name) {
                    Some(k) => k,
                    None => {
                        regs.push(name.to_string());
                        regs.len() - 1
                    }
                };
                out.push_str(&format!("{{{k}}}"));
                i = j;
                continue;
            }
        }
        match c {
            b'{' => out.push_str("{{"),
            b'}' => out.push_str("}}"),
            _ => {
                let ch = text[i..].chars().next().unwrap();
                out.push(ch);
                i += ch.len_utf8();
                continue;
            }
        }
        i += 1;
    }
    (out, regs)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refs_and_selects() {
        let r = refs("vmax {0}[4:], {1}[0 :], merge({2}[1:], 8) vl=16");
        assert_eq!(r.len(), 3);
        assert_eq!(r[0].sel, Some((4, true)));
        assert_eq!(r[1].sel, Some((0, true)));
        assert_eq!(r[2].sym, 2);
        assert_eq!(refs("vsel {0}, {1}, {2}, {3}")[3].sel, None);
        assert_eq!(kw_num("vadd {0}, {1}, {2} vl=64", "vl"), Some(64));
        assert_eq!(kw_num("vunpk {0} <- l1[8] f16 n=8", "n"), Some(8));
        assert_eq!(strip_options("{0}, k1, {1} vl=16 if=p1"), "{0}, k1, {1}");
        assert!(predicated("vmov {0}, k1 if=p1"));
        assert_eq!(
            symbolic("vmul v10, k1, xor(v2, 1)"),
            (
                "vmul {0}, k1, xor({1}, 1)".to_string(),
                vec!["v10".into(), "v2".into()]
            )
        );
        assert_eq!(
            format("vmov {0}[4:], {1}", &["v1".into(), "v2".into()]),
            "vmov v1[4:], v2"
        );
    }
}
