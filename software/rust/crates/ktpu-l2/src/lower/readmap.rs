//! Logical element walks composed before memory lowering; tiles separate
//! source word addresses from register lane rearrangement.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::{Arc, Mutex, OnceLock};

use ktpu_l1::text::{Env, Stmt, Term};

use crate::ir::{const_of, Body, Op, Result};
use crate::target::Target;

type StepKey = (i64, i64, i64, i64, Vec<(i64, i64)>, Vec<(i64, i64)>);
type ReadmapCacheMap = HashMap<Vec<StepKey>, Arc<Vec<i64>>>;
pub type DescriptorTransfers = (Vec<(i64, i64)>, usize, Vec<(usize, i64)>);
type ChunkKey = Vec<(usize, Option<(usize, usize, bool)>)>;

static READMAP_CACHE: OnceLock<Mutex<ReadmapCacheMap>> = OnceLock::new();
/// Composed maps a process remembers before starting over.
const MEMO_MAPS: usize = 256;
/// Levels of a walk the source descriptors take.
const MAX_DIMS: usize = 4;

/// F16 values a source word holds and words a vector register holds: the shape
/// a composed read is written for (`fits_layout`).
const WORD_LANES: i64 = 16;
const REG_WORDS: usize = 8;
/// Source registers v32.. hold a tile's words; lower registers hold entries.
const SOURCE_REG: usize = 32;
/// Registers a composed read takes, and descriptors of its smallest image.
const VREGS: u32 = 48;
const DESCRIPTORS: usize = 4;
/// L1 words a composed pack takes: two source slots, the pack output, the status.
pub const L1_WORDS: i64 = 388;

/// The stack's choices in composed reads (`docs/spec/stack/l2/program.md`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Knobs {
    /// Most words a tile's lattice covers, and the floor of its padding bound.
    pub lattice_words: usize,
    pub lattice_floor: usize,
    /// Most words one non-contiguous transfer gathers: pack images, loads.
    pub pack_gather_words: usize,
    pub load_gather_words: usize,
    /// Destinations one pack drains to; entries, segments and copies of an image.
    pub fanout: usize,
    pub image_entries: usize,
    pub image_segments: usize,
    pub image_copies: usize,
    /// Keys a run search tries, and the longest run it extends to.
    pub run_probe: usize,
    pub run_limit: i64,
    /// Most trips of a looped pack image.
    pub image_trips: usize,
    /// IMEM words an image leaves free.
    pub imem_margin: usize,
    /// Largest word stride of a direct unpack.
    pub direct_stride: i64,
    /// Cycles of a dispatch and configuration, a continuation.
    pub continuation_cycles: i64,
    /// Retile costs the longest core of every stage, not the first core's first.
    pub retile_every_stage: bool,
    /// Split any image past the room, not only a composed read's: retile
    /// otherwise takes a smaller tile, with no continuation to pay for.
    pub split_every_image: bool,
}

impl Knobs {
    pub const DEFAULT: Knobs = Knobs {
        lattice_words: 64,
        lattice_floor: 32,
        pack_gather_words: 64,
        load_gather_words: 128,
        fanout: 64,
        image_entries: 4,
        image_segments: 256,
        image_copies: 64,
        run_probe: 256,
        run_limit: 128,
        image_trips: 64,
        imem_margin: 64,
        direct_stride: 512,
        continuation_cycles: 32,
        retile_every_stage: true,
        split_every_image: false,
    };
}

/// `st` or a statement inside it reads through a `read_map`.
pub fn reads_mapped(st: &Stmt) -> bool {
    st.kw("read_map").is_some() || st.body.iter().any(reads_mapped)
}

/// The machine holds the layout a composed read writes. A machine that does
/// not reads materialized (the planner consults this; lowering only checks).
pub fn fits_layout(t: &Target) -> bool {
    t.lanes == WORD_LANES
        && t.chunks == REG_WORDS as i64
        && t.l1_words >= L1_WORDS
        && t.vregs >= VREGS
        && t.descriptors >= DESCRIPTORS
}

pub struct ReadMap {
    pub elements: Arc<Vec<i64>>,
}

impl ReadMap {
    pub fn read(body: &Body, op: &Op, env: &Env) -> Result<Self> {
        let st = &op.st;
        let err = |s: &str| body.reader.fail(st, "L2.READ.001", s);
        let Some(Term::Tuple(steps)) = st.kw("read_map") else {
            return Err(err("read_map is a tuple of paired logical walks"));
        };
        let walk = |x: &Term| -> Result<Vec<(i64, i64)>> {
            let Term::Tuple(levels) = x else {
                return Err(err("read_map walk is a tuple"));
            };
            levels
                .iter()
                .map(|level| {
                    let Term::Tuple(pair) = level else {
                        return Err(err("read_map axis is (count, stride)"));
                    };
                    let [count, stride] = pair.as_slice() else {
                        return Err(err("read_map axis is (count, stride)"));
                    };
                    let count = const_of(body, st, count, env)?;
                    if count <= 0 {
                        return Err(err("read_map counts are positive"));
                    }
                    Ok((count, const_of(body, st, stride, env)?))
                })
                .collect()
        };
        let mut parsed_steps = Vec::with_capacity(steps.len());
        for step in steps {
            let Term::Tuple(fields) = step else {
                return Err(err("read_map step is a tuple"));
            };
            let [n, at, dat, m, sw, dw] = fields.as_slice() else {
                return Err(err("read_map step is (source extent, source offset, destination offset, destination extent, source walk, destination walk)"));
            };
            let n = const_of(body, st, n, env)?;
            let m = const_of(body, st, m, env)?;
            let at = const_of(body, st, at, env)?;
            let dat = const_of(body, st, dat, env)?;
            let sw = walk(sw)?;
            let dw = walk(dw)?;
            if sw.iter().map(|x| x.0).collect::<Vec<_>>()
                != dw.iter().map(|x| x.0).collect::<Vec<_>>()
            {
                return Err(err("read_map walks share their iteration axes"));
            }
            parsed_steps.push((n, at, dat, m, sw, dw));
        }
        if parsed_steps.is_empty() {
            return Err(err("read_map is nonempty"));
        }
        let cache = READMAP_CACHE.get_or_init(|| Mutex::new(HashMap::new()));
        if let Some(cached) = cache.lock().unwrap().get(&parsed_steps).cloned() {
            return Ok(Self { elements: cached });
        }
        let mut previous: Option<Vec<i64>> = None;
        for (n, at, dat, m, sw, dw) in &parsed_steps {
            let (n, at, dat, m) = (*n, *at, *dat, *m);
            if n <= 0 || m <= 0 || previous.as_ref().is_some_and(|p| n > p.len() as i64) {
                return Err(err(
                    "read_map extents must cover the preceding logical value",
                ));
            }
            let mut positions = vec![(at, dat)];
            for (&(count, s), &(_, d)) in sw.iter().zip(dw) {
                let mut next = Vec::with_capacity(positions.len() * count as usize);
                for (a, b) in positions {
                    for i in 0..count {
                        next.push((a + i * s, b + i * d));
                    }
                }
                positions = next;
            }
            let mut result = vec![-1; m as usize];
            let mut written = vec![false; m as usize];
            for (a, b) in positions {
                if !(0..n).contains(&a) || !(0..m).contains(&b) || written[b as usize] {
                    return Err(err(
                        "read_map has an out-of-bounds read/write or repeated destination",
                    ));
                }
                written[b as usize] = true;
                result[b as usize] = previous.as_ref().map_or(a, |p| p[a as usize]);
            }
            previous = Some(result);
        }
        let elements = Arc::new(previous.unwrap());
        let mut guard = cache.lock().unwrap();
        if guard.len() > MEMO_MAPS {
            guard.clear();
        }
        guard.insert(parsed_steps, Arc::clone(&elements));
        Ok(Self { elements })
    }

    pub fn tile(&self, positions: &[i64], k: &Knobs) -> Tile {
        Tile::new(
            &positions
                .iter()
                .map(|&p| if p < 0 { -1 } else { self.elements[p as usize] })
                .collect::<Vec<_>>(),
            k,
        )
    }
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Segment {
    pub chunk: usize,
    pub end: usize,
    pub source: Option<(usize, usize, bool)>,
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Shape {
    pub words: Vec<i64>,
    pub segments: Vec<Segment>,
}

pub struct Tile {
    pub anchor: i64,
    pub shape: Shape,
}

fn gen_lattice(dims: &[(i64, i64)]) -> Vec<i64> {
    let mut pts = vec![0];
    for &(stride, count) in dims {
        let mut next = Vec::with_capacity(pts.len() * count as usize);
        for i in 0..count {
            for &p in &pts {
                next.push(p + i * stride);
            }
        }
        pts = next;
    }
    pts
}

fn complete_lattice(
    pts: &[i64],
    limit_sz: usize,
    hi_w: i64,
    depth_left: usize,
) -> Option<Vec<(i64, i64)>> {
    if pts.len() <= 1 {
        return Some(Vec::new());
    }
    if pts.len() > limit_sz || *pts.last().unwrap() > hi_w {
        return None;
    }
    let s0 = pts[1] - pts[0];
    if s0 > 0 && pts.windows(2).all(|w| w[1] - w[0] == s0) {
        return Some(vec![(s0, pts.len() as i64)]);
    }
    if depth_left <= 1 {
        return None;
    }
    let mut diffs = BTreeSet::new();
    for (i, &a) in pts.iter().enumerate() {
        for &b in &pts[i + 1..] {
            if b > a {
                diffs.insert(b - a);
            }
        }
    }
    let mut best: Option<(usize, Vec<(i64, i64)>)> = None;
    for s in diffs.into_iter().rev() {
        let c = (*pts.last().unwrap() / s + 1) as usize;
        if c < 2 || c > limit_sz || (c as i64 - 1) * s > hi_w {
            continue;
        }
        let rem_hi = hi_w - (c as i64 - 1) * s;
        let max_bases = (limit_sz / c)
            .min(pts.len().div_ceil(c) + 1)
            .min(pts.len() - 1);
        let mut bases = BTreeSet::new();
        let mut ok = true;
        for &p in pts {
            let idx = ((p / s) as usize).min(c - 1) as i64;
            let b = p - idx * s;
            if b < 0 || b >= s || b > rem_hi {
                ok = false;
                break;
            }
            bases.insert(b);
            if bases.len() > max_bases {
                ok = false;
                break;
            }
        }
        if !ok {
            continue;
        }
        let base_vec: Vec<i64> = bases.into_iter().collect();
        if let Some(mut inner) = complete_lattice(&base_vec, limit_sz / c, rem_hi, depth_left - 1) {
            inner.push((s, c as i64));
            let sz: usize = inner.iter().map(|&(_, cnt)| cnt as usize).product();
            if best
                .as_ref()
                .is_none_or(|(bsz, bdims)| (sz, inner.len()) < (*bsz, bdims.len()))
            {
                best = Some((sz, inner));
                if sz == pts.len() {
                    break;
                }
            }
        }
    }
    best.map(|(_, d)| d)
}

/// The smallest lattice over `words` a descriptor walks in `dims` levels, or
/// None where none is worth its padding: the tile then fetches its own words.
fn covering_lattice(words: &[i64], dims: usize, k: &Knobs) -> Option<Vec<i64>> {
    if words.len() <= 1 || words.len() > k.lattice_words {
        return None;
    }
    let max_w = *words.last().unwrap();
    let max_allowed = k.lattice_words.min(k.lattice_floor.max(words.len() * 2));
    let dims = complete_lattice(words, max_allowed, max_w, dims)?;
    let lat = gen_lattice(&dims);
    let set: BTreeSet<i64> = lat.iter().copied().collect();
    if set.len() == lat.len()
        && lat.iter().all(|&x| x >= 0 && x <= max_w)
        && words.iter().all(|w| set.contains(w))
    {
        Some(lat)
    } else {
        None
    }
}

fn factor_lattice(block: &[i64], step_limit: i64, max_dims: usize) -> Option<Vec<(i64, i64)>> {
    if block.len() <= 1 {
        return Some(vec![(32, 1)]);
    }
    let mut cur = block.to_vec();
    let mut dims = Vec::new();
    while cur.len() > 1 {
        if dims.len() >= max_dims {
            return None;
        }
        let s0 = cur[1] - cur[0];
        if s0 <= 0 || !(-step_limit <= s0 && s0 < step_limit) || s0 % 32 != 0 {
            return None;
        }
        let mut max_r = 2;
        while max_r < cur.len() && cur[max_r] - cur[max_r - 1] == s0 {
            max_r += 1;
        }
        let mut r = max_r;
        while r >= 2 {
            if cur.len().is_multiple_of(r)
                && cur
                    .chunks(r)
                    .all(|c| c.windows(2).all(|w| w[1] - w[0] == s0))
            {
                break;
            }
            r -= 1;
        }
        if r < 2 {
            return None;
        }
        dims.push((s0, r as i64));
        cur = cur.into_iter().step_by(r).collect();
    }
    Some(dims)
}

impl Tile {
    fn new(elements: &[i64], k: &Knobs) -> Self {
        let raw_words: Vec<i64> = elements
            .iter()
            .filter(|&&x| x >= 0)
            .map(|x| x / WORD_LANES)
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        let anchor = raw_words.first().copied().unwrap_or(0);
        let rel_bytes: Vec<i64> = raw_words.iter().map(|&w| (w - anchor) * 32).collect();
        let lattice = if crate::mutant::on("l2.readmap/no-lattice") {
            None
        } else {
            covering_lattice(&rel_bytes, MAX_DIMS, k)
        };
        let word_bytes = lattice.unwrap_or(rel_bytes);
        let index: BTreeMap<_, _> = word_bytes
            .iter()
            .enumerate()
            .map(|(i, &wb)| (anchor + wb / 32, i))
            .collect();
        let mut segments = Vec::new();
        for (chunk, row) in elements.chunks(WORD_LANES as usize).enumerate() {
            let mut start = 0;
            let mut part = Vec::new();
            while start < row.len() {
                let a = row[start];
                let broadcast = a >= 0 && row.get(start + 1) == Some(&a);
                let mut end = start + 1;
                while end < row.len()
                    && if a < 0 {
                        row[end] < 0
                    } else {
                        row[end] >= 0
                            && row[end] / WORD_LANES == a / WORD_LANES
                            && row[end] == a + if broadcast { 0 } else { (end - start) as i64 }
                    }
                {
                    end += 1;
                }
                let single_lane =
                    a >= 0 && end == start + 1 && !crate::mutant::on("l2.readmap/broadcast-phase");
                let use_bcast = broadcast || single_lane;
                let source = (a >= 0).then(|| {
                    (
                        index[&(a / WORD_LANES)],
                        if use_bcast {
                            (a % WORD_LANES) as usize
                        } else {
                            (a - start as i64).rem_euclid(WORD_LANES) as usize
                        },
                        use_bcast,
                    )
                });
                part.push(Segment { chunk, end, source });
                start = end;
            }
            segments.extend(part.into_iter().rev());
        }
        Self {
            anchor: anchor * 32,
            shape: Shape {
                words: word_bytes,
                segments,
            },
        }
    }
}

impl Shape {
    /// Bytes from the first fetched word to the end of the last. A tile of no
    /// source words still walks one word (`descriptor_transfers`).
    pub fn extent(&self) -> i64 {
        self.words.last().map_or(32, |w| w + 32)
    }

    /// Maximal contiguous words, then their common transfer size. A descriptor
    /// can gather these equal-sized runs by changing only its byte increment.
    pub fn transfers(&self) -> (usize, Vec<(usize, i64)>) {
        let mut runs = Vec::new();
        let mut i = 0;
        while i < self.words.len() {
            let start = i;
            i += 1;
            while i < self.words.len() && self.words[i] == self.words[i - 1] + 32 {
                i += 1;
            }
            runs.push((start, i - start));
        }
        let unit = runs
            .iter()
            .fold(0, |mut a, &(_, mut b)| {
                while b != 0 {
                    let r = a % b;
                    a = b;
                    b = r;
                }
                a
            })
            .max(1);
        (
            unit,
            runs.into_iter()
                .flat_map(|(a, n)| (a..a + n).step_by(unit).map(|i| (i, self.words[i])))
                .collect(),
        )
    }

    /// A descriptor's walk, the words of a transfer and each transfer's
    /// `(L1 position, byte offset)`: one walk of a lattice block where the
    /// tile repeats one, else its contiguous runs.
    pub fn descriptor_transfers(&self, t: &Target, gather_words: usize) -> DescriptorTransfers {
        if self.words.is_empty() {
            return (vec![(32, 1)], 1, vec![(0, 0)]);
        }
        if crate::mutant::on("l2.readmap/no-lattice-transfers") {
            let (unit, transfers) = self.transfers();
            return (vec![(32, unit as i64)], unit, transfers);
        }
        let step_limit = t.step();
        let n = self.words.len();
        let walk = t.machine.vc.walk_max as usize;
        for unit in (2..=n.min(walk)).rev() {
            if !n.is_multiple_of(unit) {
                continue;
            }
            let Some(dims) = factor_lattice(&self.words[..unit], step_limit, MAX_DIMS) else {
                continue;
            };
            if dims[0].0 == 32 && dims[0].1 > walk as i64 {
                continue;
            }
            if dims[0].0 != 32 && unit > gather_words {
                continue;
            }
            let ok = self
                .words
                .chunks(unit)
                .skip(1)
                .all(|c| factor_lattice(c, step_limit, MAX_DIMS).as_ref() == Some(&dims));
            if ok {
                let transfers = (0..n).step_by(unit).map(|i| (i, self.words[i])).collect();
                return (dims, unit, transfers);
            }
        }
        let (unit, transfers) = self.transfers();
        (vec![(32, unit as i64)], unit, transfers)
    }

    pub fn unpack(&self, slot: usize, lines: &mut Vec<String>) {
        for (reg, start) in (0..self.words.len()).step_by(REG_WORDS).enumerate() {
            lines.push(format!(
                "    vunpk v{} <- l1[{}] f16 n={}",
                SOURCE_REG + reg,
                slot + start,
                (self.words.len() - start).min(REG_WORDS)
            ));
        }
        lines.push("    vsync fe on=unpack".into());
    }

    pub fn rearrange(&self, dest: usize, pipeline: bool, lines: &mut Vec<String>) {
        let mut chunks: Vec<Vec<&Segment>> = (0..REG_WORDS)
            .map(|c| self.segments.iter().filter(|s| s.chunk == c).collect())
            .collect();
        let mut copies = Vec::new();
        let mut assembled: BTreeMap<ChunkKey, usize> = BTreeMap::new();
        if pipeline {
            for (chunk, c_segs) in chunks.iter_mut().enumerate() {
                if c_segs.is_empty() {
                    continue;
                }
                let key: Vec<_> = c_segs.iter().map(|s| (s.end, s.source)).collect();
                if let Some(&from) = assembled.get(&key) {
                    copies.push((chunk, from));
                    c_segs.clear();
                } else {
                    assembled.insert(key, chunk);
                }
            }
        }
        let depth = chunks.iter().map(Vec::len).max().unwrap_or(0);
        for layer in 0..depth {
            let at = |chunk: usize| chunks.get(chunk).and_then(|c| c.get(layer)).copied();
            let mut ops = Vec::new();
            let mut chunk = 0;
            while chunk < REG_WORDS {
                let Some(s) = at(chunk) else {
                    chunk += 1;
                    continue;
                };
                let mut n = 1;
                let sequential = if let (Some((a, k, b)), Some(next)) = (s.source, at(chunk + 1)) {
                    next.source == Some((a + 1, k, b)) && a / REG_WORDS == (a + 1) / REG_WORDS
                } else {
                    false
                };
                if s.end == WORD_LANES as usize {
                    while chunk + n < REG_WORDS {
                        let Some(next) = at(chunk + n) else {
                            break;
                        };
                        let wanted = s
                            .source
                            .map(|(a, k, b)| (a + if sequential { n } else { 0 }, k, b));
                        if next.end != WORD_LANES as usize
                            || next.source != wanted
                            || s.source.is_some_and(|(a, _, _)| {
                                sequential && a / REG_WORDS != (a + n) / REG_WORDS
                            })
                        {
                            break;
                        }
                        n += 1;
                    }
                }
                let xbar_key = match s.source {
                    None => (0u8, 0usize),
                    Some((_, k, true)) => (3, k),
                    Some((_, 0, false)) => (1, 0),
                    Some((_, k, false)) => (2, k),
                };
                let source = match s.source {
                    None => "k0".to_string(),
                    Some((i, k, bcast)) => {
                        let r = format!(
                            "v{}[{}{}]",
                            SOURCE_REG + i / REG_WORDS,
                            i % REG_WORDS,
                            if sequential && n > 1 { ":" } else { "" }
                        );
                        if bcast {
                            format!(
                                "bcast({r}, lane={k}, sh={})",
                                if crate::mutant::on("l2.readmap/broadcast-phase") {
                                    0
                                } else {
                                    3
                                }
                            )
                        } else if k == 0 {
                            r
                        } else {
                            format!("rot({r}, {k})")
                        }
                    }
                };
                let vl = if n > 1 {
                    n * WORD_LANES as usize
                } else {
                    s.end
                };
                let line = format!(
                    "    vsel v{dest}[{}{}], k1, {source}, k0 vl={vl}",
                    s.chunk,
                    if n > 1 { ":" } else { "" },
                );
                ops.push((vl, xbar_key, s.chunk, line));
                chunk += n;
            }
            if pipeline {
                ops.sort_by_key(|&(vl, xk, ch, _)| (vl, xk, ch));
                if layer % 2 == 1 {
                    ops.reverse();
                }
            }
            for (_, _, _, line) in ops {
                lines.push(line);
            }
            if !pipeline {
                lines.push("    vsync fe on=math".into());
            }
        }
        for (chunk, from) in copies {
            lines.push(format!("    vmov v{dest}[{chunk}], v{dest}[{from}] vl=16"));
        }
    }
}
