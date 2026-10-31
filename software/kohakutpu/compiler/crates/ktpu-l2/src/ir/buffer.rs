//! A memory buffer's type and layout (L2.SPACE.001-.002, .009-.011):
//! `DTYPE[dims] @SPACE(KEY=VALUE, ...)`.

use indexmap::IndexMap;
use ktpu_l1::text::Term;

use super::repr::{py_str, repr};
use crate::target::Target;

/// Element types a buffer may hold, and their bytes (MXFP7 aside).
pub const DTYPES: [(&str, i64); 6] = [
    ("f16", 2),
    ("f32", 4),
    ("u8", 1),
    ("u32", 4),
    ("i32", 4),
    ("u64", 8),
];

/// A layout value: a number or a `GMxGN` pair.
#[derive(Clone, Debug, PartialEq)]
pub enum LayoutVal {
    Int(i64),
    Float(f64),
    Dims(Vec<i64>),
}

impl LayoutVal {
    pub fn int(&self) -> i64 {
        match self {
            LayoutVal::Int(v) => *v,
            LayoutVal::Float(f) => *f as i64,
            LayoutVal::Dims(d) => d.first().copied().unwrap_or(0),
        }
    }

    pub fn truthy(&self) -> bool {
        match self {
            LayoutVal::Int(v) => *v != 0,
            LayoutVal::Float(f) => *f != 0.0,
            LayoutVal::Dims(d) => !d.is_empty(),
        }
    }

    /// Python's `str` of the value (a tuple for dims).
    pub fn py(&self) -> String {
        match self {
            LayoutVal::Int(v) => v.to_string(),
            LayoutVal::Float(f) => ktpu_l1::text::py_float(*f),
            LayoutVal::Dims(d) if d.len() == 1 => format!("({},)", d[0]),
            LayoutVal::Dims(d) => format!(
                "({})",
                d.iter()
                    .map(|x| x.to_string())
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Buffer {
    pub dtype: String,
    pub shape: Vec<i64>,
    pub space: String,
    pub layout: IndexMap<String, LayoutVal>,
}

fn sorted_keys(layout: &IndexMap<String, LayoutVal>) -> String {
    let mut keys: Vec<&String> = layout.keys().collect();
    keys.sort();
    format!(
        "[{}]",
        keys.iter()
            .map(|k| py_str(k))
            .collect::<Vec<_>>()
            .join(", ")
    )
}

impl Buffer {
    /// The buffer a type term names; the error is Python's message.
    pub fn of(t: &Term) -> Result<Buffer, String> {
        let mut space = "dram".to_string();
        let mut layout = IndexMap::new();
        let mut t = t;
        if let Term::Placed(x, at) = t {
            t = x;
            let at = match at.as_ref() {
                Term::Call(name, args) => {
                    for a in args {
                        let Term::Kw(k, v) = a else {
                            return Err(format!("a layout is NAME=VALUE, got {}", repr(a)));
                        };
                        let v = match v.as_ref() {
                            Term::Dims(d) => LayoutVal::Dims(d.iter().map(|x| *x as i64).collect()),
                            Term::Int(i, _) => LayoutVal::Int(*i as i64),
                            Term::Float(f) => LayoutVal::Float(*f),
                            other => {
                                return Err(format!("a layout is NAME=VALUE, got {}", repr(other)))
                            }
                        };
                        layout.insert(k.clone(), v);
                    }
                    Term::Name(name.clone())
                }
                other => other.clone(),
            };
            match at {
                Term::Name(s) => space = s,
                other => {
                    return Err(format!(
                        "an L1 buffer is DTYPE[constant dims], got {}",
                        repr(&other)
                    ))
                }
            }
        }
        let Term::View(dtype, dims) = t else {
            return Err(format!(
                "an L1 buffer is DTYPE[constant dims], got {}",
                repr(t)
            ));
        };
        let mut shape = Vec::new();
        for d in dims {
            match d {
                Term::Int(v, _) => shape.push(*v as i64),
                _ => {
                    return Err(format!(
                        "an L1 buffer is DTYPE[constant dims], got {}",
                        repr(t)
                    ))
                }
            }
        }
        let b = Buffer {
            dtype: dtype.clone(),
            shape,
            space,
            layout,
        };
        b.check()?;
        let mut b = b;
        if b.is_mx7() && crate::mutant::on("l2.buffer/mx7-layout-unread") {
            b.layout = IndexMap::new();
        }
        Ok(b)
    }

    fn check(&self) -> Result<(), String> {
        let want: Option<&[&str]> = match self.dtype.as_str() {
            "mx7a" => Some(&["gm", "nk"]),
            "mx7b" => Some(&["gn", "nk"]),
            _ => None,
        };
        if let Some(want) = want {
            let mut have: Vec<&str> = self
                .layout
                .keys()
                .map(|k| k.as_str())
                .filter(|k| *k != "t" && *k != "node")
                .collect();
            have.sort();
            have.dedup();
            if have != want {
                return Err(format!(
                    "{} wants @dram({}=...)",
                    self.dtype,
                    want.join(", ")
                ));
            }
        }
        if !matches!(self.dtype.as_str(), "mx7a" | "mx7b")
            && !DTYPES.iter().any(|(d, _)| {
                *d == self.dtype
                    && !(*d == "f16" && crate::mutant::on("l2.buffer/no-f16"))
                    && !(*d == "f32" && crate::mutant::on("l2.buffer/no-f32"))
            })
        {
            return Err(format!("no buffer dtype {}", self.dtype));
        }
        const KEYS: [&str; 7] = ["gm", "gn", "nk", "tile", "ieee", "t", "node"];
        if self.layout.keys().any(|k| !KEYS.contains(&k.as_str())) {
            return Err(format!(
                "layout keys: gm gn nk tile ieee t node; got {}",
                sorted_keys(&self.layout)
            ));
        }
        if self.layout.contains_key("ieee") && !self.layout.contains_key("tile") {
            return Err("ieee= reads a tile= layout's values as IEEE fp16".into());
        }
        if self.layout.contains_key("node") && self.space != "stage" {
            return Err("node= places a buffer in that node's staging: @stage".into());
        }
        if self.layout.keys().any(|k| k != "node") && self.shape.len() < 2 {
            return Err(format!(
                "a {} layout is of [.., M, N] slabs",
                sorted_keys(&self.layout)
            ));
        }
        Ok(())
    }

    /// The integer layout value `key` (0 when absent).
    pub fn get(&self, key: &str) -> i64 {
        self.layout.get(key).map(LayoutVal::int).unwrap_or(0)
    }

    pub fn has(&self, key: &str) -> bool {
        self.layout.contains_key(key)
    }

    /// Whether `key` is present and true (`layout.get(key)`).
    pub fn flag(&self, key: &str) -> bool {
        self.layout.get(key).map(LayoutVal::truthy).unwrap_or(false)
    }

    /// `tile=GMxGN`.
    pub fn tile(&self) -> Option<(i64, i64)> {
        match self.layout.get("tile") {
            Some(LayoutVal::Dims(d)) if d.len() == 2 => Some((d[0], d[1])),
            _ => None,
        }
    }

    pub fn is_mx7(&self) -> bool {
        self.dtype == "mx7a" || self.dtype == "mx7b"
    }

    /// Bytes an element takes (MXFP7 aside: see [`Buffer::nbytes`]).
    pub fn item(&self) -> i64 {
        DTYPES
            .iter()
            .find(|(d, _)| *d == self.dtype)
            .map(|(_, n)| *n)
            .unwrap_or(1)
    }

    pub fn count(&self) -> i64 {
        self.shape.iter().product()
    }

    /// Bytes the buffer takes on `t`: an MXFP7 buffer is whole entries of
    /// `mg.lanes x mg.kblock` values.
    pub fn nbytes(&self, t: &Target) -> i64 {
        if self.is_mx7() {
            return t.machine.mx7_bytes(self.count());
        }
        self.count() * self.item()
    }
}
