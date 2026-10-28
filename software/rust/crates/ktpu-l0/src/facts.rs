//! The machine facts L0 reads, taken from `ktpu_machine::Machine`; every
//! field is the machine's.

use std::collections::BTreeMap;

use ktpu_machine::{Machine, UnitKind};

use crate::error::Result;
use crate::refuse;

/// A unit coordinate `(x, y)`.
pub type Coord = (u32, u32);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Facts {
    /// `fw.package.magic`, `.version`, `.header_bytes`, `.regs`.
    pub package_magic: u64,
    pub package_version: u64,
    pub header_bytes: u64,
    pub regs: u64,
    /// `fw.engine_units`: unit counters the dispatch engine keeps; the next
    /// source is the mover's.
    pub engine_units: usize,
    /// `node.engine.counter_bits`.
    pub counter_bits: u32,
    /// `fw.max_pkg_units`: units a package table holds (RUL.005).
    pub max_pkg_units: usize,
    /// `node.ring_depth`: rings the interlink queues (RUL.046).
    pub ring_depth: usize,
    /// `node.mover_span`: mover registers are below it.
    pub mover_span: u64,
    /// `unit.inst_depth`: a unit's instruction FIFO, the credit it is given.
    pub inst_depth: u64,
    /// `il.slots`: interlink signal slots.
    pub il_slots: u64,
    /// `fw.boot.version`, `fw.max_units` (the boot block's unit table).
    pub boot_version: u64,
    pub boot_max_units: usize,
    /// `fw.boot.regions`: entries of the boot block's region table.
    pub boot_regions: usize,
    /// `die.noc.addr_bits`: a unit-global address.
    pub addr_bits: u32,
    /// `die.word_bytes`: a memory word, the mover's address and stride unit.
    pub word_bytes: u64,
    /// `mover.levels`, `mover.max_index`.
    pub mover_levels: usize,
    pub mover_max_index: u64,
    /// `die.units`: unit type name (`"MG"`) -> coordinates, mesh `mesh`.
    pub units: BTreeMap<String, Vec<Coord>>,
    /// `die.fetch_port`.
    pub fetch_port: Option<Coord>,
    /// The mesh a kernel is written against.
    pub mesh: u32,
}

impl Facts {
    /// The facts of machine `m`, its kernels written against mesh 0.
    pub fn of(m: &Machine) -> Facts {
        Facts::seen_from(m, 0)
    }

    /// The facts of machine `m` seen from mesh `mesh` (every mesh of a card
    /// is the same die); refuses a mesh the card does not have.
    pub fn of_mesh(m: &Machine, mesh: u32) -> Result<Facts> {
        if mesh >= m.nodes() {
            return refuse!(
                "MACHINE",
                "machine {} has no mesh {mesh}; it has {}",
                m.name,
                m.nodes()
            );
        }
        Ok(Facts::seen_from(m, mesh))
    }

    fn seen_from(m: &Machine, mesh: u32) -> Facts {
        let mut units = BTreeMap::new();
        for kind in [UnitKind::Mg, UnitKind::Vc, UnitKind::Sr] {
            let coords: Vec<Coord> = m.units(kind).iter().map(|c| (c.0, c.1)).collect();
            if !coords.is_empty() {
                units.insert(kind.tag().to_string(), coords);
            }
        }
        let pick = |name: &str, mutated: u64, real: u64| {
            if crate::mutant::on(name) {
                mutated
            } else {
                real
            }
        };
        Facts {
            regs: pick("l0.pkg/regs", 8, m.fw.package.regs as u64),
            inst_depth: pick("l0.program/inst_depth", 256, m.unit.inst_depth as u64),
            boot_max_units: pick("l0.boot/max_units", 15, m.fw.max_units as u64) as usize,
            package_magic: m.fw.package.magic as u64,
            package_version: m.fw.package.version as u64,
            header_bytes: m.fw.package.header_bytes as u64,
            engine_units: m.fw.engine_units as usize,
            counter_bits: m.node.engine.counter_bits,
            max_pkg_units: m.fw.max_pkg_units as usize,
            ring_depth: m.node.ring_depth as usize,
            mover_span: m.node.mover_span as u64,
            il_slots: m.il.slots as u64,
            boot_version: m.fw.boot.version as u64,
            boot_regions: m.fw.boot.regions as usize,
            addr_bits: m.die.noc.addr_bits,
            word_bytes: m.die.word_bytes as u64,
            mover_levels: m.mover.levels as usize,
            mover_max_index: m.mover.max_index as u64,
            units,
            fetch_port: Some((m.die.fetch_port.0, m.die.fetch_port.1)),
            mesh,
        }
    }

    /// Coordinate -> unit type name, for the mesh (`units.unit_types`).
    pub fn unit_types(&self) -> BTreeMap<Coord, String> {
        let mut out = BTreeMap::new();
        for (kind, coords) in &self.units {
            for c in coords {
                out.insert(*c, kind.clone());
            }
        }
        out
    }

    /// The dispatch-engine source of the mover's done count.
    pub fn src_mover(&self) -> u64 {
        self.engine_units as u64
    }

    /// The most units a package may name: the smallest of the package table,
    /// the dispatch engine's counters and the boot unit table (RUL.005).
    pub fn unit_bound(&self) -> usize {
        self.max_pkg_units
            .min(self.engine_units)
            .min(self.boot_max_units)
    }

    /// A `WAIT` want's mask (`node.engine.counter_bits`).
    pub fn ctr_mask(&self) -> u64 {
        if crate::mutant::on("l0.lower/ctr_mask") {
            return 0x7FF;
        }
        (1u64 << self.counter_bits) - 1
    }
}
