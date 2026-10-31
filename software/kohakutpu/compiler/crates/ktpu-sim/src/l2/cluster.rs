//! A cluster instance: fills, sweeps and drains on the cluster model
//! (L2.MG.001-.010), the banks alternating by side.

use std::collections::HashMap;

use ktpu_l1::text::{Stmt, Term};

use super::node::holds;
use super::view::Layout;
use super::{Rows, Sim, CORE_KEY};
use crate::l1::term::int;
use crate::num::mx7;
use crate::units::cluster::{Cluster, Word, BUF_ACC};
use crate::R;

const LANES: i64 = 4;
const KBLOCK: i64 = 32;
/// The sweep's exponent anchor the lowering writes when none is chosen.
const ANCHOR: i64 = 40;

/// A bank's value.
#[derive(Clone, Copy)]
struct Operand {
    bank: usize,
    groups: i64,
    nk: i64,
}

#[derive(Default)]
struct Instance {
    key: i64,
    operands: HashMap<String, Operand>,
    next: [usize; 2],
    /// Accumulators a sweep has written.
    open: HashMap<String, bool>,
    /// Each accumulator's rows and columns.
    shapes: HashMap<String, (i64, i64)>,
}

pub fn mg_block(sim: &mut Sim<'_>, unit: i64, body: &[Stmt]) -> R<()> {
    let key = sim.key(sim.node, unit);
    let mut i = Instance {
        key,
        ..Instance::default()
    };
    let mut cluster = sim.clusters.remove(&key).unwrap_or_default();
    let out = i.block(sim, &mut cluster, body);
    sim.clusters.insert(key, cluster);
    out
}

/// The R x C of a statement's value.
fn shape(st: &Stmt) -> R<(i64, i64)> {
    let ty = match st.annot.as_ref().ok_or("a value's type")? {
        Term::Placed(x, _) => x.as_ref(),
        other => other,
    };
    match ty {
        Term::View(_, d) if d.len() == 2 => Ok((
            d[0].as_int().unwrap_or(0) as i64,
            d[1].as_int().unwrap_or(0) as i64,
        )),
        _ => Err("a value of two dimensions".into()),
    }
}

impl Instance {
    fn block(&mut self, sim: &mut Sim<'_>, c: &mut Cluster, body: &[Stmt]) -> R<()> {
        for st in body {
            self.statement(sim, c, st)
                .map_err(|e| format!("line {}: {e}", st.line))?;
        }
        Ok(())
    }

    fn statement(&mut self, sim: &mut Sim<'_>, c: &mut Cluster, st: &Stmt) -> R<()> {
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
                    self.block(sim, c, &st.body)?;
                }
                sim.env.remove(&var);
                Ok(())
            }
            "when" => match holds(pos[0], &sim.env)? {
                true => self.block(sim, c, &st.body),
                false => Ok(()),
            },
            "fill" => self.fill(sim, c, st),
            "zero" => {
                let name = st.target_name().ok_or("c = zero")?.to_string();
                self.shapes.insert(name.clone(), shape(st)?);
                self.open.insert(name, false);
                Ok(())
            }
            "gemm" => self.gemm(sim, c, st),
            "recv" => self.recv(sim, c, st),
            "drain" => self.drain(sim, c, st),
            op => Err(format!("no cluster op {op} in this simulator")),
        }
    }

    fn drain(&mut self, sim: &mut Sim<'_>, c: &mut Cluster, st: &Stmt) -> R<()> {
        let pos = st.positional();
        if let Term::View(unit, s) = pos[2] {
            if (unit == "vc" || unit == "mg") && s.len() == 1 {
                return self.drain_to(sim, c, st);
            }
        }
        let v = sim.view(pos[2])?;
        let b = sim.buf(&v.name)?;
        let (r0, rows, c0, cols) = v.rect();
        let word = Word::Drain {
            addr: b.element(r0, c0)?,
            n: (rows / LANES * (cols / LANES)) as usize,
        };
        if sim.deferring {
            sim.drains.push((self.key, word));
            return Ok(());
        }
        c.run(&word, sim.mem)
    }

    /// An accumulator into a core's L1 or, `add`, a cluster's accumulator.
    fn drain_to(&mut self, sim: &mut Sim<'_>, c: &mut Cluster, st: &Stmt) -> R<()> {
        let pos = st.positional();
        let name = pos[0].as_name().ok_or("drain ACC -> UNIT")?;
        let (rows, cols) = *self
            .shapes
            .get(name)
            .ok_or(format!("no accumulator {name}"))?;
        let tiles = (rows / LANES * (cols / LANES)) as usize;
        let node = st.kw("node").map_or(Ok(sim.node), |t| int(t, &sim.env))?;
        let index = sim.unit(pos[2])?;
        if matches!(pos[2], Term::View(u, _) if u == "mg") {
            sim.adds.push((sim.key(node, index), c.out(BUF_ACC, tiles)));
            return Ok(());
        }
        let words = c.words(tiles);
        let per_row = (cols / LANES) as usize;
        let mut v = Vec::with_capacity((rows * cols) as usize);
        for r in 0..rows as usize {
            for col in 0..cols as usize {
                let word = &words[(r / 4) * per_row + col / 4];
                let e = (r % 4 * 4 + col % 4) * 2;
                v.push(u32::from(u16::from_le_bytes([word[e], word[e + 1]])));
            }
        }
        let value = Rows {
            rows: rows as usize,
            cols: cols as usize,
            v,
        };
        sim.links
            .entry(sim.key(node, CORE_KEY + index))
            .or_default()
            .push_back(value);
        Ok(())
    }

    /// Rows a core sent, quantised into the next bank as a burst lands them.
    fn recv(&mut self, sim: &mut Sim<'_>, c: &mut Cluster, st: &Stmt) -> R<()> {
        let (rows, cols) = shape(st)?;
        let got = sim.received(self.key, rows as usize)?;
        let side = false;
        let bank = self.next[0];
        self.next[0] = 1 - bank;
        let (groups, nk) = (rows / LANES, cols / KBLOCK);
        for g in 0..groups {
            for k in 0..nk {
                let mut x = [[0u16; mx7::KBLOCK]; mx7::LANES];
                for (l, row) in x.iter_mut().enumerate() {
                    for (e, code) in row.iter_mut().enumerate() {
                        let col = k as usize * mx7::KBLOCK + e;
                        *code = got.v[(g as usize * 4 + l) * got.cols + col] as u16;
                    }
                }
                c.place(side, bank, (g * nk + k) as usize, mx7::entry(&x));
            }
        }
        let name = st.target_name().ok_or("x = recv")?.to_string();
        self.operands.insert(name, Operand { bank, groups, nk });
        Ok(())
    }

    fn fill(&mut self, sim: &mut Sim<'_>, c: &mut Cluster, st: &Stmt) -> R<()> {
        let v = sim.view(st.positional()[0])?;
        let b = sim.buf(&v.name)?.clone();
        let Layout::Mx7 { b: side, g, nk } = b.layout else {
            return Err("a fill reads an MXFP7 buffer".into());
        };
        let which = usize::from(side);
        let bank = self.next[which];
        self.next[which] = 1 - bank;
        let (r0, rows, k0, _) = v.rect();
        let (mut lo, hi) = (r0 / LANES, (r0 + rows) / LANES);
        let groups = hi - lo;
        let mut eoff = 0;
        while lo < hi {
            let t = lo / g;
            let top = hi.min((t + 1) * g);
            let n = (top - lo) * nk;
            let word = Word::Fill {
                b: side,
                bank,
                eoff: eoff as usize,
                addr: b.entry(lo, k0 / KBLOCK)?,
                n: n as usize,
            };
            c.run(&word, sim.mem)?;
            eoff += n;
            lo = top;
        }
        let name = st.target_name().ok_or("x = fill")?.to_string();
        self.operands.insert(name, Operand { bank, groups, nk });
        Ok(())
    }

    fn gemm(&mut self, sim: &mut Sim<'_>, c: &mut Cluster, st: &Stmt) -> R<()> {
        let pos = st.positional();
        let operand = |t: &Term| {
            let name = t.as_name().ok_or("gemm a, b")?;
            self.operands
                .get(name)
                .copied()
                .ok_or(format!("no operand {name}"))
        };
        let (a, b) = (operand(pos[0])?, operand(pos[1])?);
        let name = st.target_name().ok_or("c = gemm")?.to_string();
        self.shapes.insert(name.clone(), shape(st)?);
        let keep = self.open.get(&name).copied().unwrap_or(false);
        let word = Word::Gemm {
            gm: a.groups as usize,
            gn: b.groups as usize,
            nk: a.nk as usize,
            abank: a.bank,
            bbank: b.bank,
            aoff: 0,
            boff: 0,
            acc: keep,
            anchor: ANCHOR,
            emit: None,
        };
        c.run(&word, sim.mem)?;
        self.open.insert(name, true);
        Ok(())
    }
}
