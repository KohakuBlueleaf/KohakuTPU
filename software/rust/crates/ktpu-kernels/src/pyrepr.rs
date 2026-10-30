//! Kernel names: `PREFIX_` and the first 10 hex digits of sha1 over the
//! Python `repr` of the generator's key, as the Python generators name
//! their kernels (`kernels/vector.py` `name`).

/// A value as Python would `repr` it.
#[derive(Clone, Debug)]
pub enum Py {
    Int(i64),
    Float(f64),
    Str(String),
    Bool(bool),
    None,
    Tuple(Vec<Py>),
    /// A dataclass: `Name(field=value, ..)`.
    Obj(&'static str, Vec<(&'static str, Py)>),
}

impl Py {
    pub fn str(s: &str) -> Py {
        Py::Str(s.to_string())
    }

    pub fn ints(xs: &[i64]) -> Py {
        Py::Tuple(xs.iter().map(|x| Py::Int(*x)).collect())
    }

    pub fn walk(levels: &[(i64, i64)]) -> Py {
        Py::Tuple(levels.iter().map(|(n, s)| Py::ints(&[*n, *s])).collect())
    }

    pub fn repr(&self) -> String {
        match self {
            Py::Int(v) => v.to_string(),
            Py::Float(x) => float_repr(*x),
            Py::Str(s) => str_repr(s),
            Py::Bool(b) => if *b { "True" } else { "False" }.to_string(),
            Py::None => "None".to_string(),
            Py::Tuple(xs) => {
                let inner: Vec<String> = xs.iter().map(Py::repr).collect();
                if xs.len() == 1 {
                    format!("({},)", inner[0])
                } else {
                    format!("({})", inner.join(", "))
                }
            }
            Py::Obj(name, fields) => {
                let inner: Vec<String> = fields
                    .iter()
                    .map(|(k, v)| format!("{k}={}", v.repr()))
                    .collect();
                format!("{name}({})", inner.join(", "))
            }
        }
    }
}

/// Python's `repr(float)`: the shortest round-trip digits, positional
/// for decimal exponents -4..16, else `d.ddde+XX`.
pub fn float_repr(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.into();
    }
    let sign = if x.is_sign_negative() { "-" } else { "" };
    let sci = format!("{:e}", x.abs());
    let (mant, exp) = sci.split_once('e').expect("an exponent");
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let exp: i64 = exp.parse().expect("an int exponent");
    let decpt = exp + 1;
    let n = digits.len() as i64;
    let body = if -4 < decpt && decpt <= 16 {
        if decpt <= 0 {
            format!("0.{}{digits}", "0".repeat((-decpt) as usize))
        } else if decpt < n {
            format!(
                "{}.{}",
                &digits[..decpt as usize],
                &digits[decpt as usize..]
            )
        } else {
            format!("{digits}{}.0", "0".repeat((decpt - n) as usize))
        }
    } else {
        let m = if n == 1 {
            digits.clone()
        } else {
            format!("{}.{}", &digits[..1], &digits[1..])
        };
        let e = decpt - 1;
        format!("{m}e{}{:02}", if e < 0 { '-' } else { '+' }, e.abs())
    };
    format!("{sign}{body}")
}

/// Python's `repr(str)`.
pub fn str_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `PREFIX_` + sha1(repr(parts))[:10].
pub fn name(prefix: &str, parts: Vec<Py>) -> String {
    let text = Py::Tuple(parts).repr();
    let hex = sha1_smol::Sha1::from(text.as_bytes()).digest().to_string();
    let k = if crate::mutants::on("dsl.kernel/name") {
        "_k"
    } else {
        ""
    };
    format!("{prefix}_{}{k}", &hex[..10])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floats_print_as_python() {
        let cases = [
            (1.0, "1.0"),
            (2.0, "2.0"),
            (0.5, "0.5"),
            (-0.0, "-0.0"),
            (1e16, "1e+16"),
            (1e15, "1000000000000000.0"),
            (1e-5, "1e-05"),
            (0.0001, "0.0001"),
            (1e-30, "1e-30"),
            (1.5e-7, "1.5e-07"),
            (49152.0, "49152.0"),
            (0.15915494309189535, "0.15915494309189535"),
            (-65504.0, "-65504.0"),
            (std::f64::consts::LOG2_E, "1.4426950408889634"),
            (f64::INFINITY, "inf"),
            (123456789012345680.0, "1.2345678901234568e+17"),
        ];
        for (x, want) in cases {
            assert_eq!(float_repr(x), want, "{x:e}");
        }
    }

    #[test]
    fn strings_and_tuples_print_as_python() {
        assert_eq!(str_repr("f16"), "'f16'");
        assert_eq!(str_repr("Op(name='add')"), "\"Op(name='add')\"");
        assert_eq!(Py::Tuple(vec![Py::Int(3)]).repr(), "(3,)");
        assert_eq!(Py::Tuple(vec![]).repr(), "()");
        assert_eq!(name("copy", vec![Py::Int(4)]).len(), "copy_".len() + 10);
    }
}
