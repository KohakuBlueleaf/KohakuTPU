//! A machine's units as a package's unit table (L0.PKG.016).

use crate::error::Result;
use crate::facts::Facts;
use crate::format::{type_code, unit_word};

/// Every unit of the machine's mesh as package unit words, by type name then
/// coordinate order.
pub fn machine_units(f: &Facts) -> Result<Vec<u64>> {
    let mut out = Vec::new();
    for (kind, coords) in &f.units {
        let t = type_code(kind)?;
        for &(x, y) in coords {
            out.push(unit_word(t, x as u64, y as u64, f.mesh as u64));
        }
    }
    Ok(out)
}
