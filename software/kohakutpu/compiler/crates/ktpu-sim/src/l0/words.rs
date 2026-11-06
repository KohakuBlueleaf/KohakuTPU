//! A unit's 256-bit words read by the ISA's formats.

use ktpu_isa::unit::vector::VecCu;
use ktpu_isa::{Isa, W256};

use crate::machine::VcWord;
use crate::units::cluster::{Output, Word};
use crate::units::link::{Place, Target};
use crate::R;

const ADDR_LO_BITS: u32 = 34;
const WORD_BYTES: i64 = 32;
/// A burst's flags[1] (ISA.VC.182).
const FLAG_COUNT: i64 = 2;

pub fn vector(isa: &Isa, w: W256) -> R<VcWord> {
    match isa.vector.decode_cu(w) {
        Some(VecCu::Imem { addr, word }) => Ok(VcWord::Imem(addr as usize, word)),
        Some(VecCu::Desc { ad, fld, value }) => Ok(VcWord::DescField(ad, fld, value)),
        Some(VecCu::Run { pc }) => Ok(VcWord::RunAt(pc as usize)),
        None => Err("no vector CU word".into()),
    }
}

pub fn cluster(isa: &Isa, w: W256) -> R<Word> {
    let (name, f) = isa
        .cluster
        .set
        .decode(w)
        .map_err(|e| format!("no cluster word: {e:?}"))?;
    let g = |n: &str| f.iter().find(|(k, _)| *k == n).map_or(0, |(_, v)| *v);
    let addr = g("addr") | g("addr_hi") << ADDR_LO_BITS;
    let toward_unit = g("dnode") != 0;
    let remote = g("dfin") != 0;
    let to = Target {
        mesh: remote.then_some(g("dmesh") as usize),
        place: if remote {
            Place::At((g("dfin") & 0xF) as u32, (g("dfin") >> 4) as u32)
        } else {
            Place::At(g("dst_x") as u32, g("dst_y") as u32)
        },
    };
    let output = |n| Output {
        to,
        buf: g("dbuf") as u32,
        at: (addr / WORD_BYTES) as usize,
        n,
        count: g("dflags") & FLAG_COUNT != 0,
        credit: g("credit") != 0,
    };
    Ok(match name {
        "FILL" if g("recv") != 0 => Word::FillRecv {
            b: g("sel") != 0,
            n: g("n") as usize,
        },
        "FILL" => Word::Fill {
            b: g("sel") != 0,
            bank: g("fbank") as usize,
            eoff: g("eoff") as usize,
            addr,
            n: g("n") as usize,
        },
        "GEMM" => {
            let sweep = Word::Gemm {
                gm: g("gm") as usize,
                gn: g("gn") as usize,
                nk: g("nk") as usize,
                abank: g("abank") as usize,
                bbank: g("bbank") as usize,
                aoff: g("aoff") as usize,
                boff: g("boff") as usize,
                acc: g("acc") != 0,
                anchor: g("anchor"),
                emit: (g("emit") != 0 && !toward_unit).then_some(addr),
            };
            if g("emit") != 0 && toward_unit {
                Word::GemmTo {
                    sweep: Box::new(sweep),
                    output: output(g("gm") as usize * g("gn") as usize),
                }
            } else {
                sweep
            }
        }
        // ISA.MG.030: collection never sends the results a second time.
        "DRAIN" if g("fuse") != 0 => Word::Fused,
        "DRAIN" if toward_unit && g("credit") != 0 => Word::DrainCreditTo {
            output: output(g("n") as usize),
        },
        "DRAIN" if toward_unit => Word::DrainTo {
            to,
            buf: g("dbuf") as u32,
            at: (addr / WORD_BYTES) as usize,
            n: g("n") as usize,
            count: g("dflags") & FLAG_COUNT != 0,
        },
        "DRAIN" => Word::Drain {
            addr,
            n: g("n") as usize,
        },
        other => return Err(format!("{other} in this simulator")),
    })
}
