//! An instruction layout declared once: encode, decode, match, span.
//!
//! Fields are laid MSB first from the top of the container, as the RTL reads
//! a flit (`payload[255 -: 32]`); bits below the last field are padding. A
//! value outside its field is refused with the field's clause id.

use super::bits::{mask, W256};
use ktpu_machine::{refuse, Refusal};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Field {
    pub name: &'static str,
    pub width: u32,
    /// Fixed value: not an encode argument, checked on decode.
    pub konst: Option<u64>,
    /// Two's complement.
    pub signed: bool,
    /// The value when the caller gives none.
    pub default: Option<i64>,
    /// The clause a value outside the field breaks.
    pub clause: &'static str,
}

impl Field {
    pub fn new(name: &'static str, width: u32, clause: &'static str) -> Field {
        Field {
            name,
            width,
            konst: None,
            signed: false,
            default: None,
            clause,
        }
    }

    pub fn konst(mut self, value: u64) -> Field {
        self.konst = Some(value);
        self
    }

    pub fn default(mut self, value: i64) -> Field {
        self.default = Some(value);
        self
    }

    pub fn signed(mut self) -> Field {
        self.signed = true;
        self
    }

    /// The inclusive range the field holds (a field wider than 64 bits
    /// takes 64 bits of value).
    pub fn limits(&self) -> (i128, i128) {
        let width = self.width.min(64);
        if self.signed {
            let half = 1i128 << (width - 1);
            (-half, half - 1)
        } else {
            (0, (1i128 << width) - 1)
        }
    }

    /// `value` as raw bits (at most 64 significant), or the refusal.
    pub fn fit(&self, format: &str, value: i64) -> Result<u64, Refusal> {
        let (lo, hi) = self.limits();
        if !crate::mutant::on("isa.layout/fit_wraps") && !(lo..=hi).contains(&(value as i128)) {
            return refuse(
                self.clause,
                format!(
                    "{format}: field {:?} is {} bits ({}), so it holds {lo}..{hi}; got {value}",
                    self.name,
                    self.width,
                    if self.signed { "signed" } else { "unsigned" }
                ),
            );
        }
        Ok(value as u64 & mask(self.width.min(64)))
    }

    /// Raw bits back to a value, sign-extending a signed field.
    pub fn unfit(&self, raw: u64) -> i64 {
        if self.signed && self.width < 64 && raw >> (self.width - 1) & 1 == 1 {
            (raw | !mask(self.width)) as i64
        } else {
            raw as i64
        }
    }
}

/// A named layout in a `width`-bit container.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Format {
    pub name: &'static str,
    pub width: u32,
    pub fields: Vec<Field>,
    /// Each field's lowest bit, in field order.
    pos: Vec<u32>,
    /// The clause an unknown or missing field name breaks.
    pub clause: &'static str,
}

/// One decoded field: name and value.
pub type Decoded = Vec<(&'static str, i64)>;

impl Format {
    /// Lay out `fields` MSB first. Panics on a malformed layout (two fields
    /// of one name, more bits than the container): a layout is a program
    /// constant, so it is a bug, not an input.
    pub fn new(name: &'static str, width: u32, clause: &'static str, fields: Vec<Field>) -> Format {
        let used: u32 = fields.iter().map(|f| f.width).sum();
        assert!(
            used <= width,
            "{name}: fields total {used} bits, container is {width}"
        );
        for (i, f) in fields.iter().enumerate() {
            assert!(f.width >= 1, "{name}: field {} has width 0", f.name);
            assert!(
                !fields[..i].iter().any(|g| g.name == f.name),
                "{name}: field {} appears twice",
                f.name
            );
        }
        let mut at = width;
        let pos = fields
            .iter()
            .map(|f| {
                at -= f.width;
                at
            })
            .collect();
        Format {
            name,
            width,
            fields,
            pos,
            clause,
        }
    }

    fn index(&self, name: &str) -> Option<usize> {
        self.fields.iter().position(|f| f.name == name)
    }

    pub fn field(&self, name: &str) -> Option<&Field> {
        self.index(name).map(|i| &self.fields[i])
    }

    /// `(lowest bit, width)` of field `name`. Panics on an unknown name.
    pub fn span(&self, name: &str) -> (u32, u32) {
        let i = self
            .index(name)
            .unwrap_or_else(|| panic!("{} has no field {name}", self.name));
        (self.pos[i], self.fields[i].width)
    }

    /// Field names `encode` takes: every one without a fixed value.
    pub fn settable(&self) -> Vec<&'static str> {
        self.fields
            .iter()
            .filter(|f| f.konst.is_none())
            .map(|f| f.name)
            .collect()
    }

    /// Pack `values` into one word; an omitted field takes its default.
    pub fn encode(&self, values: &[(&str, i64)]) -> Result<W256, Refusal> {
        for (name, _) in values {
            if self.index(name).is_none() {
                return refuse(
                    self.clause,
                    format!(
                        "{}: no field {name:?}; it takes {:?}",
                        self.name,
                        self.settable()
                    ),
                );
            }
        }
        let mut word = W256::ZERO;
        for (f, &lo) in self.fields.iter().zip(&self.pos) {
            let given = values
                .iter()
                .rev()
                .find(|(n, _)| *n == f.name)
                .map(|(_, v)| *v);
            let raw = match (f.konst, given, f.default) {
                (Some(k), Some(v), _) if v as u64 != k => {
                    return refuse(
                        self.clause,
                        format!("{}: {:?} is fixed at {k}, got {v}", self.name, f.name),
                    )
                }
                (Some(k), _, _) => k,
                (None, Some(v), _) | (None, None, Some(v)) => f.fit(self.name, v)?,
                (None, None, None) => {
                    return refuse(
                        self.clause,
                        format!("{}: field {:?} has no value", self.name, f.name),
                    )
                }
            };
            word = word.with(lo, f.width.min(64), raw);
        }
        Ok(word)
    }

    /// Whether every fixed field of `word` holds its value.
    pub fn matches(&self, word: W256) -> bool {
        self.fields
            .iter()
            .zip(&self.pos)
            .all(|(f, &lo)| match f.konst {
                Some(k) => word.get(lo, f.width) == k & mask(f.width),
                None => true,
            })
    }

    /// Every field of `word`. Refuses a word whose fixed field disagrees,
    /// or whose field wider than 64 bits holds more than 64 bits of value.
    pub fn decode(&self, word: W256) -> Result<Decoded, Refusal> {
        let mut out = Vec::with_capacity(self.fields.len());
        for (f, &lo) in self.fields.iter().zip(&self.pos) {
            if f.width > 64 && !word.is_zero_in(lo + 64, f.width - 64) {
                return refuse(
                    self.clause,
                    format!("{}: field {:?} holds more than 64 bits", self.name, f.name),
                );
            }
            let raw = word.get(lo, f.width.min(64));
            if let Some(k) = f.konst {
                if raw != k & mask(f.width.min(64)) {
                    return refuse(
                        self.clause,
                        format!(
                            "{}: field {:?} should be {k:#x}, word has {raw:#x}",
                            self.name, f.name
                        ),
                    );
                }
            }
            out.push((f.name, f.unfit(raw)));
        }
        Ok(out)
    }

    /// One readable line: the format's name and every settable field.
    pub fn disasm(&self, word: W256) -> String {
        match self.decode(word) {
            Ok(fields) => {
                let mut s = self.name.to_string();
                for ((name, v), f) in fields.iter().zip(&self.fields) {
                    if f.konst.is_none() {
                        if *v >= 0 {
                            s.push_str(&format!(" {name}={v:#x}"));
                        } else {
                            s.push_str(&format!(" {name}={v}"));
                        }
                    }
                }
                s
            }
            Err(r) => format!("<{r}>"),
        }
    }
}

/// The value of field `name` in a decode.
pub fn value(fields: &Decoded, name: &str) -> i64 {
    fields
        .iter()
        .find(|(n, _)| *n == name)
        .map(|(_, v)| *v)
        .unwrap_or_else(|| panic!("no decoded field {name}"))
}

/// Formats sharing a container, told apart by their fixed fields; the first
/// that matches a word claims it, so the most constrained come first.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InstSet {
    pub name: &'static str,
    pub formats: Vec<Format>,
}

impl InstSet {
    pub fn new(name: &'static str, formats: Vec<Format>) -> InstSet {
        for (i, f) in formats.iter().enumerate() {
            assert!(
                !formats[..i].iter().any(|g| g.name == f.name),
                "{name}: format {} is registered twice",
                f.name
            );
            assert_eq!(
                f.width, formats[0].width,
                "{name}: {} differs in width",
                f.name
            );
            assert!(
                f.fields.iter().any(|x| x.konst.is_some()),
                "{name}: {} has no fixed field to identify it",
                f.name
            );
        }
        InstSet { name, formats }
    }

    /// The first format whose fixed fields match `word`.
    pub fn find(&self, word: W256) -> Option<&Format> {
        self.formats.iter().find(|f| f.matches(word))
    }

    pub fn by_name(&self, name: &str) -> Option<&Format> {
        self.formats.iter().find(|f| f.name == name)
    }

    /// `(format name, fields)`, or the refusal when nothing matches.
    pub fn decode(&self, word: W256) -> Result<(&'static str, Decoded), Refusal> {
        match self.find(word) {
            Some(f) => Ok((f.name, f.decode(word)?)),
            None => refuse(
                self.formats.first().map_or("", |f| f.clause),
                format!("{}: no format matches {word}", self.name),
            ),
        }
    }

    pub fn disasm(&self, word: W256) -> String {
        match self.find(word) {
            Some(f) => f.disasm(word),
            None => format!("<unknown {word}>"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fmt() -> Format {
        Format::new(
            "T",
            32,
            "T.000",
            vec![
                Field::new("op", 5, "T.000").konst(3),
                Field::new("a", 5, "T.001"),
                Field::new("s", 6, "T.002").signed().default(0),
            ],
        )
    }

    #[test]
    fn fields_lay_msb_first_and_round_trip() {
        let f = fmt();
        assert_eq!(f.span("op"), (27, 5));
        assert_eq!(f.span("s"), (16, 6));
        let w = f.encode(&[("a", 7), ("s", -3)]).unwrap();
        assert_eq!(w.low_u64(), 3 << 27 | 7 << 22 | 0x3d << 16);
        assert_eq!(f.decode(w).unwrap(), vec![("op", 3), ("a", 7), ("s", -3)]);
        assert!(f.matches(w));
        assert_eq!(f.disasm(w), "T a=0x7 s=-3");
    }

    #[test]
    fn out_of_range_unknown_missing_and_fixed_are_refused() {
        let f = fmt();
        assert_eq!(f.encode(&[("a", 32)]).unwrap_err().clause, "T.001");
        assert_eq!(
            f.encode(&[("a", 1), ("s", 32)]).unwrap_err().clause,
            "T.002"
        );
        assert_eq!(f.encode(&[("a", 1), ("x", 0)]).unwrap_err().clause, "T.000");
        assert_eq!(f.encode(&[]).unwrap_err().clause, "T.000");
        assert_eq!(
            f.encode(&[("a", 1), ("op", 2)]).unwrap_err().clause,
            "T.000"
        );
        assert!(f.decode(W256::from_u64(1 << 27)).is_err());
    }
}
