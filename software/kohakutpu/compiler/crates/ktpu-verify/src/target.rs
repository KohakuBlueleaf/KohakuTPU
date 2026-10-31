//! What the checker reads of the machine: each mesh's units and memory
//! ports, and the unit facts its model takes, by machine-file path
//! (`machine.md`).

use std::sync::Arc;

use ktpu_isa::Isa;
use ktpu_machine::Machine;
use serde_json::Value;

/// Times a node loop on data is run (verify.md s5).
pub const TRIPS: u32 = 3;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum UnitKind {
    Mg,
    Vc,
    Sr,
}

impl UnitKind {
    pub fn tag(self) -> &'static str {
        match self {
            UnitKind::Mg => "mg",
            UnitKind::Vc => "vc",
            UnitKind::Sr => "sr",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct MeshUnits {
    pub index: u32,
    pub mg: Vec<(u8, u8)>,
    pub vc: Vec<(u8, u8)>,
    pub sr: Vec<(u8, u8)>,
    pub mem_ports: Vec<(u8, u8)>,
}

impl MeshUnits {
    /// The kind at (x, y): MG, then VC, then SR.
    pub fn kind_at(&self, xy: (u8, u8)) -> Option<UnitKind> {
        if self.mg.contains(&xy) {
            Some(UnitKind::Mg)
        } else if self.vc.contains(&xy) {
            Some(UnitKind::Vc)
        } else if self.sr.contains(&xy) {
            Some(UnitKind::Sr)
        } else {
            None
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Facts {
    /// `fw.fetch_depth`: a fetch run's room.
    pub fetch_depth: i64,
    /// `vc.l1_words`, `vc.walk_max` (a peer drain's words, F_LEN).
    pub vc_l1_words: i64,
    pub vc_walk_max: i64,
    /// `vc.dma_queue_depth` (FILL, DRAIN), `vc.queue_depth` (UNPACK, MATH, PACK).
    pub vc_dma_queue_depth: usize,
    /// `vc.fill_track_depth`: fills taken and unretired (ISA.VC.193).
    pub vc_fill_track_depth: u32,
    pub vc_queue_depth: usize,
    /// `vc.dualq`: independent unpack and pack queue 1.
    pub vc_dualq: bool,
    /// `vc.regs`.
    pub vc_regs: u32,
    /// VC scalar/predicate counts, descriptor dimensions and pack geometry.
    pub vc_sregs: i64,
    pub vc_preds: i64,
    pub vc_descriptor_dims: u32,
    pub vc_has_mx: bool,
    pub mover_quant_allowed: bool,
    pub vector_mx7_allowed: bool,
    pub vc_gt4_words: i64,
    /// `mg.l1_a_entries`, `mg.l1_b_entries`, `mg.l1_bank_entries`, `mg.tiles`.
    pub mg_l1_a_entries: i64,
    pub mg_l1_b_entries: i64,
    pub mg_bank_entries: i64,
    pub mg_tiles: i64,
    /// Granules of an L1 entry (`mg.entry_bits` over `die.word_bytes`) and
    /// of a peer accumulator sub-tile (`mg.lanes` squared elements of
    /// `mg.acc_bits`), each rounded up.
    pub mg_entry_granules: i64,
    pub mg_subtile_granules: i64,
    /// `mg.dq_limit` (sub-tiles the drain queue holds before a sweep is
    /// held), `mg.wburst`.
    pub mg_dq_room: i64,
    pub mg_burst: i64,
    /// `sr.fifo_depth`, `sr.burst` (an output burst, words), `sr.held`
    /// (sub-rows held past the output that has left); 0 on a die without
    /// row units.
    pub sr_fifo: i64,
    pub sr_burst: i64,
    pub sr_held: i64,
    /// `die.word_bytes`: bytes of a granule (a byte address over it is a
    /// word offset).
    pub word_bytes: i64,
    /// `card.dram.line_bytes`: independently owned external write line.
    pub line_bytes: i64,
    /// `fw.package.unbounded`: the node-wide cap (boot `cq_depth` 0: none)
    /// the card boots with (L0.STEP.008); a table credit of 0 takes it
    /// (RUL.042).
    pub unbounded: i64,
}

#[derive(Clone, Debug)]
pub struct Target {
    pub machine: Machine,
    pub meshes: Vec<MeshUnits>,
    pub facts: Facts,
    /// The unit instruction formats of the machine (`ktpu-isa`).
    pub isa: Arc<Isa>,
}

impl PartialEq for Target {
    fn eq(&self, o: &Target) -> bool {
        self.meshes == o.meshes && self.facts == o.facts
    }
}

impl Eq for Target {}

/// The fact at a dotted path.
type Look<'a> = &'a dyn Fn(&str) -> Option<&'a Value>;

fn at<'a>(v: Look<'a>, path: &str) -> Result<&'a Value, String> {
    v(path).ok_or_else(|| format!("machine has no fact {path}"))
}

fn int(v: Look, path: &str) -> Result<i64, String> {
    at(v, path)?
        .as_i64()
        .ok_or_else(|| format!("machine fact {path} is not an integer"))
}

fn coords(v: Look, path: &str) -> Result<Vec<(u8, u8)>, String> {
    let list = at(v, path)?
        .as_array()
        .ok_or_else(|| format!("machine fact {path} is not a list"))?;
    list.iter()
        .map(|p| {
            let x = p.get(0).and_then(Value::as_u64);
            let y = p.get(1).and_then(Value::as_u64);
            match (x, y) {
                (Some(x), Some(y)) => Ok((x as u8, y as u8)),
                _ => Err(format!("machine fact {path} holds {p}")),
            }
        })
        .collect()
}

impl Target {
    /// The target of machine `m`: `nodes` identical meshes (the card's
    /// node count when None).
    pub fn from_machine(m: &Machine, nodes: Option<u32>) -> Result<Target, String> {
        let isa = Isa::reference(m).map_err(|r| r.to_string())?;
        let look = |path: &str| m.get(path);
        Target::from_facts(&look, nodes, Arc::new(isa), m.clone())
    }

    fn from_facts(
        m: Look,
        nodes: Option<u32>,
        isa: Arc<Isa>,
        machine: Machine,
    ) -> Result<Target, String> {
        let nodes = match nodes {
            Some(n) => n,
            None => int(m, "card.nodes")? as u32,
        };
        let mg = coords(m, "die.units.mg")?;
        let vc = coords(m, "die.units.vc")?;
        let sr = coords(m, "die.units.sr")?;
        let mem_ports = coords(m, "die.sys_ports")?;
        let meshes = (0..nodes.max(1))
            .map(|i| MeshUnits {
                index: i,
                mg: mg.clone(),
                vc: vc.clone(),
                sr: sr.clone(),
                mem_ports: mem_ports.clone(),
            })
            .collect();
        let word_bytes = int(m, "die.word_bytes")?;
        let word_bits = word_bytes * 8;
        let lanes = int(m, "mg.lanes")?;
        let facts = Facts {
            fetch_depth: int(m, "fw.fetch_depth")?,
            vc_l1_words: int(m, "vc.l1_words")?,
            vc_walk_max: int(m, "vc.walk_max")?,
            vc_dma_queue_depth: int(m, "vc.dma_queue_depth")? as usize,
            vc_fill_track_depth: int(m, "vc.fill_track_depth")? as u32,
            vc_queue_depth: int(m, "vc.queue_depth")? as usize,
            vc_dualq: int(m, "vc.dualq")? != 0,
            vc_regs: int(m, "vc.regs")? as u32,
            vc_sregs: int(m, "vc.sregs")?,
            vc_preds: int(m, "vc.preds")?,
            vc_descriptor_dims: int(m, "vc.descriptor_dims")? as u32,
            vc_has_mx: int(m, "vc.has_mx")? != 0,
            mover_quant_allowed: at(m, "conversion.fp16_mx7.allowed_backends")?
                .as_array()
                .ok_or("packing backends must be a list")?
                .iter()
                .any(|v| v.as_str() == Some("mover_quant")),
            vector_mx7_allowed: at(m, "conversion.fp16_mx7.allowed_backends")?
                .as_array()
                .ok_or("packing backends must be a list")?
                .iter()
                .any(|v| v.as_str() == Some("vector_mx7")),
            vc_gt4_words: word_bits / int(m, "mover.xform.gt4_granule_bits")?,
            mg_l1_a_entries: int(m, "mg.l1_a_entries")?,
            mg_l1_b_entries: int(m, "mg.l1_b_entries")?,
            mg_bank_entries: int(m, "mg.l1_bank_entries")?,
            mg_tiles: int(m, "mg.tiles")?,
            mg_entry_granules: (int(m, "mg.entry_bits")? + word_bits - 1) / word_bits,
            mg_subtile_granules: (lanes * lanes * int(m, "mg.acc_bits")? + word_bits - 1)
                / word_bits,
            mg_dq_room: int(m, "mg.dq_limit")?,
            mg_burst: int(m, "mg.wburst")?,
            sr_fifo: at(m, "sr.fifo_depth")?.as_i64().unwrap_or(0),
            sr_burst: at(m, "sr.burst")?.as_i64().unwrap_or(0),
            sr_held: at(m, "sr.held")?.as_i64().unwrap_or(0),
            word_bytes,
            line_bytes: int(m, "card.dram.line_bytes")?,
            unbounded: int(m, "fw.package.unbounded")?,
        };
        Ok(Target {
            machine,
            meshes,
            facts,
            isa,
        })
    }

    pub fn mesh(&self, index: u32) -> Option<&MeshUnits> {
        self.meshes.iter().find(|m| m.index == index)
    }

    /// A stable text of everything the checker reads here, for keys.
    pub fn digest_text(&self) -> String {
        format!("{:?} {:?}", self.meshes, self.facts)
    }
}
