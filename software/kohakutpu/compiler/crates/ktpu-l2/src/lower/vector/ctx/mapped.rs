//! Logical source words staged into the ordinary typed vector pipeline.

use super::emit::typed::{symbol, Emitter};
use super::*;
use crate::lower::readmap::{Exchange, ReadMap, Tile};
use crate::lower::vector::gen::typed::{self as ir, Select};

impl Ctx<'_> {
    pub(in crate::lower::vector) fn mapped_tile(
        &self,
        o: &Op,
        at: Option<i64>,
    ) -> VResult<Option<Tile>> {
        if o.op != "load" || o.st.kw("read_map").is_none() {
            return Ok(None);
        }
        let t = o.ty.as_ref().unwrap();
        if t.dtype != "f16"
            || t.shape.len() != 2
            || (t.mode.is_none() && t.shape[1] != self.t.vlmax)
        {
            return Err(self.fail(&o.st, "L2.READ.001", "mapped load is plain f16[R, VLMAX]"));
        }
        let mut env = self.env.clone();
        if let (Some(lp), Some(at)) = (&self.lp, at) {
            env = bind(&env, &lp.var, at);
        }
        let map = ReadMap::read(self.body, o, &env)?;
        let first = crate::ir::const_of(
            self.body,
            &o.st,
            o.st.kw("read_at").unwrap_or(&Term::int(0)),
            &env,
        )?;
        if first < 0 {
            return Err(self.fail(
                &o.st,
                "L2.READ.001",
                "logical load is inside its composed destination",
            ));
        }
        let pitch = if let Some(v) = o.st.kw("read_pitch") {
            crate::ir::const_of(self.body, &o.st, v, &env)?
        } else {
            t.shape[1]
        };
        let stride = if let Some(v) = o.st.kw("read_stride") {
            crate::ir::const_of(self.body, &o.st, v, &env)?
        } else {
            1
        };
        let positions = if t.mode.as_deref() == Some("gt4") {
            (0..t.shape[0] / 4)
                .flat_map(|b| {
                    (0..t.shape[1] / 32).flat_map(move |col| {
                        (0..2).flat_map(move |half| {
                            (0..4).flat_map(move |r| {
                                (0..16).map(move |lane| {
                                    first
                                        + (b * 4 + r) * pitch
                                        + (col * 32 + half * 16 + lane) * stride
                                })
                            })
                        })
                    })
                })
                .collect::<Vec<_>>()
        } else {
            (0..t.shape[0])
                .flat_map(|r| (0..t.shape[1]).map(move |c| first + r * pitch + c * stride))
                .collect::<Vec<_>>()
        };
        let valid = if let Some(v) = o.st.kw("read_rows") {
            crate::ir::const_of(self.body, &o.st, v, &env)?
        } else {
            t.shape[0]
        };
        let columns = if let Some(v) = o.st.kw("read_cols") {
            crate::ir::const_of(self.body, &o.st, v, &env)?
        } else {
            t.shape[1]
        };
        let width = t.shape[1] as usize;
        let positions = positions
            .into_iter()
            .enumerate()
            .map(|(i, p)| {
                let (row, col) = if t.mode.as_deref() == Some("gt4") {
                    (
                        (i / (width * 4)) * 4 + (i / 16) % 4,
                        ((i / 128) % (width / 32)) * 32 + ((i / 64) % 2) * 16 + i % 16,
                    )
                } else {
                    (i / width, i % width)
                };
                if row >= valid as usize || col >= columns as usize {
                    -1
                } else {
                    p
                }
            })
            .collect::<Vec<_>>();
        if positions.iter().any(|&p| p >= map.elements.len() as i64) {
            return Err(self.fail(
                &o.st,
                "L2.READ.001",
                "mapped tile fits its logical destination",
            ));
        }
        let mut tile = map.tile(&positions, &self.t.read);
        if t.mode.as_deref() == Some("gt4") && width == 32 {
            tile.shape.exchange = map.exchange(&positions, t.shape[0] as usize, &tile);
        }
        Ok(Some(tile))
    }

    /// The registers of an [`Exchange`]: GT4 source reads, two masked lane
    /// swaps, then a pack and a GT4 read of its words.
    fn exchange_unpack(
        &self,
        g: &mut Emitter,
        x: Exchange,
        slot: i64,
        spare: i64,
        target: &str,
        local: &mut IndexMap<String, Val>,
    ) {
        let block = self.exchanged.as_ref().expect("a tile's exchange masks");
        let unpack = |operand: Term, at: i64, options: Vec<Term>| {
            ir::memory(ir::Memory::Unpack, operand, at, Some("gt4"), false, options).unwrap()
        };
        let regs = (x.cols / 8) as i64;
        let mut held = Vec::new();
        for n in 0..regs {
            let s = g.sym();
            g.op(
                unpack(s.operand(Select::Whole), slot + 8 * n, vec![]),
                vec![s.clone()],
            );
            held.push(s);
        }
        for (k, mask) in [5, 10].into_iter().zip(block) {
            let mask = symbol(mask);
            for s in held.iter_mut() {
                let d = g.sym();
                let a = s.operand(Select::Whole);
                g.op(
                    ir::math(
                        "vsel",
                        d.operand(Select::Whole),
                        vec![
                            a.clone(),
                            ir::crossbar("xor", a, k),
                            mask.operand(Select::Whole),
                        ],
                        vec![],
                    ),
                    vec![d.clone(), s.clone(), mask.clone()],
                );
                *s = d;
            }
        }
        g.fixed(ir::sync("pack", "unpack", None));
        for (n, s) in held.into_iter().enumerate() {
            g.op(
                ir::memory(
                    ir::Memory::Pack,
                    s.operand(Select::Whole),
                    spare + 8 * n as i64,
                    Some("f16"),
                    false,
                    vec![],
                )
                .unwrap(),
                vec![s],
            );
        }
        g.fixed(ir::sync("unpack", "pack", None));
        let bands = self.tile.min(local[target].rows);
        let zero = |d: &ir::Symbol, g: &mut Emitter, chunk: u64, n: i64| {
            let k0 = Term::name("k0");
            g.op(
                ir::math(
                    "vsel",
                    d.operand(if n > 1 {
                        Select::Stride(chunk)
                    } else {
                        Select::Chunk(chunk)
                    }),
                    vec![Term::name("k1"), k0.clone(), k0],
                    vec![ir::kw("vl", Term::int((n * 16) as i128))],
                ),
                vec![d.clone()],
            );
        };
        for b in 0..bands {
            let d = g.sym();
            local.get_mut(target).unwrap().regs.insert(b, d.name());
            let mut options = vec![ir::kw("walk", ir::walk(&[(4, 16)]).unwrap())];
            if x.cols == 16 {
                options.push(ir::kw("n", Term::int(4)));
            }
            g.op(
                unpack(d.operand(Select::Whole), spare + x.band as i64 + b, options),
                vec![d.clone()],
            );
            if x.cols == 16 {
                zero(&d, g, 4, 4);
            }
            for r in (0..4).filter(|r| 4 * b as usize + r >= x.rows) {
                zero(&d, g, r as u64, 1);
                if x.cols == 32 {
                    zero(&d, g, 4 + r as u64, 1);
                }
            }
        }
    }

    /// The pinned masks of a tile's exchange.
    pub(super) fn exchange_masks(&mut self, g: &mut Emitter) {
        let made = Exchange::mask_writes().map(|writes| {
            let m = g.sym();
            for (vl, set) in writes {
                g.op(
                    ir::math(
                        "vmov",
                        m.operand(Select::Chunk(0)),
                        vec![Term::name(if set { "k1" } else { "k0" })],
                        vec![ir::kw("vl", Term::int(vl as i128))],
                    ),
                    vec![m.clone()],
                );
            }
            g.op(
                ir::math(
                    "vsel",
                    m.operand(Select::Stride(1)),
                    vec![
                        Term::name("k1"),
                        m.operand(Select::Chunk(0)),
                        Term::name("k0"),
                    ],
                    vec![ir::kw("vl", Term::int(112))],
                ),
                vec![m.clone()],
            );
            self.pinned.insert(m.name());
            m.name()
        });
        self.exchanged = Some(made);
    }

    pub(super) fn fill_typed(&self, g: &mut Emitter, d: &Desc, slot: i64, relative: bool) {
        self.fill_phase(g, d, slot, relative, self.phase);
    }

    pub(super) fn fill_phase(
        &self,
        g: &mut Emitter,
        d: &Desc,
        slot: i64,
        relative: bool,
        phase: usize,
    ) {
        let address = Term::name(format!("a{}", d.ad));
        if d.mapped.is_some() {
            let shape = &d.phases[phase % d.phases.len()];
            let (_, _, transfers) =
                shape.descriptor_transfers(self.t, self.t.read.load_gather_words);
            let advance = d.advances[phase % d.advances.len()];
            let depth = self.t.dma_queue_depth;
            if transfers.len() > 1
                && !on("l2.readmap/no-load-throttle")
                && !on("l2.fill/no-throttle")
            {
                g.fixed(ir::sync("fe", "fill", None));
            }
            for (i, &(position, offset)) in transfers.iter().enumerate() {
                if i > 0
                    && i % depth == 0
                    && !on("l2.readmap/no-load-throttle")
                    && !on("l2.fill/no-throttle")
                {
                    g.fixed(ir::sync("fe", "fill", None));
                }
                let next = transfers
                    .get(i + 1)
                    .map(|x| x.1)
                    .unwrap_or(if self.trips.len() > 1 {
                        advance
                    } else {
                        offset
                    });
                let step = next - offset;
                let rebase = !(-self.t.step() <= step && step < self.t.step());
                g.fixed(ir::ainc(
                    &format!("a{}", d.ad),
                    if rebase { 0 } else { step },
                ));
                g.fixed(
                    ir::memory(
                        ir::Memory::Fill,
                        address.clone(),
                        slot + position as i64,
                        None,
                        relative,
                        vec![],
                    )
                    .unwrap(),
                );
                if rebase && i + 1 < transfers.len() {
                    g.fixed(
                        ir::desc(
                            &format!("a{}", d.ad),
                            Term::Offset(
                                Box::new(Term::name(format!("p_a{}", d.ad))),
                                Box::new(Term::int(if on("l2.readmap/no-rebase") {
                                    0
                                } else {
                                    next as i128
                                })),
                            ),
                            &d.walk.0,
                        )
                        .unwrap(),
                    );
                }
            }
        } else {
            g.fixed(ir::memory(ir::Memory::Fill, address, slot, None, relative, vec![]).unwrap());
        }
    }

    pub(super) fn mapped_unpack(
        &self,
        g: &mut Emitter,
        load: &Op,
        slot: i64,
        rows: &[i64],
        target: &str,
        local: &mut IndexMap<String, Val>,
    ) -> VResult<bool> {
        let Some(&index) = self.of.get(&load.uid) else {
            return Ok(false);
        };
        let d = &self.descs[index];
        if d.mapped.is_none() {
            return Ok(false);
        }
        let shape = &d.phases[self.phase % d.phases.len()];
        if let Some(x) = shape.exchange {
            if rows.iter().any(|r| !local[target].regs.contains_key(r)) {
                self.exchange_unpack(g, x, slot, d.spare, target, local);
            }
            return Ok(true);
        }
        let is_gt4 = load
            .ty
            .as_ref()
            .is_some_and(|t| t.mode.as_deref() == Some("gt4"));
        let allow_direct =
            is_gt4 && !on("l2.readmap/dsl-chunk") && !on("l2.readmap/dsl-copy-reuse");
        let mut row_chunks = Vec::new();
        let mut needed_blocks = std::collections::BTreeSet::new();
        for &row in rows {
            let chunks = (0..8)
                .map(|chunk| {
                    shape
                        .segments
                        .iter()
                        .filter(|s| s.chunk == row as usize * 8 + chunk)
                        .collect::<Vec<_>>()
                })
                .collect::<Vec<_>>();
            let direct = if allow_direct
                && chunks.iter().all(|c| {
                    c.len() == 1 && c[0].end == 16 && matches!(c[0].source, Some((_, 0, false)))
                }) {
                let words: Vec<i64> = chunks
                    .iter()
                    .map(|c| c[0].source.unwrap().0 as i64)
                    .collect();
                let s0 = words[1] - words[0];
                let s1 = words[4] - words[0];
                let stride = self.t.read.direct_stride;
                if s0 > 0
                    && s0 < stride
                    && s1 > 0
                    && s1 < stride
                    && (0..8)
                        .all(|c| words[c] == words[0] + (c as i64 % 4) * s0 + (c as i64 / 4) * s1)
                {
                    Some((words[0], s0, s1))
                } else {
                    None
                }
            } else {
                None
            };
            if direct.is_none() {
                for c in &chunks {
                    for s in c {
                        if let Some((word, _, _)) = s.source {
                            needed_blocks.insert(word / 8);
                        }
                    }
                }
            }
            row_chunks.push((row, chunks, direct));
        }
        let mut sources: std::collections::BTreeMap<usize, ir::Symbol> =
            std::collections::BTreeMap::new();
        for i in needed_blocks {
            let source = g.sym();
            let n = (shape.words.len() - 8 * i).min(8);
            g.op(
                ir::memory(
                    ir::Memory::Unpack,
                    source.operand(if on("l2.readmap/dsl-chunk") {
                        Select::Stride(1)
                    } else {
                        Select::Whole
                    }),
                    slot + (8 * i) as i64,
                    Some("f16"),
                    false,
                    vec![ir::kw("n", Term::int(n as i128))],
                )
                .unwrap(),
                vec![source.clone()],
            );
            sources.insert(i, source);
        }
        let mut assembled: std::collections::BTreeMap<_, (ir::Symbol, usize)> =
            std::collections::BTreeMap::new();
        let mut pending_rows = Vec::new();
        let mut all_copies = Vec::new();
        for (row, mut chunks, direct) in row_chunks {
            let dest = g.sym();
            local.get_mut(target).unwrap().regs.insert(row, dest.name());
            if let Some((w0, s0, s1)) = direct {
                let mut kw = Vec::new();
                if (s0, s1) != (1, 4) {
                    kw.push(ir::kw("walk", ir::walk(&[(s0, s1)]).unwrap()));
                }
                g.op(
                    ir::memory(
                        ir::Memory::Unpack,
                        dest.operand(Select::Whole),
                        slot + w0,
                        Some("f16"),
                        false,
                        kw,
                    )
                    .unwrap(),
                    vec![dest],
                );
                continue;
            }
            for (chunk, program) in chunks.iter_mut().enumerate() {
                if program.is_empty() {
                    continue;
                }
                let key = program
                    .iter()
                    .map(|s| (s.end, s.source))
                    .collect::<Vec<_>>();
                if let Some((source, from)) = assembled.get(&key) {
                    all_copies.push((dest.clone(), chunk, source.clone(), *from));
                    program.clear();
                } else {
                    assembled.insert(key, (dest.clone(), chunk));
                }
            }
            pending_rows.push((dest, chunks));
        }
        let max_depth = pending_rows
            .iter()
            .flat_map(|(_, chunks)| chunks.iter().map(Vec::len))
            .max()
            .unwrap_or(0);
        for layer in 0..max_depth {
            let mut layer_ops = Vec::new();
            for (row_idx, (dest, chunks)) in pending_rows.iter().enumerate() {
                let mut chunk = 0;
                while chunk < 8 {
                    let Some(segment) = chunks[chunk].get(layer) else {
                        chunk += 1;
                        continue;
                    };
                    let mut n = 1;
                    let sequential = segment.source.is_some_and(|(a, k, b)| {
                        chunks
                            .get(chunk + 1)
                            .and_then(|c| c.get(layer))
                            .is_some_and(|next| {
                                next.source == Some((a + 1, k, b)) && a / 8 == (a + 1) / 8
                            })
                    });
                    if segment.end == 16 {
                        while chunk + n < 8
                            && chunks[chunk + n].get(layer).is_some_and(|next| {
                                next.end == 16
                                    && next.source
                                        == segment.source.map(|(a, k, b)| {
                                            (a + if sequential { n } else { 0 }, k, b)
                                        })
                                    && !segment
                                        .source
                                        .is_some_and(|(a, _, _)| sequential && a / 8 != (a + n) / 8)
                            })
                        {
                            n += 1;
                        }
                    }
                    let (xm, xk, cb) = match segment.source {
                        None => (0u8, 0usize, 0usize),
                        Some((w, k, true)) => (3, k, w % 8),
                        Some((w, 0, false)) => (1, 0, w % 8),
                        Some((w, k, false)) => (2, k, w % 8),
                    };
                    let mut kw = Vec::new();
                    let mut symbols = vec![dest.clone()];
                    let source = match segment.source {
                        None => Term::name("k0"),
                        Some((word, k, broadcast)) => {
                            let symbol = &sources[&(word / 8)];
                            symbols.push(symbol.clone());
                            let operand = symbol.operand(if sequential && n > 1 {
                                Select::Stride((word % 8) as u64)
                            } else {
                                Select::Chunk((word % 8) as u64)
                            });
                            if broadcast {
                                ir::bcast(operand, k as i64, 3)
                            } else if k == 0 {
                                operand
                            } else {
                                ir::crossbar("rot", operand, k as i64)
                            }
                        }
                    };
                    let vl = if n > 1 { n * 16 } else { segment.end };
                    kw.push(ir::kw("vl", Term::int(vl as i128)));
                    let st = ir::math(
                        "vsel",
                        dest.operand(if n > 1 {
                            Select::Stride(chunk as u64)
                        } else {
                            Select::Chunk(chunk as u64)
                        }),
                        vec![Term::name("k1"), source, Term::name("k0")],
                        kw,
                    );
                    let half_key = ((xk / 4) % 2) ^ (chunk / 4);
                    layer_ops.push(((vl, chunk % 4, cb, xm, xk, half_key, row_idx), st, symbols));
                    chunk += n;
                }
            }
            layer_ops.sort_by_key(|(k, _, _)| *k);
            if layer % 2 == 1 {
                layer_ops.reverse();
            }
            for (_, st, symbols) in layer_ops {
                g.op(st, symbols);
            }
        }
        for (dest, chunk, source, from) in all_copies {
            g.op(
                ir::math(
                    "vmov",
                    dest.operand(Select::Chunk(chunk as u64)),
                    vec![
                        source.operand(Select::Chunk(if on("l2.readmap/dsl-copy-reuse") {
                            (from as u64 + 1) % 8
                        } else {
                            from as u64
                        })),
                    ],
                    vec![ir::kw("vl", Term::int(16))],
                ),
                vec![dest, source],
            );
        }
        Ok(true)
    }
}
