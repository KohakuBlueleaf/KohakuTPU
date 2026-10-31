//! The Streaming Row Unit (`SR`): CFG, LOAD, RUN, STORE, POST, one 256-bit
//! payload each (`docs/spec/stack/isa/sru.md`, RTL `sru_cu.v`), and its
//! configuration map ([`config`]).

pub mod config;

use crate::layout::{value, Field, Format, InstSet, W256};
use crate::reloc::{AddressField, Segment};
use ktpu_machine::{Coord, Machine, Refusal};
use serde::{Deserialize, Serialize};

pub use config::{e8, pack, schedule, words, Job, OpValue, Pool, PoolOp, Schedule};

pub const OP_CFG: u64 = 1;
pub const OP_LOAD: u64 = 2;
pub const OP_RUN: u64 = 3;
pub const OP_STORE: u64 = 4;
pub const OP_POST: u64 = 5;

/// LOAD targets: the function table (4 entries a word), column constants
/// (2 a word), row state (a row a word).
pub const LOAD_TABLE: u32 = 0;
pub const LOAD_COLS: u32 = 1;
pub const LOAD_ROWS: u32 = 2;

const OP_BITS: u32 = 4;
/// Memory addresses in SRU words are this wide (`addr`, `aaddr`, ...).
pub const MEM_ADDR_BITS: u32 = 40;
/// Bits of a POST's credit count `n` (ISA.SR.014).
pub const POST_N_BITS: u32 = 16;

/// The five formats, built for one machine's payload width.
#[derive(Clone, Debug)]
pub struct SruIsa {
    pub cfg: Format,
    pub load: Format,
    pub run: Format,
    pub store: Format,
    pub post: Format,
    pub set: InstSet,
}

impl SruIsa {
    pub fn new(m: &Machine) -> SruIsa {
        let mut isa = SruIsa::of(m.die.noc.data_bits);
        crate::mutant::sru(&mut isa);
        isa
    }

    pub fn of(payload_bits: u32) -> SruIsa {
        let op = |code, clause| Field::new("op", OP_BITS, clause).konst(code);
        let pad = |width, clause| Field::new("_pad", width, clause).default(0);
        let c = "ISA.SR.010";
        let cfg = Format::new(
            "CFG",
            payload_bits,
            c,
            vec![
                op(OP_CFG, c),
                Field::new("word", config::ADDR_BITS, c),
                pad(payload_bits - OP_BITS - config::ADDR_BITS - 32, c),
                Field::new("value", 32, c),
            ],
        );
        let c = "ISA.SR.011";
        let load = Format::new(
            "LOAD",
            payload_bits,
            c,
            vec![
                op(OP_LOAD, c),
                Field::new("what", 2, c),
                Field::new("first", 10, c).default(0),
                Field::new("addr", MEM_ADDR_BITS, c),
                Field::new("words", 12, c),
            ],
        );
        let c = "ISA.SR.012";
        let f = |name, width| Field::new(name, width, "ISA.SR.012").default(0);
        let run = Format::new(
            "RUN",
            payload_bits,
            c,
            vec![
                op(OP_RUN, c),
                f("amem", 1).default(1),
                f("peer", 1),
                f("aaddr", MEM_ADDR_BITS),
                f("astride", 16),
                f("oaddr", MEM_ADDR_BITS),
                f("ostride", 16),
                f("baddr", MEM_ADDR_BITS),
                f("bstride", 16),
                f("px", 4),
                f("py", 4),
                f("pbuf", 4),
                f("poff", 16),
                f("count", 1),
                f("ax", 4),
                f("ay", 4),
                f("words", 24),
            ],
        );
        let c = "ISA.SR.013";
        let store = Format::new(
            "STORE",
            payload_bits,
            c,
            vec![
                op(OP_STORE, c),
                pad(3, c),
                Field::new("first", 9, c).default(0),
                Field::new("addr", MEM_ADDR_BITS, c),
                Field::new("rows", 12, c),
            ],
        );
        let c = "ISA.SR.014";
        let post = Format::new(
            "POST",
            payload_bits,
            c,
            vec![
                op(OP_POST, c),
                Field::new("x", 4, c),
                Field::new("y", 4, c),
                pad(payload_bits - OP_BITS - 8 - POST_N_BITS, c),
                Field::new("n", POST_N_BITS, c),
            ],
        );
        let set = InstSet::new(
            "kohakutpu-sru",
            vec![
                cfg.clone(),
                load.clone(),
                run.clone(),
                store.clone(),
                post.clone(),
            ],
        );
        SruIsa {
            cfg,
            load,
            run,
            store,
            post,
            set,
        }
    }

    /// The memory addresses an SRU word carries: LOAD and STORE `addr`; RUN
    /// `aaddr` when A is from memory, `oaddr` when the output goes to memory,
    /// `baddr` when set.
    pub fn addresses(&self, word: W256) -> Vec<AddressField> {
        let Some(fmt) = self.set.find(word) else {
            return Vec::new();
        };
        let Ok(f) = fmt.decode(word) else {
            return Vec::new();
        };
        let names: Vec<&str> = match fmt.name {
            "LOAD" | "STORE" => vec!["addr"],
            "RUN" => [
                ("aaddr", value(&f, "amem") != 0),
                ("oaddr", value(&f, "peer") == 0),
                ("baddr", value(&f, "baddr") != 0),
            ]
            .into_iter()
            .filter(|(_, on)| *on)
            .map(|(n, _)| n)
            .collect(),
            _ => Vec::new(),
        };
        names
            .into_iter()
            .map(|n| {
                let (bit, width) = fmt.span(n);
                AddressField {
                    segments: vec![Segment {
                        bit,
                        width,
                        shift: 0,
                    }],
                    value: value(&f, n) as u64,
                }
            })
            .collect()
    }
}

/// One SRU instruction (CFG words come as a [`SruInst::Config`] of several).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum SruInst {
    /// CFG words `(address, value)` in the order written.
    Config { words: Vec<(u32, u32)> },
    /// `words` 32-byte words from `addr` into target `what` from entry `first`.
    Load {
        what: u32,
        addr: u64,
        words: u32,
        #[serde(default)]
        first: u32,
    },
    /// One job over the configuration the unit holds. A from memory at `a`
    /// or, with `a` None, a COUNT stream whose producer `src` gets the
    /// credits; out to memory at `out` or to the unit `to` (buffer `buf`,
    /// granule `at`, flagged COUNT with `count`).
    Run {
        a: Option<u64>,
        astride: u32,
        #[serde(default)]
        out: u64,
        #[serde(default)]
        ostride: u32,
        #[serde(default)]
        b: u64,
        #[serde(default)]
        bstride: u32,
        #[serde(default)]
        src: Coord,
        #[serde(default)]
        to: Option<Coord>,
        #[serde(default)]
        buf: u32,
        #[serde(default)]
        at: u32,
        #[serde(default)]
        count: bool,
        /// A stream's words (NW x NSR): the credits its producer gets.
        #[serde(default)]
        words: u32,
    },
    /// Row-state rows `first`.. (`rows` of them) to `addr`, a row a word.
    Store {
        addr: u64,
        rows: u32,
        #[serde(default)]
        first: u32,
    },
    /// `n` credits (32-byte granules) to the unit at `dst`.
    Post { dst: Coord, n: u32 },
}

impl SruInst {
    pub fn encode(&self, isa: &SruIsa) -> Result<Vec<W256>, Refusal> {
        match self {
            SruInst::Config { words } => words
                .iter()
                .map(|(a, v)| isa.cfg.encode(&[("word", *a as i64), ("value", *v as i64)]))
                .collect(),
            SruInst::Load {
                what,
                addr,
                words,
                first,
            } => Ok(vec![isa.load.encode(&[
                ("what", *what as i64),
                ("first", *first as i64),
                ("addr", *addr as i64),
                ("words", *words as i64),
            ])?]),
            SruInst::Run {
                a,
                astride,
                out,
                ostride,
                b,
                bstride,
                src,
                to,
                buf,
                at,
                count,
                words,
            } => {
                let peer = to.is_some();
                let to = to.unwrap_or_default();
                Ok(vec![isa.run.encode(&[
                    ("amem", a.is_some() as i64),
                    ("peer", peer as i64),
                    ("aaddr", a.unwrap_or(0) as i64),
                    ("astride", *astride as i64),
                    ("oaddr", if peer { 0 } else { *out as i64 }),
                    ("ostride", *ostride as i64),
                    ("baddr", *b as i64),
                    ("bstride", *bstride as i64),
                    ("px", to.x() as i64),
                    ("py", to.y() as i64),
                    ("pbuf", *buf as i64),
                    ("poff", *at as i64),
                    ("count", *count as i64),
                    ("ax", src.x() as i64),
                    ("ay", src.y() as i64),
                    ("words", if a.is_none() { *words as i64 } else { 0 }),
                ])?])
            }
            SruInst::Store { addr, rows, first } => Ok(vec![isa.store.encode(&[
                ("first", *first as i64),
                ("addr", *addr as i64),
                ("rows", *rows as i64),
            ])?]),
            SruInst::Post { dst, n } => Ok(vec![isa.post.encode(&[
                ("x", dst.x() as i64),
                ("y", dst.y() as i64),
                ("n", *n as i64),
            ])?]),
        }
    }
}
