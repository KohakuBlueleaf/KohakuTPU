//! The boot block a node's firmware reads at its scratchpad's base
//! (docs/spec/stack/l0/boot-block.md): nine header words, the unit table,
//! then the region table (`fw.boot.regions` entries of base, size, use;
//! RUL.024).

use crate::error::Result;
use crate::facts::Facts;
use crate::refuse;

/// "KABOOT1" (L0.BOOT.004).
pub const MAGIC: u64 = 0x0031_544F_4F42_414B;
/// Header words before the unit table.
pub const HEAD_WORDS: usize = 9;

/// What a node is told before it boots (L0.BOOT.004-L0.BOOT.012).
///
/// `queue` is the unit-global address of the node's queue region. `timeout`
/// is the wait bound in node cycles, the caller's seconds times the active
/// profile's sysnode clock; there is no default (RUL.033). With no `units`,
/// the firmware finds its own by a caps read of every coordinate in
/// `0..scan`. `cq_depth` 0 sets no node-wide completion bound.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BootArgs {
    pub queue: u64,
    pub mesh: u64,
    pub scan: u64,
    pub timeout: u64,
    pub cq_depth: u64,
    pub flags: u64,
    pub units: Vec<u64>,
    /// Memory regions the host assigns the node, unit-global bytes; `use_`
    /// is a `fw.boot.region_uses` code (0 marks an empty entry).
    pub regions: Vec<Region>,
}

/// One region table entry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Region {
    pub base: u64,
    pub size: u64,
    pub use_: u64,
}

impl BootArgs {
    /// `new` for a caller's `timeout`, which it must name (RUL.033).
    pub fn named(queue: u64, timeout: Option<u64>) -> Result<BootArgs> {
        match timeout {
            Some(t) => Ok(BootArgs::new(queue, t)),
            None if crate::mutant::on("l0.boot/raw_default") => Ok(BootArgs::new(queue, 2_000_000)),
            None => refuse!("RUL.033", "no timeout: the caller names the bound"),
        }
    }

    pub fn new(queue: u64, timeout: u64) -> BootArgs {
        BootArgs {
            queue,
            mesh: 0,
            scan: 0,
            timeout,
            cq_depth: if crate::mutant::on("l0.boot/defaults") {
                16
            } else {
                0
            },
            flags: 0,
            units: Vec::new(),
            regions: Vec::new(),
        }
    }

    /// The block's words. Refuses more units than `fw.max_units`
    /// (L0.BOOT.014) or more regions than `fw.boot.regions` (RUL.024).
    pub fn words(&self, f: &Facts) -> Result<Vec<u64>> {
        if self.units.len() > f.boot_max_units {
            return refuse!(
                "L0.BOOT.014",
                "{} units; the firmware's table holds {}",
                self.units.len(),
                f.boot_max_units
            );
        }
        if self.regions.len() > f.boot_regions {
            return refuse!(
                "RUL.024",
                "{} regions; the firmware's table holds {}",
                self.regions.len(),
                f.boot_regions
            );
        }
        let magic = if crate::mutant::on("l0.boot/magic") {
            0x0032_544F_4F42_414B
        } else {
            MAGIC
        };
        let mut words = vec![
            magic,
            f.boot_version,
            self.queue,
            self.mesh,
            self.scan,
            self.timeout,
            self.cq_depth,
            self.flags,
            self.units.len() as u64,
        ];
        words.extend_from_slice(&self.units);
        words.resize(HEAD_WORDS + f.boot_max_units, 0);
        for r in &self.regions {
            if crate::mutant::on("l0.boot/region_order") {
                words.extend_from_slice(&[r.size, r.base, r.use_]);
            } else {
                words.extend_from_slice(&[r.base, r.size, r.use_]);
            }
        }
        words.resize(HEAD_WORDS + f.boot_max_units + 3 * f.boot_regions, 0);
        Ok(words)
    }

    /// The block's bytes, little-endian.
    pub fn pack(&self, f: &Facts) -> Result<Vec<u8>> {
        Ok(self
            .words(f)?
            .iter()
            .flat_map(|w| w.to_le_bytes())
            .collect())
    }
}
