//! The matmul cluster (`MG`): FILL, GEMM, DRAIN, POST, one 256-bit payload
//! each, every opcode on one field layout (`docs/spec/stack/isa/cluster.md`,
//! RTL `mx_cluster_cu.v`).

use crate::layout::{bits_for, mask, value, Field, Format, InstSet, W256};
use crate::reloc::{AddressField, Segment};
use ktpu_machine::{refuse, Coord, Machine, Refusal};
use serde::{Deserialize, Serialize};

pub const OP_FILL: u64 = 1;
pub const OP_GEMM: u64 = 2;
pub const OP_DRAIN: u64 = 3;
pub const OP_POST: u64 = 4;

/// CU_DATA descriptor flags: [0] answer DATA_RECEIVED, [1] COUNT.
pub const DFLAG_SIGNAL: u32 = 1;
pub const DFLAG_COUNT: u32 = 2;

/// DRAIN to a unit: which of a cluster's buffers.
pub const BUF_L1A: u32 = 0;
pub const BUF_L1B: u32 = 1;
pub const BUF_ACC: u32 = 2;

/// `2 * SBIAS` of MXFP7, the scale anchor of every GEMM the stack emits
/// (`numerics/formats.md`).
pub const ANCHOR: u32 = 40;

/// Address bits held in `addr`; the rest of `die.noc.addr_bits` sit in
/// `addr_hi` near the tail, so widening the address moved no field.
pub const ADDR_LO_BITS: u32 = 34;
const OP_BITS: u32 = 4;
/// Bits of a word's count `n`: FILL entries, DRAIN sub-tiles, POST credits
/// (ISA.MG.003).
pub const N_BITS: u32 = 16;
const GRID_BITS: u32 = 8;

/// The machine facts the cluster encoding depends on.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClusterCfg {
    pub payload_bits: u32,
    pub addr_hi_bits: u32,
    pub pos_bits: u32,
    pub max_fill: u32,
    pub max_peers: u32,
    pub kstep: u32,
    /// Entries of one L1 bank: a FILL's `eoff + n` stays within it (RUL.048).
    pub bank_entries: u32,
    /// A GEMM's scale anchor when its text names none ([`ANCHOR`]).
    pub anchor: u32,
}

impl ClusterCfg {
    pub fn of(m: &Machine) -> Result<ClusterCfg, Refusal> {
        if m.die.noc.addr_bits < ADDR_LO_BITS {
            return refuse(
                "ISA.MG.007",
                format!(
                    "die.noc.addr_bits {} is below the {ADDR_LO_BITS} bits of addr",
                    m.die.noc.addr_bits
                ),
            );
        }
        Ok(ClusterCfg {
            payload_bits: m.die.noc.data_bits,
            addr_hi_bits: m.die.noc.addr_bits - ADDR_LO_BITS,
            pos_bits: m.die.noc.pos_bits,
            max_fill: m.mg.max_fill,
            max_peers: m.mg.max_peers,
            kstep: m.mg.kstep,
            bank_entries: m.mg.l1_bank_entries,
            anchor: ANCHOR,
        })
    }

    pub fn addr_bits(&self) -> u32 {
        ADDR_LO_BITS + self.addr_hi_bits
    }

    /// Bits of one `{y, x}` coordinate (two positions).
    pub fn coord_bits(&self) -> u32 {
        2 * self.pos_bits
    }
}

/// The four formats, built for one machine.
#[derive(Clone, Debug)]
pub struct ClusterIsa {
    pub cfg: ClusterCfg,
    pub fill: Format,
    pub gemm: Format,
    pub drain: Format,
    pub post: Format,
    pub set: InstSet,
}

const SILICON: &str = "ISA.MG.004";
const RTL: &str = "ISA.MG.005";
const OFFSETS: &str = "ISA.MG.094";

fn tail(cfg: &ClusterCfg) -> Vec<Field> {
    let p = cfg.pos_bits;
    let g = GRID_BITS;
    let f = |name, width, clause| Field::new(name, width, clause).default(0);
    vec![
        f("addr", ADDR_LO_BITS, SILICON),
        f("n", N_BITS, SILICON),
        f("sel", 1, SILICON),
        f("acc", 1, SILICON),
        f("gm", g, SILICON),
        f("gn", g, SILICON),
        f("nk", g, SILICON).default(1),
        f("anchor", g, SILICON).default(cfg.anchor as i64),
        f("peers", cfg.coord_bits() * cfg.max_peers, RTL),
        f("npeer", bits_for(cfg.max_peers as u64 + 1), RTL),
        f("recv", 1, RTL),
        f("eoff", g, RTL),
        f("aoff", g, OFFSETS),
        f("boff", g, OFFSETS),
        f("emit", 1, SILICON),
        f("fuse", 1, SILICON),
        f("abank", 1, SILICON),
        f("bbank", 1, SILICON),
        f("fbank", 1, SILICON),
        f("dnode", 1, RTL),
        f("dst_x", p, RTL),
        f("dst_y", p, RTL),
        f("dbuf", g, RTL),
        f("dflags", g, RTL),
        f("dack_y", p, RTL),
        f("dack_x", p, RTL),
        f("dmesh", 2, RTL),
        f("dfin", g, RTL),
        f("addr_hi", cfg.addr_hi_bits, SILICON),
        f("credit", 1, RTL),
    ]
}

impl ClusterIsa {
    pub fn new(m: &Machine) -> Result<ClusterIsa, Refusal> {
        let mut isa = ClusterIsa::of(ClusterCfg::of(m)?);
        crate::mutant::cluster(&mut isa);
        Ok(isa)
    }

    pub fn of(cfg: ClusterCfg) -> ClusterIsa {
        let format = |name, code| {
            let mut fields = vec![Field::new("op", OP_BITS, "ISA.MG.002").konst(code)];
            fields.extend(tail(&cfg));
            Format::new(name, cfg.payload_bits, SILICON, fields)
        };
        let fill = format("FILL", OP_FILL);
        let gemm = format("GEMM", OP_GEMM);
        let drain = format("DRAIN", OP_DRAIN);
        let post = format("POST", OP_POST);
        let set = InstSet::new(
            "kohakutpu-cu",
            vec![fill.clone(), gemm.clone(), drain.clone(), post.clone()],
        );
        ClusterIsa {
            cfg,
            fill,
            gemm,
            drain,
            post,
            set,
        }
    }

    /// `{"addr": low, "addr_hi": high}` of a `die.noc.addr_bits` address.
    pub fn split_addr(&self, addr: u64) -> Result<[(&'static str, i64); 2], Refusal> {
        let total = self.cfg.addr_bits();
        if addr >> total != 0 {
            return refuse(
                "ISA.MG.097",
                format!("address {addr:#x} does not fit {total} bits"),
            );
        }
        let lo = if crate::mutant::on("isa.cluster/split") {
            32
        } else {
            ADDR_LO_BITS
        };
        Ok([
            ("addr", (addr & mask(lo)) as i64),
            ("addr_hi", (addr >> lo) as i64),
        ])
    }

    /// A coordinate as the ISA's `{y, x}` byte.
    pub fn coord_byte(&self, c: Coord) -> i64 {
        let p = self.cfg.pos_bits;
        let (hi, lo) = if crate::mutant::on("isa.cluster/coord") {
            (c.x(), c.y())
        } else {
            (c.y(), c.x())
        };
        ((hi as u64 & mask(p)) << p | (lo as u64 & mask(p))) as i64
    }

    /// `peers` / `npeer` of up to `mg.max_peers` sharers, peer 0 low.
    pub fn peers(&self, nodes: &[Coord]) -> Result<[(&'static str, i64); 2], Refusal> {
        if nodes.len() > self.cfg.max_peers as usize {
            return refuse(
                "ISA.MG.014",
                format!(
                    "at most {} peers per fill, got {}",
                    self.cfg.max_peers,
                    nodes.len()
                ),
            );
        }
        let w = self.cfg.coord_bits();
        let mut packed = 0i64;
        for (i, n) in nodes.iter().enumerate() {
            packed |= (self.coord_byte(*n) & mask(w) as i64) << (i as u32 * w);
        }
        Ok([("peers", packed), ("npeer", nodes.len() as i64)])
    }

    /// Whether a GEMM may sweep `nk` K blocks: one, or whole issues.
    pub fn legal_nk(&self, nk: u32) -> bool {
        nk == 1 || nk.is_multiple_of(self.cfg.kstep)
    }

    /// `((bit, width, shift), ...)` of field `name`; an address is two.
    pub fn segments(&self, name: &str) -> Vec<Segment> {
        if name == "addr" {
            let (lo, wlo) = self.fill.span("addr");
            let (hi, whi) = self.fill.span("addr_hi");
            return vec![
                Segment {
                    bit: lo,
                    width: wlo,
                    shift: 0,
                },
                Segment {
                    bit: hi,
                    width: whi,
                    shift: wlo,
                },
            ];
        }
        let (bit, width) = self.fill.span(name);
        vec![Segment {
            bit,
            width,
            shift: 0,
        }]
    }

    /// The memory address a cluster word carries: FILL, memory DRAIN and an
    /// emitting GEMM; a node-addressed word carries an L1 offset instead.
    pub fn addresses(&self, word: W256) -> Vec<AddressField> {
        let Some(fmt) = self.set.find(word) else {
            return Vec::new();
        };
        let Ok(f) = fmt.decode(word) else {
            return Vec::new();
        };
        if value(&f, "dnode") != 0 || (fmt.name == "GEMM" && value(&f, "emit") == 0) {
            return Vec::new();
        }
        let addr = value(&f, "addr") as u64 | (value(&f, "addr_hi") as u64) << ADDR_LO_BITS;
        vec![AddressField {
            segments: self.segments("addr"),
            value: addr,
        }]
    }
}

/// `n` entries from byte address `addr` into L1 side `sel` (0 A, 1 B), bank
/// `fbank`, from entry `eoff`. `peers` are up to `mg.max_peers` sharers;
/// `recv` fetches nothing and takes `n` entries landed by COUNT CU_DATA.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Fill {
    pub addr: u64,
    pub n: u32,
    pub sel: u32,
    pub eoff: u32,
    pub fbank: u32,
    pub peers: Vec<Coord>,
    pub recv: bool,
}

/// Sweep `gm` x `gn` sub-tiles over `nk` K blocks. With `emit`, sub-tiles
/// stream to `addr` as they complete: memory, or with `to` that unit's
/// buffer at granule `addr / 32`. Without `emit` no address is carried.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Gemm {
    pub gm: u32,
    pub gn: u32,
    pub nk: u32,
    pub acc: bool,
    pub aoff: u32,
    pub boff: u32,
    pub abank: u32,
    pub bbank: u32,
    pub emit: bool,
    pub addr: u64,
    pub anchor: u32,
    pub to: Option<Drain>,
}

impl Default for Gemm {
    fn default() -> Gemm {
        Gemm {
            gm: 0,
            gn: 0,
            nk: 1,
            acc: false,
            aoff: 0,
            boff: 0,
            abank: 0,
            bbank: 0,
            emit: false,
            addr: 0,
            anchor: ANCHOR,
            to: None,
        }
    }
}

/// `n` sub-tiles to byte address `addr`, or to unit `dst`'s buffer `buf` at
/// granule `addr / 32`. `mesh` / `fin` aim it into another mesh: `fin` is
/// the final coordinate there and `dst` this mesh's memory port.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Drain {
    pub addr: u64,
    pub n: u32,
    pub fuse: bool,
    pub dst: Option<Coord>,
    pub dflags: u32,
    pub ack: Coord,
    pub buf: u32,
    pub mesh: u32,
    pub fin: Option<Coord>,
    /// To a unit: each burst spends its length in credits the sink returns.
    pub credit: bool,
}

/// `n` credits (32-byte granules) to the unit at `dst` once the array has
/// stopped reading L1.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Post {
    pub dst: Coord,
    pub n: u32,
}

/// One cluster instruction.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum ClusterInst {
    Fill(Fill),
    Gemm(Gemm),
    Drain(Drain),
    Post(Post),
}

type Kw = Vec<(&'static str, i64)>;

impl Drain {
    /// The sink fields of a drain to a unit or another mesh.
    pub fn node_fields(&self, isa: &ClusterIsa) -> Kw {
        let mut out = Kw::new();
        if let Some(dst) = self.dst {
            out.extend([
                ("dnode", 1),
                ("dst_x", dst.x() as i64),
                ("dst_y", dst.y() as i64),
                ("dbuf", self.buf as i64),
                ("dflags", self.dflags as i64),
                ("dack_x", self.ack.x() as i64),
                ("dack_y", self.ack.y() as i64),
                ("credit", self.credit as i64),
            ]);
        }
        if let Some(fin) = self.fin {
            out.extend([("dmesh", self.mesh as i64), ("dfin", isa.coord_byte(fin))]);
        }
        out
    }
}

impl ClusterInst {
    /// The instruction's payload.
    pub fn encode(&self, isa: &ClusterIsa) -> Result<W256, Refusal> {
        match self {
            ClusterInst::Fill(f) => {
                if f.n > isa.cfg.max_fill {
                    return refuse(
                        "ISA.MG.018",
                        format!(
                            "FILL of {} entries exceeds the {}-entry streaming count; split it",
                            f.n, isa.cfg.max_fill
                        ),
                    );
                }
                if !crate::mutant::on("rul.048/fill-wraps")
                    && f.eoff as u64 + f.n as u64 > isa.cfg.bank_entries as u64
                {
                    return refuse(
                        "RUL.048",
                        format!(
                            "FILL of {} entries at entry {} passes the {}-entry bank",
                            f.n, f.eoff, isa.cfg.bank_entries
                        ),
                    );
                }
                let mut kw: Kw = vec![
                    ("n", f.n as i64),
                    ("sel", f.sel as i64),
                    ("eoff", f.eoff as i64),
                    ("fbank", f.fbank as i64),
                    ("recv", f.recv as i64),
                ];
                kw.extend(isa.split_addr(f.addr)?);
                if !f.peers.is_empty() {
                    kw.extend(isa.peers(&f.peers)?);
                }
                isa.fill.encode(&kw)
            }
            ClusterInst::Gemm(g) => {
                if !isa.legal_nk(g.nk) {
                    return refuse(
                        "ISA.MG.099",
                        format!(
                            "a GEMM of nk {}: the pumped sweep takes K blocks in issues of {}, \
                             and an odd nk >= 3 corrupts sub-tile (0, 0); use 1 or a multiple",
                            g.nk, isa.cfg.kstep
                        ),
                    );
                }
                let mut kw: Kw = vec![
                    ("gm", g.gm as i64),
                    ("gn", g.gn as i64),
                    ("nk", g.nk as i64),
                    ("acc", g.acc as i64),
                    ("aoff", g.aoff as i64),
                    ("boff", g.boff as i64),
                    ("abank", g.abank as i64),
                    ("bbank", g.bbank as i64),
                    ("anchor", g.anchor as i64),
                ];
                if g.emit {
                    kw.push(("emit", 1));
                    kw.extend(isa.split_addr(g.addr)?);
                    if let Some(to) = &g.to {
                        kw.extend(to.node_fields(isa));
                    }
                }
                isa.gemm.encode(&kw)
            }
            ClusterInst::Drain(d) => {
                let mut kw: Kw = vec![("n", d.n as i64), ("fuse", d.fuse as i64)];
                kw.extend(isa.split_addr(d.addr)?);
                kw.extend(d.node_fields(isa));
                isa.drain.encode(&kw)
            }
            ClusterInst::Post(p) => isa.post.encode(&[
                ("n", p.n as i64),
                ("dst_x", p.dst.x() as i64),
                ("dst_y", p.dst.y() as i64),
            ]),
        }
    }
}
