//! The machine facts the L2 stage reads, taken from a `ktpu_machine::Machine`
//! once (every hardware number comes from the machine file), and the stack's
//! own choices (`docs/spec/stack/l2/program.md` Parameters).

use ktpu_machine::{Machine, UnitKind};

use crate::mutant::on;

/// The top of a receiving core's L1 clusters drain tiles into: the stack's
/// landing pool, in words.
pub const POOL: i64 = 256;
/// IMEM words an image may take: the stack's image room.
pub const IMEM_ROOM: i64 = 1000;
/// Words one move takes at most: the stack's move bound.
pub const MOVE_WORDS: i64 = 1 << 14;

/// The register shape the vector lowering's templates are written for:
/// reductions over 16 lanes x 8 chunks, GT4 bands of 4 rows by 32 columns,
/// 16 f16 values a word (L2.VC.025).
pub const VECTOR_SHAPE: [(&str, i64); 6] = [
    ("vc.lanes", 16),
    ("vc.chunks", 8),
    ("vc.vlmax", 128),
    ("die.word_bytes", 32),
    ("mg.lanes", 4),
    ("mg.kblock", 32),
];

#[derive(Clone, Debug)]
pub struct Target {
    /// The machine file the facts come from (the image encoder reads it).
    pub machine: Machine,
    /// Nodes of the card (meshes 0..nodes-1).
    pub nodes: i64,
    /// Unit coordinates `(x, y)` by kind, unit-index order.
    pub mg: Vec<(i64, i64)>,
    pub vc: Vec<(i64, i64)>,
    /// The system node's memory ports.
    pub ports: Vec<(i64, i64)>,
    pub word_bytes: i64,
    // vector core
    pub vregs: u32,
    pub vcopies: u32,
    pub l1_words: i64,
    pub descriptors: usize,
    pub vlmax: i64,
    pub lanes: i64,
    pub chunks: i64,
    pub sregs: i64,
    pub ainc_bits: u32,
    pub queue_depth: usize,
    pub dma_queue_depth: usize,
    /// (cycles before the first word, cycles a word) of a fill alone, and
    /// beside streaming clusters.
    pub fill_alone: (i64, i64),
    pub fill_shared: (i64, i64),
    // matmul cluster
    pub bank: i64,
    pub tiles: i64,
    pub max_fill: i64,
    pub shared_b_max: i64,
    pub kstep: i64,
    pub kblock: i64,
    pub mg_lanes: i64,
    // mover
    pub levels: usize,
    pub max_index: i64,
    pub row_words: i64,
    pub one_word_runs: bool,
    // interlink
    pub il_slots: i64,
}

fn coords(m: &Machine, k: UnitKind) -> Vec<(i64, i64)> {
    m.units(k)
        .iter()
        .map(|c| (c.0 as i64, c.1 as i64))
        .collect()
}

impl Target {
    pub fn of(m: &Machine) -> Target {
        Target {
            machine: m.clone(),
            nodes: m.card.nodes as i64,
            mg: coords(m, UnitKind::Mg),
            vc: coords(m, UnitKind::Vc),
            ports: m
                .die
                .sys_ports
                .iter()
                .map(|c| (c.0 as i64, c.1 as i64))
                .collect(),
            word_bytes: m.die.word_bytes as i64,
            vregs: m.vc.regs,
            vcopies: m.vc.rf_copies,
            l1_words: m.vc.l1_words as i64,
            descriptors: m.vc.descriptors as usize,
            vlmax: m.vc.vlmax as i64,
            lanes: m.vc.lanes as i64,
            chunks: m.vc.chunks as i64,
            sregs: m.vc.sregs as i64,
            ainc_bits: m.vc.ainc_bits,
            queue_depth: m.vc.queue_depth as usize,
            dma_queue_depth: m.vc.dma_queue_depth as usize,
            fill_alone: (m.vc.fill_alone.0 as i64, m.vc.fill_alone.1 as i64),
            fill_shared: (m.vc.fill_shared.0 as i64, m.vc.fill_shared.1 as i64),
            bank: m.mg.l1_bank_entries as i64,
            tiles: m.mg.tiles as i64,
            max_fill: m.mg.max_fill as i64,
            shared_b_max: m.mg.shared_b_max as i64,
            kstep: m.mg.kstep as i64,
            kblock: m.mg.kblock as i64,
            mg_lanes: m.mg.lanes as i64,
            levels: m.mover.levels as usize,
            max_index: m.mover.max_index as i64,
            row_words: m.mover.row_words as i64,
            one_word_runs: m.mover.one_word_runs,
            il_slots: m.il.slots as i64,
        }
        .mutated()
    }

    /// The facts the active spec mutant changes (`crate::mutant`).
    fn mutated(mut self) -> Target {
        let pick = |name: &str, v: i64, to: i64| if on(name) { to } else { v };
        self.bank = pick("l2.cluster/bank-128", self.bank, 128);
        self.tiles = pick("l2.cluster/acc-4095", self.tiles, 4095);
        self.max_fill = pick("l2.cluster/max-fill-256", self.max_fill, 256);
        self.max_fill = pick("l2.cluster/max-fill-100", self.max_fill, 100);
        self.l1_words = pick("l2.vector/l1-384", self.l1_words, 384);
        self.vlmax = pick("l2.vector/row-64", self.vlmax, 64);
        self.descriptors = pick("l2.vector/descs-1", self.descriptors as i64, 1) as usize;
        self.max_index = pick("l2.mover/max-index-8", self.max_index, 8);
        self.levels = pick("l2.mover/ndim-7", self.levels as i64, 7) as usize;
        if on("l2.target/vregs-default") {
            self.vregs = 32;
        }
        self
    }

    /// The stack's landing pool, in words.
    pub fn pool(&self) -> i64 {
        if on("l2.lower/pool-128") {
            128
        } else if on("isa.vector/links") {
            512
        } else {
            POOL
        }
    }

    /// Ring room a stream into a cluster side takes at most: both banks.
    pub fn ring_entries(&self) -> i64 {
        2 * self.machine.mg.l1_bank_entries as i64
    }

    /// The machine with a card of `nodes` nodes.
    pub fn with_nodes(mut self, nodes: i64) -> Target {
        self.nodes = nodes;
        self
    }

    /// Units of a kind (`"mg"` / `"vc"`) a node carries.
    pub fn count(&self, kind: &str) -> i64 {
        match kind {
            "mg" | "MG" => self.mg.len() as i64,
            "vc" | "VC" => self.vc.len() as i64,
            _ => 0,
        }
    }

    /// The memory port fewest hops from `c` (the first on a tie).
    pub fn nearest_port(&self, c: (i64, i64)) -> (i64, i64) {
        let hops = |p: &(i64, i64)| (c.0 - p.0).abs() + (c.1 - p.1).abs();
        let mut best = self.ports[0];
        for p in &self.ports {
            if hops(p) < hops(&best) {
                best = *p;
            }
        }
        best
    }

    /// `(latency, cycles a word)` of a vector core's fill, alone or beside
    /// streaming clusters.
    pub fn mem(&self, shared: bool) -> (i64, i64) {
        if shared {
            self.fill_shared
        } else {
            self.fill_alone
        }
    }

    /// A descriptor's step a trip: signed `ainc_bits` bits.
    pub fn step(&self) -> i64 {
        1 << (self.ainc_bits - 1)
    }

    /// Scalar registers free for constants: the file less the sticky flag
    /// and the loop count.
    pub fn stk_s(&self) -> i64 {
        if on("l2.vector/stk-s13") {
            return 13;
        }
        self.sregs - 2
    }

    /// The scalar register of the loop count.
    pub fn loop_s(&self) -> i64 {
        if on("l2.vector/loop-s13") {
            return 13;
        }
        self.sregs - 1
    }

    /// The scalar register a constant past the scalar file is set into.
    pub fn stage_reg(&self) -> i64 {
        self.sregs - 3
    }

    /// A gemm may sweep `nk` K blocks: one, or a whole number of issues.
    pub fn legal_nk(&self, nk: i64) -> bool {
        nk == 1 || nk % self.kstep == 0
    }

    /// The machine's values that differ from [`VECTOR_SHAPE`], as
    /// "fact N (the lowering's M)".
    pub fn shape_misfit(&self) -> Vec<String> {
        if on("l2.vector/shape-unchecked") {
            return Vec::new();
        }
        let got = |f: &str| match f {
            "vc.lanes" => self.lanes,
            "vc.chunks" => self.chunks,
            "vc.vlmax" => self.vlmax,
            "die.word_bytes" => self.word_bytes,
            "mg.lanes" => self.mg_lanes,
            _ => self.kblock,
        };
        VECTOR_SHAPE
            .iter()
            .filter(|(f, v)| got(f) != *v)
            .map(|(f, v)| format!("{f} {} (the lowering's {v})", got(f)))
            .collect()
    }

    /// Words an MXFP7 entry takes: `mg.entry_bits` in whole words.
    pub fn entry_words(&self) -> i64 {
        let word_bits = 8 * self.word_bytes;
        (self.machine.mg.entry_bits as i64 + word_bits - 1) / word_bits
    }

    /// f16 values a word holds.
    pub fn f16_word(&self) -> i64 {
        self.word_bytes / 2
    }

    /// L1 entries of a cluster side a `rows x cols` f16 tile takes: lane
    /// groups of `mg.lanes` rows by K blocks of `mg.kblock`.
    pub fn entries(&self, rows: i64, cols: i64) -> i64 {
        (rows / self.mg_lanes) * (cols / self.kblock)
    }

    /// The stack's staging room a node (`Machine::stage_room`).
    pub fn stage_room(&self) -> i64 {
        self.machine.stage_room()
    }
}
