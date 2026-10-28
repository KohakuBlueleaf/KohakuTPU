//! Runtime card topology and addresses, derived only from machine facts.

use crate::schema::die::Address;
use crate::{mutant, Coord, Machine, Refusal, Sr, Units};

#[derive(Clone, Debug)]
pub struct Mesh {
    pub index: u32,
    pub units: Units,
    pub mem_ports: Vec<Coord>,
    pub agent: Coord,
}

#[derive(Clone, Debug)]
pub struct Card {
    pub board: String,
    pub name: String,
    pub meshes: Vec<Mesh>,
    pub links: Vec<(u32, u32)>,
    pub fetch: Coord,
    pub inst_depth: u32,
    pub stage_flits: u32,
    pub stage_bytes: u64,
    pub regs: u32,
    pub rf_copies: u32,
    pub sr: Sr,
    pub one_word_runs: bool,
    address: Address,
    aperture: u32,
}

fn invalid(message: &str) -> Refusal {
    Refusal::new("MACHINE", message)
}

impl Card {
    pub fn of(m: &Machine, nodes: Option<u32>, units: Option<Units>) -> Result<Self, Refusal> {
        let mut a = m.die.address.clone();
        if mutant::on("machine.card/mesh-bits") {
            a.mesh_bits = 2;
        }
        if mutant::on("machine.card/mesh-shift") {
            a.mesh_shift = 36;
        }
        let fits = |lo: u32, bits: u32, end: u32| {
            bits > 0 && lo.checked_add(bits).is_some_and(|hi| hi <= end)
        };
        if !(a.special_bit < m.die.noc.addr_bits
            && m.die.noc.addr_bits <= 64
            && a.reserved_bit < a.special_bit
            && fits(a.mesh_shift, a.mesh_bits, a.reserved_bit)
            && fits(a.aperture_shift, a.aperture_bits, a.mesh_shift))
        {
            return Err(invalid("address fields must be disjoint and ordered"));
        }
        let stage_bytes = if mutant::on("machine.card/stage-banks") {
            u64::from(m.die.staging.entries)
                .checked_mul(u64::from(m.die.staging.banks))
                .and_then(|n| n.checked_mul(u64::from(m.die.word_bytes)))
        } else {
            m.die.stage_bytes()
        }
        .ok_or_else(|| invalid("staging byte size overflows"))?;
        let aperture = m.die.staging.aperture;
        if stage_bytes == 0
            || stage_bytes > (1u64 << a.aperture_shift)
            || u64::from(aperture) >= (1u64 << a.aperture_bits)
            || aperture >= 64
            || (m.die.staging.implemented >> aperture) & 1 == 0
        {
            return Err(invalid("staging does not fit an implemented aperture"));
        }
        let count = nodes.unwrap_or(m.card.nodes);
        if count == 0 || u64::from(count) > (1u64 << a.mesh_bits) {
            return Err(invalid("node count does not fit the mesh field"));
        }
        let chain: Vec<u32> = if nodes.is_some() {
            (0..count).collect()
        } else {
            m.card.chain.clone()
        };
        let mut sorted = chain.clone();
        sorted.sort_unstable();
        if sorted != (0..count).collect::<Vec<_>>() {
            return Err(invalid("card chain must contain every mesh exactly once"));
        }
        let agent = *m
            .die
            .sys_ports
            .first()
            .ok_or_else(|| invalid("no reply port"))?;
        let units = units.unwrap_or_else(|| m.die.units.clone());
        Ok(Self {
            board: m.name.clone(),
            name: m.die.target.clone(),
            meshes: (0..count)
                .map(|index| Mesh {
                    index,
                    units: units.clone(),
                    mem_ports: m.die.sys_ports.clone(),
                    agent,
                })
                .collect(),
            links: chain.windows(2).map(|p| (p[0], p[1])).collect(),
            fetch: m.die.fetch_port,
            inst_depth: m.unit.inst_depth,
            stage_flits: m.die.staging.flits,
            stage_bytes,
            regs: m.vc.regs,
            rf_copies: m.vc.rf_copies,
            sr: m.sr.clone(),
            one_word_runs: m.mover.one_word_runs,
            address: a,
            aperture,
        })
    }

    pub fn mesh(&self, index: u32) -> Result<&Mesh, Refusal> {
        self.meshes
            .iter()
            .find(|m| m.index == index)
            .ok_or_else(|| invalid("absent mesh"))
    }

    pub fn global_addr(&self, base: u64, mesh: u32) -> Result<u64, Refusal> {
        self.mesh(mesh)?;
        if base >= (1u64 << self.address.mesh_shift) {
            return Err(invalid("base does not fit one mesh"));
        }
        Ok(base | (u64::from(mesh) << self.address.mesh_shift))
    }

    pub fn stage_addr(&self, offset: u64, mesh: u32) -> Result<u64, Refusal> {
        self.mesh(mesh)?;
        if offset >= self.stage_bytes {
            return Err(invalid("offset past staging"));
        }
        Ok((1u64 << self.address.special_bit)
            | (u64::from(mesh) << self.address.mesh_shift)
            | (u64::from(self.aperture) << self.address.aperture_shift)
            | offset)
    }
}
