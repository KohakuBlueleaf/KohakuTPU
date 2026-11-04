//! A vector instance: loads, unpacks, math, reductions and stores on arrays
//! (L2.VC, L2.OP, L2.STAT).

use std::cell::Cell;
use std::collections::HashMap;

use ktpu_isa::unit::vector::core::MathOp;
use ktpu_l1::text::{fmt, Stmt, Term};

use super::node::holds;
use super::view::{view, Layout};
use super::{map, Rows, Sim, CORE_KEY};
use crate::l1::term::int;
use crate::mem::WORD;
use crate::num::{self, mx7};
use crate::units::core::{lane_op, TAU};
use crate::R;

/// Lanes a chunk of a register.
const LANES: usize = 16;
/// An MXFP7 entry's rows and columns.
const MG_LANES: i64 = mx7::LANES as i64;
const MG_BLOCK: i64 = mx7::KBLOCK as i64;

#[derive(Default)]
pub struct Instance {
    values: HashMap<String, Rows>,
    /// An `exp2d` lane reached 2^tau since the last `if sticky`.
    sticky: bool,
}

/// An operand: a value, a statistic broadcast over its rows, or a number.
enum Operand<'a> {
    Value(&'a Rows),
    Number(u32),
}

impl Operand<'_> {
    fn at(&self, r: usize, c: usize) -> u32 {
        match self {
            Operand::Value(v) if v.cols == 1 => v.at(r % v.rows, 0),
            Operand::Value(v) => v.at(r % v.rows, c),
            Operand::Number(n) => *n,
        }
    }
}

fn number(t: &Term) -> Option<f64> {
    match t {
        Term::Float(f) => Some(*f),
        Term::Int(v, _) => Some(*v as f64),
        Term::Neg(a) => number(a).map(|x| -x),
        _ => None,
    }
}

/// The R x C of a value's type; a one-dimensional type is a statistic.
fn shape(st: &Stmt) -> R<(usize, usize)> {
    let t = st.annot.as_ref().ok_or("a value's type")?;
    let ty = match t {
        Term::Placed(x, _) => x.as_ref(),
        other => other,
    };
    let Term::View(_, dims) = ty else {
        return Err(format!("a value's type, got {}", fmt(t)));
    };
    let d: Vec<usize> = dims
        .iter()
        .map(|x| x.as_int().unwrap_or(1) as usize)
        .collect();
    Ok(match d[..] {
        [r] => (r, 1),
        [r, c] => (r, c),
        _ => return Err(format!("a value of {} dimensions", d.len())),
    })
}

impl Instance {
    pub fn block(&mut self, sim: &mut Sim<'_>, body: &[Stmt]) -> R<()> {
        for st in body {
            self.statement(sim, st)
                .map_err(|e| format!("line {}: {e}", st.line))?;
        }
        Ok(())
    }

    fn operand<'a>(&'a self, t: &Term) -> R<Operand<'a>> {
        if let Some(x) = number(t) {
            return Ok(Operand::Number(num::lane(x)));
        }
        let name = match t {
            Term::View(n, _) => n.as_str(),
            Term::Name(n) => n.as_str(),
            other => return Err(format!("an operand, got {}", fmt(other))),
        };
        self.values
            .get(name)
            .map(Operand::Value)
            .ok_or(format!("no value {name}"))
    }

    /// Every operand reads as `rows` x `cols`, a one-column value broadcast.
    fn fits(&self, st: &Stmt, rows: usize, cols: usize) -> R<()> {
        for t in st.positional() {
            if let Ok(Operand::Value(v)) = self.operand(t) {
                if v.cols != 1 && (!rows.is_multiple_of(v.rows) || v.cols < cols) {
                    return Err(format!(
                        "{} {}: {}x{} read as {rows}x{cols}",
                        st.op,
                        fmt(t),
                        v.rows,
                        v.cols
                    ));
                }
            }
        }
        Ok(())
    }

    fn put(&mut self, st: &Stmt, rows: Rows) -> R<()> {
        let name = st.target_name().ok_or("NAME = OP")?.to_string();
        self.values.insert(name, rows);
        Ok(())
    }

    fn statement(&mut self, sim: &mut Sim<'_>, st: &Stmt) -> R<()> {
        let pos = st.positional();
        match st.op.as_str() {
            "for" => {
                let Some(Term::Member(var, dom)) = pos.first() else {
                    return Err("VAR in LO..HI".into());
                };
                let Term::Span(lo, hi) = dom.as_ref() else {
                    return Err("VAR in LO..HI".into());
                };
                let var = var.as_name().ok_or("VAR")?.to_string();
                for i in int(lo, &sim.env)?..int(hi, &sim.env)? {
                    sim.env.insert(var.clone(), i);
                    self.block(sim, &st.body)?;
                }
                sim.env.remove(&var);
                Ok(())
            }
            "when" => match holds(pos[0], &sim.env)? {
                true => self.block(sim, &st.body),
                false => Ok(()),
            },
            "if" => {
                if pos[0].as_name() != Some("sticky") {
                    return Err("if sticky".into());
                }
                match std::mem::take(&mut self.sticky) {
                    true => self.block(sim, &st.body),
                    false => Ok(()),
                }
            }
            "recv" => {
                let (rows, cols) = shape(st)?;
                let key = sim.key(sim.node, CORE_KEY + sim.env_core()?);
                let got = sim.received(key, rows)?;
                if got.cols != cols {
                    return Err(format!("a recv of {} columns into {cols}", got.cols));
                }
                self.put(st, got)
            }
            "send" => {
                let Operand::Value(q) = self.operand(pos[0])? else {
                    return Err("send VALUE -> mg[I]".into());
                };
                let key = sim.key(sim.node, sim.unit(pos[2])?);
                sim.links.entry(key).or_default().push_back(q.clone());
                Ok(())
            }
            "load" => self.load(sim, st),
            "unpack" => self.unpack(sim, st),
            "pack" => {
                let (r, c) = shape(st)?;
                self.fits(st, r, c)?;
                let from = self.operand(pos[0])?;
                let v: Vec<u32> = (0..r * c)
                    .map(|i| u32::from(num::to_f16(from.at(i / c, i % c))))
                    .collect();
                // A checked pack (`status=`) refuses a non-finite f16 value.
                if st.kw("status").is_some() && v.iter().any(|x| x & 0x7c00 == 0x7c00) {
                    return Err("a checked pack of a non-finite value".into());
                }
                self.put(
                    st,
                    Rows {
                        rows: r,
                        cols: c,
                        v,
                    },
                )
            }
            "store" => self.store(sim, st),
            "rsum" | "rmax" => self.reduce(st),
            "xlane" | "xrot" => self.shuffle(st),
            _ => self.math(st),
        }
    }

    fn load(&mut self, sim: &mut Sim<'_>, st: &Stmt) -> R<()> {
        let (rows, cols) = shape(st)?;
        let v = sim.view(st.positional()[0])?;
        let b = sim.buf(&v.name)?.clone();
        let mut out = Vec::with_capacity(rows * cols);
        let code = |sim: &Sim<'_>, r: i64, c: i64| -> R<u32> {
            let bytes = sim.mem.read(b.element(r, c)?, 2)?;
            Ok(u32::from(u16::from_le_bytes([bytes[0], bytes[1]])))
        };
        if st.kw("read_map").is_some() {
            let elements = map::read(st, &sim.env)?;
            let kw = |k: &str, d: i64| st.kw(k).map_or(Ok(d), |t| int(t, &sim.env));
            let first = kw("read_at", 0)?;
            let pitch = kw("read_pitch", cols as i64)?;
            let stride = kw("read_stride", 1)?;
            let valid = kw("read_rows", rows as i64)?;
            let columns = kw("read_cols", cols as i64)?;
            let source_pitch = *b.shape.last().unwrap_or(&1);
            for r in 0..rows as i64 {
                for c in 0..cols as i64 {
                    let logical = first + r * pitch + c * stride;
                    let at = if r < valid && c < columns {
                        elements[logical as usize]
                    } else {
                        -1
                    };
                    out.push(if at < 0 {
                        0
                    } else {
                        code(sim, at / source_pitch, at % source_pitch)?
                    });
                }
            }
        } else {
            let (r0, vr, c0, vc) = v.rect();
            if (cols == 1 || vc as usize != cols) && (vr * vc) as usize == rows * cols {
                // The view's elements in row-major order (L2.VC.003): a
                // [1, 256] view is a [2, 128] value's two rows.
                for i in 0..vr * vc {
                    out.push(code(sim, r0 + i / vc, c0 + i % vc)?);
                }
            } else {
                // A rank-1 view is one row, every row of the value (L2.VC.003's band).
                if vr as usize * (rows / vr as usize) != rows || vc as usize != cols {
                    return Err(format!("a view of {vr}x{vc} into {rows}x{cols}"));
                }
                for i in 0..rows as i64 * vc {
                    out.push(code(sim, r0 + (i / vc) % vr, c0 + i % vc)?);
                }
            }
        }
        self.put(st, Rows { rows, cols, v: out })
    }

    /// A value's rows `[lo, lo + n)`, as `x[lo : +n, :]` names them.
    fn window(&self, sim: &Sim<'_>, t: &Term) -> R<Option<Rows>> {
        let Term::View(name, slices) = t else {
            return Ok(None);
        };
        if slices.is_empty() {
            return Ok(None);
        }
        let Some(x) = self.values.get(name) else {
            return Ok(None);
        };
        let shape = |_: &str| Ok(vec![x.rows as i64, x.cols as i64]);
        let (r0, rows, c0, cols) = view(t, shape, &sim.env)?.rect();
        let (r0, rows, c0, cols) = (r0 as usize, rows as usize, c0 as usize, cols as usize);
        let v = (0..rows * cols)
            .map(|i| x.at(r0 + i / cols, c0 + i % cols))
            .collect();
        Ok(Some(Rows { rows, cols, v }))
    }

    fn unpack(&mut self, sim: &Sim<'_>, st: &Stmt) -> R<()> {
        let (rows, cols) = shape(st)?;
        if let Some(part) = self.window(sim, st.positional()[0])? {
            let v = part.v.iter().map(|c| num::from_f16(*c as u16)).collect();
            return self.put(st, Rows { rows, cols, v });
        }
        if st.kw("lane").is_none() {
            if let Ok(Operand::Value(x)) = self.operand(st.positional()[0]) {
                if x.cols != cols && x.rows * x.cols == rows * cols {
                    // The same row-major values in the register shape: an
                    // L1 [2, 128] value unpacked as one [1, 256] row.
                    let v = x.v.iter().map(|c| num::from_f16(*c as u16)).collect();
                    return self.put(st, Rows { rows, cols, v });
                }
            }
            self.fits(st, rows, cols)?;
        }
        let from = self.operand(st.positional()[0])?;
        let v: Vec<u32> = match st.kw("lane") {
            Some(t) => {
                let l = number(t).ok_or("lane=L")? as usize;
                vec![num::from_f16(from.at(0, l) as u16); rows * cols]
            }
            None => (0..rows * cols)
                .map(|i| num::from_f16(from.at(i / cols, i % cols) as u16))
                .collect(),
        };
        self.put(st, Rows { rows, cols, v })
    }

    fn store(&mut self, sim: &mut Sim<'_>, st: &Stmt) -> R<()> {
        let pos = st.positional();
        let v = sim.view(pos[0])?;
        let b = sim.buf(&v.name)?.clone();
        let (r0, vr, c0, vc) = v.rect();
        let from = self.operand(pos[2])?;
        if let Layout::Mx7 { b: side, .. } = b.layout {
            for g in 0..vr / MG_LANES {
                for k in 0..vc / MG_BLOCK {
                    let mut x = [[0u16; mx7::KBLOCK]; mx7::LANES];
                    for (l, row) in x.iter_mut().enumerate() {
                        for (e, code) in row.iter_mut().enumerate() {
                            let r = g as usize * mx7::LANES + l;
                            *code = from.at(r, k as usize * mx7::KBLOCK + e) as u16;
                        }
                    }
                    let at = b.entry(r0 / MG_LANES + g, c0 / MG_BLOCK + k)?;
                    for (i, word) in mx7::entry(&x).write(side).iter().enumerate() {
                        sim.mem.set_word(at + (i * WORD) as i64, word)?;
                    }
                }
            }
            return Ok(());
        }
        let statistic = matches!(from, Operand::Value(x) if x.cols == 1);
        for i in 0..vr * vc {
            let (r, c) = if statistic {
                (i as usize, 0)
            } else {
                ((i / vc) as usize, (i % vc) as usize)
            };
            let code = from.at(r, c) as u16;
            sim.mem
                .write(b.element(r0 + i / vc, c0 + i % vc)?, &code.to_le_bytes())?;
        }
        Ok(())
    }

    /// `rsum` exactly, rounded once; the lowered tree stays within its ulps.
    fn reduce(&mut self, st: &Stmt) -> R<()> {
        let (rows, _) = shape(st)?;
        let t = st.positional()[0];
        let from = self.operand(t)?;
        let Operand::Value(x) = from else {
            return Err(format!("a reduction of {}", fmt(t)));
        };
        if x.rows < rows {
            return Err(format!("{} of {} rows into {rows}", st.op, x.rows));
        }
        let v = (0..rows)
            .map(|r| {
                let row = (0..x.cols).map(|c| x.at(r, c));
                if st.op == "rsum" {
                    num::lane(row.map(num::value).sum())
                } else {
                    row.reduce(|a, b| lane_op(MathOp::Vmax, a, b, 0))
                        .unwrap_or(0)
                }
            })
            .collect();
        self.put(st, Rows { rows, cols: 1, v })
    }

    fn shuffle(&mut self, st: &Stmt) -> R<()> {
        let (rows, cols) = shape(st)?;
        self.fits(st, rows, cols)?;
        let pos = st.positional();
        let from = self.operand(pos[0])?;
        let k = number(pos[1]).ok_or("OP X, K")? as usize;
        let mut v = Vec::with_capacity(rows * cols);
        for r in 0..rows {
            for c in 0..cols {
                let l = c % LANES;
                let lane = if st.op == "xlane" {
                    l ^ k
                } else {
                    (l + k) % LANES
                };
                v.push(from.at(r, c - l + lane));
            }
        }
        self.put(st, Rows { rows, cols, v })
    }

    fn math(&mut self, st: &Stmt) -> R<()> {
        let (rows, cols) = shape(st)?;
        self.fits(st, rows, cols)?;
        let pos = st.positional();
        let ops: Vec<Operand<'_>> = pos.iter().map(|t| self.operand(t)).collect::<R<_>>()?;
        let zero = Operand::Number(0);
        let arg = |i: usize| ops.get(i).unwrap_or(&zero);
        let sticky = Cell::new(false);
        let lane = |r: usize, c: usize| -> R<u32> {
            let (a, b, x) = (arg(0).at(r, c), arg(1).at(r, c), arg(2).at(r, c));
            Ok(match st.op.as_str() {
                "exp2d" => {
                    let (v, s) = num::exp2d(a, b, TAU);
                    sticky.set(sticky.get() | s);
                    v
                }
                "mov" => lane_op(MathOp::Vmov, a, 0, 0),
                "neg" => lane_op(MathOp::Vneg, a, 0, 0),
                "abs" => lane_op(MathOp::Vabs, a, 0, 0),
                "add" => lane_op(MathOp::Vadd, a, 0, b),
                "sub" => lane_op(MathOp::Vsub, a, 0, b),
                "mul" => lane_op(MathOp::Vmul, a, b, 0),
                "fma" => lane_op(MathOp::Vfma, a, b, x),
                "fnma" => lane_op(MathOp::Vfnma, a, b, x),
                "max" => lane_op(MathOp::Vmax, a, b, 0),
                "min" => lane_op(MathOp::Vmin, a, b, 0),
                "sel" => lane_op(MathOp::Vsel, b, x, a),
                "exp2" => lane_op(MathOp::Vexp2, a, 0, 0),
                "log2" => lane_op(MathOp::Vlog2, a, 0, 0),
                "inv" => lane_op(MathOp::Vinv, a, 0, 0),
                "rsqrt" => lane_op(MathOp::Vrsqrt, a, 0, 0),
                "cmplt" => {
                    if num::lt(a, b) {
                        num::ONE
                    } else {
                        0
                    }
                }
                "cmpgt" => {
                    if num::gt(a, b) {
                        num::ONE
                    } else {
                        0
                    }
                }
                "cmpeq" => {
                    if num::eq(a, b) {
                        num::ONE
                    } else {
                        0
                    }
                }
                op => return Err(format!("no vector op {op} in this simulator")),
            })
        };
        let mut v = Vec::with_capacity(rows * cols);
        for r in 0..rows {
            for c in 0..cols {
                v.push(lane(r, c)?);
            }
        }
        drop(ops);
        self.sticky |= sticky.get();
        self.put(st, Rows { rows, cols, v })
    }
}
