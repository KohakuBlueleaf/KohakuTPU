//! Node scope: control, buffers and the mover ops (L2.BODY, L2.MOV).

use std::rc::Rc;

use ktpu_l1::text::{fmt, Stmt, Term};

use super::cluster::mg_block;
use super::math::Instance;
use super::view::{walk_term, Buf, Layout, View, WORD};
use super::Sim;
use crate::l1::term::int;
use crate::num::mx7;
use crate::units::cluster::BUF_ACC;
use crate::units::mover::{self, Index, Level, Move, Walk};
use crate::R;

/// `VAR in LO..HI`.
fn range(st: &Stmt, env: &crate::l1::term::Env) -> R<(String, i64, i64)> {
    let pos = st.positional();
    let Some(Term::Member(var, dom)) = pos.first() else {
        return Err("VAR in LO..HI".into());
    };
    let Term::Span(lo, hi) = dom.as_ref() else {
        return Err("VAR in LO..HI".into());
    };
    let name = var.as_name().ok_or("VAR in LO..HI")?.to_string();
    Ok((name, int(lo, env)?, int(hi, env)?))
}

/// A `when`'s condition.
pub fn holds(t: &Term, env: &crate::l1::term::Env) -> R<bool> {
    let Term::Compare(op, a, b) = t else {
        return Ok(int(t, env)? != 0);
    };
    let (a, b) = (int(a, env)?, int(b, env)?);
    Ok(match op.as_str() {
        "<" => a < b,
        "<=" => a <= b,
        ">" => a > b,
        ">=" => a >= b,
        "==" => a == b,
        "!=" => a != b,
        _ => return Err(format!("a condition {op}")),
    })
}

/// Whether a block drains one accumulator into another.
fn adds(body: &[Stmt]) -> bool {
    body.iter().any(|s| {
        (s.op == "drain" && s.positional().iter().any(|p| p.as_name() == Some("add")))
            || adds(&s.body)
    })
}

fn whole(b: &Buf) -> Walk {
    let level = Level {
        count: b.bytes() / WORD,
        stride: WORD,
        axis: 0,
        astep: 0,
    };
    Walk {
        base: b.at,
        levels: vec![level],
        windows: [(0, 0); 2],
    }
}

impl Sim<'_> {
    pub(super) fn block(&mut self, stmts: &[Stmt]) -> R<()> {
        for st in stmts {
            self.statement(st)
                .map_err(|e| format!("line {}: {e}", st.line))?;
        }
        Ok(())
    }

    fn statement(&mut self, st: &Stmt) -> R<()> {
        let pos = st.positional();
        match st.op.as_str() {
            "buffer" => {
                let name = st.target_name().ok_or("NAME = buffer")?.to_string();
                let ty = st.annot.as_ref().ok_or("a buffer's type")?;
                let b = match self.placed.get(&name) {
                    Some(at) => Buf::of(ty, *at)?,
                    None => {
                        let b = Buf::of(ty, self.mem.bytes.len() as i64)?;
                        let end = b.at + b.bytes() + WORD;
                        self.mem
                            .bytes
                            .resize((end + WORD - 1) as usize / WORD as usize * WORD as usize, 0);
                        b
                    }
                };
                self.bufs.insert(name, b);
                Ok(())
            }
            "for" => {
                let (var, lo, hi) = range(st, &self.env)?;
                for i in lo..hi {
                    self.env.insert(var.clone(), i);
                    self.block(&st.body)?;
                }
                self.env.remove(&var);
                Ok(())
            }
            "when" => match holds(pos[0], &self.env)? {
                true => self.block(&st.body),
                false => Ok(()),
            },
            "par" => self.par(st),
            "copy" => self.copy(st),
            "gemm_tiles" => super::cluster::tiles(self, st),
            "fill" => self.fill(st),
            "gen" => {
                let v = self.view(pos[0])?;
                let seed = int(st.kw("seed").ok_or("gen seed=")?, &self.env)? as u64;
                let dst = self.walk_of(&v)?;
                mover::run(&Move::Gen { dst, seed }, self.mem)
            }
            "gather" | "scatter" => self.indexed(st),
            "gt4" => self.gt4(st),
            "quant" => self.quant(st),
            // A checked quant refuses non-finite input where it reads it.
            "quant_check" => Ok(()),
            "tile_transpose" => self.tile_transpose(st),
            op => Err(format!("no node op {op} in this simulator")),
        }
    }

    fn par(&mut self, st: &Stmt) -> R<()> {
        let (var, lo, hi) = range(st, &self.env)?;
        let on = st.positional().last().copied().ok_or("a par's unit")?;
        let (unit, any) = match on {
            Term::View(n, s) => (
                n.clone(),
                s.first().and_then(|x| x.as_name()) == Some("any"),
            ),
            other => return Err(format!("a par's unit, got {}", fmt(other))),
        };
        let mut order: Vec<i64> = (lo..hi).collect();
        if unit != "node" || !adds(&st.body) {
            for i in (1..order.len()).rev() {
                order.swap(i, self.rng.pick(i + 1));
            }
        }
        let outer = !self.deferring && adds(&st.body);
        self.deferring |= outer;
        if unit == "node" {
            // Nodes run a stage's statement together (L2.BODY.019).
            for s in &st.body {
                for i in &order {
                    self.env.insert(var.clone(), *i);
                    self.node = self.unit(on)?;
                    self.statement(s)?;
                }
                if outer {
                    self.settle()?;
                }
            }
            self.node = 0;
            self.deferring = false;
            self.env.remove(&var);
            return Ok(());
        }
        for i in order {
            self.env.insert(var.clone(), i);
            let at = if any { i } else { self.unit(on)? };
            match unit.as_str() {
                "vc" => {
                    self.core = Some(at);
                    Instance::default().block(self, &st.body)?;
                    self.core = None;
                }
                "mg" => mg_block(self, at, &st.body)?,
                u => return Err(format!("a par on {u} in this simulator")),
            }
        }
        if outer {
            self.settle()?;
            self.deferring = false;
        }
        self.env.remove(&var);
        Ok(())
    }

    /// The accumulator adds of a stage, then the drains that read their sum.
    fn settle(&mut self) -> R<()> {
        let mut pending = std::mem::take(&mut self.adds);
        while !pending.is_empty() {
            let mut ready = Vec::new();
            let mut later = Vec::new();
            for &(src, dst, tiles) in &pending {
                if pending.iter().any(|&(_, target, _)| target == src) {
                    later.push((src, dst, tiles));
                } else {
                    ready.push((src, dst, tiles));
                }
            }
            if ready.is_empty() {
                return Err("cyclic accumulator reduction".into());
            }
            for (src, dst, tiles) in ready {
                let data = self.clusters.entry(src).or_default().out(BUF_ACC, tiles);
                self.clusters
                    .entry(dst)
                    .or_default()
                    .land(BUF_ACC, 0, &data, false)?;
            }
            pending = later;
        }
        for (key, word) in std::mem::take(&mut self.drains) {
            self.clusters.entry(key).or_default().run(&word, self.mem)?;
        }
        Ok(())
    }

    fn walk_of(&self, v: &View) -> R<Walk> {
        v.walk(self.buf(&v.name)?)
    }

    /// The walk a side names: its `walk=` from the buffer, or its view.
    fn side(&self, t: &Term, walk: Option<&Term>, at: Option<&Term>) -> R<Walk> {
        let v = self.view(t)?;
        match walk {
            Some(w) => {
                let off = at.map_or(Ok(0), |x| int(x, &self.env))?;
                walk_term(w, self.buf(&v.name)?.at + off, &self.env)
            }
            None => self.walk_of(&v),
        }
    }

    fn copy_elements(&mut self, to: &View, from: &View) -> R<()> {
        let (a, b) = (self.buf(&to.name)?.clone(), self.buf(&from.name)?.clone());
        let (r0, rows, c0, cols) = to.rect();
        let (s0, srows, t0, scols) = from.rect();
        if rows * cols != srows * scols {
            return Err(format!("a copy of {} into {}", srows * scols, rows * cols));
        }
        for i in 0..rows * cols {
            let (r, c) = (i / cols, i % cols);
            let (sr, sc) = (i / scols, i % scols);
            let bytes = self.mem.read(b.element(s0 + sr, t0 + sc)?, 2)?.to_vec();
            self.mem.write(a.element(r0 + r, c0 + c)?, &bytes)?;
        }
        Ok(())
    }

    /// `tile_transpose Y <- X mask=M nb= a= b=`: f16 `[nb, a, b]` to
    /// `[nb, b, a]`. A set lane of M row k takes lane `l ^ (5 << k)` of each
    /// 4 x 4 block before the blocks exchange; the real table transposes them.
    fn tile_transpose(&mut self, st: &Stmt) -> R<()> {
        let pos = st.positional();
        let (to, from) = (self.view(pos[0])?, self.view(pos[2])?);
        let mask = self.view(st.kw("mask").ok_or("tile_transpose mask=")?)?;
        let (y0, x0) = (self.walk_of(&to)?.base, self.walk_of(&from)?.base);
        let m0 = self.walk_of(&mask)?.base;
        let arg = |k: &str| int(st.kw(k).ok_or("tile_transpose nb= a= b=")?, &self.env);
        let (nb, a, b) = (arg("nb")?, arg("a")?, arg("b")?);
        let sp = st
            .kw("source_pitch")
            .map_or(Ok(2 * b), |t| int(t, &self.env))?;
        let dp = st
            .kw("destination_pitch")
            .map_or(Ok(2 * a), |t| int(t, &self.env))?;
        let mut set = [[false; 128]; 2];
        for (k, row) in set.iter_mut().enumerate() {
            for (i, lane) in row.iter_mut().enumerate() {
                let bits = self.mem.read(m0 + 2 * (128 * k as i64 + i as i64), 2)?;
                *lane = u16::from_le_bytes([bits[0], bits[1]]) & 0x7fff != 0;
            }
        }
        for n in 0..nb {
            for ri in (0..a).step_by(32) {
                for ci in (0..b).step_by(32) {
                    for i in 0..8 {
                        for j in 0..8 {
                            for l in 0..16i64 {
                                let at = (16 * j + l) as usize;
                                let l1 = if set[1][at] { l ^ 10 } else { l };
                                let l0 = if set[0][(16 * j + l1) as usize] {
                                    l1 ^ 5
                                } else {
                                    l1
                                };
                                let (r, c) = (ri + 4 * i + (l0 >> 2), ci + 4 * j + (l0 & 3));
                                let bytes =
                                    self.mem.read(x0 + (n * a + r) * sp + 2 * c, 2)?.to_vec();
                                let (ro, co) = (ci + 4 * j + (l >> 2), ri + 4 * i + (l & 3));
                                self.mem.write(y0 + (n * b + ro) * dp + 2 * co, &bytes)?;
                            }
                        }
                    }
                }
            }
        }
        Ok(())
    }

    /// `copy Y <- X perm=(1, 0)`: X's two word axes swapped (L2.MOV.013).
    fn transpose(&mut self, to: &View, from: &View) -> R<()> {
        let (a, b) = (self.buf(&to.name)?.clone(), self.buf(&from.name)?.clone());
        let (rows, words) = (from.axes[0].1, from.axes[1].1 * b.item() / WORD);
        for r in 0..rows {
            for w in 0..words {
                let word = self.mem.word(b.at + r * b.pitch() + w * WORD)?;
                self.mem.set_word(a.at + w * a.pitch() + r * WORD, &word)?;
            }
        }
        Ok(())
    }

    fn copy(&mut self, st: &Stmt) -> R<()> {
        let pos = st.positional();
        let (to, from) = (self.view(pos[0])?, self.view(pos[2])?);
        if st.kw("perm").is_some() {
            return self.transpose(&to, &from);
        }
        let plain = self.buf(&to.name)?.layout == Layout::Rows
            && self.buf(&from.name)?.layout == Layout::Rows;
        if !plain && st.kw("walk").is_none() {
            return self.copy_elements(&to, &from);
        }
        let mv = Move::Copy {
            src: self.side(pos[2], st.kw("walk"), st.kw("at"))?,
            dst: self.side(pos[0], st.kw("dwalk"), st.kw("dat"))?,
            pad: 0,
            ew: 32,
        };
        mover::run(&mv, self.mem)
    }

    fn fill(&mut self, st: &Stmt) -> R<()> {
        let pos = st.positional();
        let v = self.view(pos[0])?;
        let b = self.buf(&v.name)?;
        let dst = match st.kw("walk") {
            Some(_) => self.side(pos[0], st.kw("walk"), st.kw("at"))?,
            None if v.axes.iter().zip(&b.shape).all(|(a, n)| *a == (0, *n)) => whole(b),
            None => self.walk_of(&v)?,
        };
        let value = st.kw("value").ok_or("fill needs value=")?;
        let mv = Move::Fill {
            dst,
            value: int(value, &self.env)? as u64,
            ew: st.kw("ew").map_or(Ok(32), |t| int(t, &self.env))? as u32,
        };
        mover::run(&mv, self.mem)
    }

    fn indexed(&mut self, st: &Stmt) -> R<()> {
        let pos = st.positional();
        let fp = pos.iter().any(|p| p.as_name() == Some("fp"));
        let list = self.view(st.kw("index").ok_or("index=")?)?;
        let (_, rows, c0, cols) = list.rect();
        let table = self.buf(&list.name)?;
        let first = table.element(0, c0.max(list.axes[0].0))?;
        let n = rows.max(cols);
        let (dst, src) = (self.view(pos[0])?, self.view(pos[2])?);
        let (data, of) = if st.op == "gather" {
            (&dst, &src)
        } else {
            (&src, &dst)
        };
        let row_bytes = data.rect().3 * self.buf(&data.name)?.item();
        let pitch = match st.kw("table_stride") {
            Some(t) => int(t, &self.env)?,
            None => self.buf(&of.name)?.pitch().max(WORD),
        };
        let index = Index {
            at: first,
            n,
            pitch,
            words: row_bytes / WORD,
            fp,
        };
        let at = self.buf(&of.name)?.at;
        let mv = if st.op == "gather" {
            Move::Gather {
                src: at,
                dst: self.walk_of(&dst)?,
                index,
            }
        } else {
            Move::Scatter {
                src: self.walk_of(&src)?,
                dst: at,
                index,
            }
        };
        mover::run(&mv, self.mem)
    }

    fn gt4(&mut self, st: &Stmt) -> R<()> {
        let pos = st.positional();
        if st.kw("walk").is_none() {
            let (to, from) = (self.view(pos[0])?, self.view(pos[2])?);
            return self.copy_elements(&to, &from);
        }
        let mv = Move::Gt4 {
            src: self.side(pos[2], st.kw("walk"), st.kw("at"))?,
            dst: self.side(pos[0], st.kw("dwalk"), st.kw("dat"))?,
        };
        mover::run(&mv, self.mem)
    }

    /// One entry a group and block, each read through the source's elements
    /// or composed read; what lies outside is zero (L2.MOV.010, L2.READ.001).
    fn quant(&mut self, st: &Stmt) -> R<()> {
        let pos = st.positional();
        let (q, x) = (self.view(pos[0])?, self.view(pos[2])?);
        let (dst, src) = (self.buf(&q.name)?.clone(), self.buf(&x.name)?.clone());
        let Layout::Mx7 { b, .. } = dst.layout else {
            return Err("a quant writes an MXFP7 buffer".into());
        };
        let (r0, rows, k0, keys) = q.rect();
        let (s0, mut srows, t0, mut scols) = x.rect();
        if st.kw("zero").is_some() && st.kw("read_map").is_some() {
            return Err("zero packing has no read map".into());
        }
        match st
            .kw("zero")
            .map(|value| int(value, &self.env))
            .transpose()?
        {
            None | Some(0) => {}
            Some(1) if st.kw("pad").is_some() => {
                srows = 0;
                scols = 0;
            }
            _ => return Err("zero is 0 or 1; zero=1 requires a padded quant".into()),
        }
        let reader = Window::of(st, self, *src.shape.last().ok_or("a quant source")?)?;
        let checked = st.kw("finite").and_then(Term::as_int) != Some(1);
        for g in 0..rows / mx7::LANES as i64 {
            for k in 0..keys / mx7::KBLOCK as i64 {
                let mut e = [[0u16; mx7::KBLOCK]; mx7::LANES];
                for (l, row) in e.iter_mut().enumerate() {
                    for (j, value) in row.iter_mut().enumerate() {
                        let (r, c) = (g * 4 + l as i64, k * 32 + j as i64);
                        let element = match &reader {
                            Some(w) => w.element(r, c),
                            None => (r < srows && c < scols).then_some((s0 + r, t0 + c)),
                        };
                        if let Some((sr, sc)) = element {
                            let bytes = self.mem.read(src.element(sr, sc)?, 2)?;
                            *value = u16::from_le_bytes([bytes[0], bytes[1]]);
                            if checked && *value & 0x7c00 == 0x7c00 {
                                return Err(format!("quant of a non-finite value at ({sr}, {sc})"));
                            }
                        }
                    }
                }
                let at = dst.entry((r0 + g * 4) / 4, k0 / 32 + k)?;
                for (i, w) in mx7::entry(&e).write(b).iter().enumerate() {
                    self.mem.set_word(at + i as i64 * WORD, w)?;
                }
            }
        }
        Ok(())
    }
}

/// `read_shape` and `read_batch` over a composed source of `pitch` columns.
struct Window {
    map: Rc<Vec<i64>>,
    pitch: i64,
    row0: i64,
    col0: i64,
    rows: i64,
    cols: i64,
    batches: i64,
    padded: i64,
    source_pitch: i64,
}

impl Window {
    /// The window of `st`'s composed read of a source `source_pitch` values wide.
    fn of(st: &Stmt, sim: &mut Sim<'_>, source_pitch: i64) -> R<Option<Window>> {
        if st.kw("read_map").is_none() {
            return Ok(None);
        }
        let Some(Term::Tuple(d)) = st.kw("read_shape") else {
            return Err("read_shape=".into());
        };
        let map = sim.maps.read(st, &sim.env)?;
        let env = &sim.env;
        let dim = |i: usize| int(&d[i], env);
        let (batches, padded) = match st.kw("read_batch") {
            Some(t) => crate::l1::term::pair(t, env)?,
            None => (1, dim(3)?),
        };
        Ok(Some(Window {
            map,
            pitch: dim(0)?,
            row0: dim(1)?,
            col0: dim(2)?,
            rows: dim(3)?,
            cols: dim(4)?,
            batches,
            padded,
            source_pitch,
        }))
    }

    /// The source (row, column) behind value (r, c), none in the padding.
    fn element(&self, r: i64, c: i64) -> Option<(i64, i64)> {
        let (batch, local) = (r / self.padded, r % self.padded);
        if batch >= self.batches || local >= self.rows || c >= self.cols {
            return None;
        }
        let logical = (self.row0 + batch * self.rows + local) * self.pitch + self.col0 + c;
        let source = *self.map.get(logical as usize)?;
        (source >= 0).then_some((source / self.source_pitch, source % self.source_pitch))
    }
}
