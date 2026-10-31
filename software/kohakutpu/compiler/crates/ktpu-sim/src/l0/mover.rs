//! The mover's command registers (ISA.MV.010-.018): writes collected into the
//! move a GO starts, every register kept across moves (ISA.MV.098).

use ktpu_isa::unit::mover::*;
use ktpu_isa::Isa;

use crate::units::mover::{Index, Level as SimLevel, Move, Walk as SimWalk};
use crate::R;

/// `bits` of `v` at `lo`, sign-extended.
fn signed(v: u64, lo: u32, bits: u32) -> i64 {
    ((v >> lo) as i64) << (64 - bits) >> (64 - bits)
}

fn field(v: u64, lo: u32, bits: u32) -> u64 {
    (v >> lo) & ((1 << bits) - 1)
}

#[derive(Clone, Default)]
struct Walker {
    base: i64,
    levels: usize,
    dims: [Dim; 6],
    windows: [(i64, i64); 2],
}

#[derive(Clone, Copy, Default)]
struct Dim {
    count: i64,
    stride: i64,
    axis: i64,
    astep: i64,
}

impl Walker {
    fn walk(&self) -> SimWalk {
        SimWalk {
            base: self.base,
            levels: self.dims[..self.levels.max(1)]
                .iter()
                .map(|d| SimLevel {
                    count: d.count,
                    stride: d.stride,
                    axis: d.axis,
                    astep: d.astep,
                })
                .collect(),
            windows: self.windows,
        }
    }
}

#[derive(Default)]
pub struct Regs {
    walkers: [Walker; 2],
    /// The DIM a next AXIS completes: (walker, level).
    staged: (usize, usize),
    xform: (u64, u64),
    imm: u64,
    index: (i64, i64),
    row: (i64, i64),
    seed: u64,
}

impl Regs {
    /// One register write; a GO gives its move.
    pub fn write(&mut self, reg: u64, v: u64, isa: &Isa) -> R<Option<Move>> {
        match reg as u32 {
            R_HDR => {
                let sel = (v & 1) as usize;
                let w = &mut self.walkers[sel];
                w.base = field(v, BASE_LSB, 40) as i64;
                w.levels = field(v, LEVELS_LSB, 3) as usize;
                if sel == 0 {
                    self.xform = (field(v, XID_LSB, 4), field(v, XMODE_LSB, 4));
                }
            }
            R_DIM => {
                let (sel, level) = ((v & 1) as usize, field(v, 1, 3) as usize);
                self.staged = (sel, level);
                let d = &mut self.walkers[sel].dims[level];
                d.count = field(v, 4, 16) as i64;
                d.stride = signed(v, 20, 32);
            }
            R_AXIS => {
                let (sel, level) = self.staged;
                let d = &mut self.walkers[sel].dims[level];
                d.axis = field(v, 0, 2) as i64;
                d.astep = signed(v, 2, 16);
            }
            R_WINDOW => {
                let (sel, axis) = ((v & 1) as usize, field(v, 1, 1) as usize);
                self.walkers[sel].windows[axis] = (signed(v, 2, 16), field(v, 18, 16) as i64);
            }
            R_INDEX => self.index = (field(v, 0, 40) as i64, field(v, 40, 16) as i64),
            R_ROW => self.row = (field(v, 0, 32) as i64, field(v, 32, 16) as i64),
            R_IMM => self.imm = field(v, 0, 32),
            R_SEED => self.seed = v,
            R_CTRL => {
                if v & GO == 0 {
                    return Ok(None);
                }
                return self.go(v, isa).map(Some);
            }
            other => return Err(format!("mover register {other:#x}")),
        }
        Ok(None)
    }

    fn go(&self, ctrl: u64, isa: &Isa) -> R<Move> {
        let mode = isa
            .mover
            .mode(field(ctrl, 0, 3))
            .ok_or("mode 1 faults F_MODE")?;
        let ew = match field(ctrl, 3, 2) as u32 {
            W8 => 8,
            W16 => 16,
            W32 => 32,
            w => return Err(format!("element width {w} faults F_EWIDTH")),
        };
        let (src, dst) = (self.walkers[0].walk(), self.walkers[1].walk());
        let index = Index {
            at: self.index.0,
            n: self.index.1,
            pitch: self.row.0,
            words: self.row.1,
            fp: ctrl & INDEX_FP != 0,
        };
        Ok(match mode {
            MoveMode::Copy => Move::Copy {
                src,
                dst,
                pad: self.imm,
                ew,
            },
            MoveMode::Fill => Move::Fill {
                dst,
                value: self.imm,
                ew,
            },
            MoveMode::Generate => Move::Gen {
                dst,
                seed: self.seed,
            },
            MoveMode::Gather => Move::Gather {
                src: src.base,
                dst,
                index,
            },
            MoveMode::Scatter => Move::Scatter {
                src,
                dst: dst.base,
                index,
            },
            MoveMode::Xform => match self.xform.0 as u32 {
                XF_QUANT => Move::Quant {
                    src,
                    dst,
                    b: self.xform.1 & 1 != 0,
                },
                XF_GT4 => Move::Gt4 { src, dst },
                id => return Err(format!("transform {id} in this simulator")),
            },
            other => return Err(format!("{other:?} in this simulator")),
        })
    }
}
