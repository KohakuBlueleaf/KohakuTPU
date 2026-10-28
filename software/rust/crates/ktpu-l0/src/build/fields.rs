//! The project's address fields (`isa/fields.py`): `ktpu_isa::addresses` as
//! the builder's [`Addresses`], by the unit type the builder names a payload
//! for (`"MG"`, `"VC"`, `"SR"`, `"mover"`).

use ktpu_isa::{Isa, Machine, Target, UnitKind};

use super::{AddrField, Addresses};
use crate::error::{Refusal, Result};
use crate::word::Word;

pub struct IsaAddresses {
    pub isa: Isa,
}

impl IsaAddresses {
    pub fn new(m: &Machine) -> Result<IsaAddresses> {
        let isa = Isa::new(m).map_err(|e| Refusal::new("ISA", e.to_string()))?;
        Ok(IsaAddresses { isa })
    }
}

impl Addresses for IsaAddresses {
    fn fields(&self, word: &Word, unit_type: &str) -> Vec<AddrField> {
        let target = match unit_type {
            "mover" => Target::Mover,
            t => match UnitKind::parse(t) {
                Some(k) => Target::Unit(k),
                None => return Vec::new(),
            },
        };
        ktpu_isa::addresses(&self.isa, *word, target)
            .into_iter()
            .map(|f| AddrField {
                segments: f
                    .segments
                    .iter()
                    .map(|s| (s.bit, s.width, s.shift))
                    .collect(),
                value: f.value,
            })
            .collect()
    }
}
