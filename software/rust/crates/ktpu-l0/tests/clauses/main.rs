//! One test per L0 clause (docs/spec/stack/l0/*.md), named after it; each
//! checks the property against values written out from the spec text.

#[path = "../common/mod.rs"]
mod common;

mod bind;
mod boot;
mod de;
mod facts;
mod pkg;
mod rul;
mod step;

use ktpu_l0::{Facts, Package, PackageBuilder, Step, Unit, Word};
use std::collections::BTreeMap;

/// The 64-bit little-endian word at byte `at`.
pub fn q(b: &[u8], at: usize) -> u64 {
    u64::from_le_bytes(b[at..at + 8].try_into().unwrap())
}

pub fn f() -> Facts {
    common::v9t6()
}

pub fn mg(x: u64, y: u64) -> Unit {
    Unit {
        type_code: 0x4D47,
        x,
        y,
        mesh: 0,
        credit: 512,
        fetch: 0,
    }
}

pub fn with_port(u: Unit) -> Unit {
    Unit {
        fetch: 1 << 16 | 2 << 8,
        ..u
    }
}

pub fn pkg(units: Vec<Unit>, steps: Vec<Step>, payloads: Vec<u64>) -> Package {
    Package {
        units,
        steps,
        payloads: payloads.into_iter().map(Word::from_u64).collect(),
        ..Package::default()
    }
}

/// A builder for the v9t6 die's types, credit 512.
pub fn builder() -> PackageBuilder {
    let f = f();
    PackageBuilder::new(&f, f.unit_types(), 512)
}

pub fn types(list: &[((u32, u32), &str)]) -> BTreeMap<(u32, u32), String> {
    list.iter().map(|&(c, t)| (c, t.to_string())).collect()
}
