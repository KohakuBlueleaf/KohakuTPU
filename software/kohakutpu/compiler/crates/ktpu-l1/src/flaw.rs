//! Silicon flaw F-001 (`docs/spec/stack/flaws.md`): a one-beat K_RD write
//! burst chained off the burst before writes the word before its own.
//!
//! The mover cuts a maximal contiguous destination run into bursts at its cap
//! and at every 4 KB page: `b = min(left, cap, 128 - word % 128)`, and no
//! compile knows a binding's page offset. A walk whose every run starts an even
//! word from a write-line base (`dram.line_bytes`, an even word count) and
//! holds an even number of words, under an even cap, cuts into even bursts at
//! any page offset. A command of one word is its own first burst, which the
//! mover admits from idle and never chains.
//!
//! [`need`] is that proof over one encoded mover command (L1.MOV.019), which
//! every producer of mover writes reaches at `move`; [`split`] cuts a command
//! it does not prove into commands it does.

use std::collections::HashSet;

use ktpu_isa::unit::mover::{Move, MoveMode, Walk, BURST_LSB, FLAG_WCOAL, MOVE_WORDS};

/// Levels of one step dropped (a walk keeps at least one).
fn squeeze(levels: &[(i64, i64)]) -> Vec<(i64, i64)> {
    let out: Vec<(i64, i64)> = levels.iter().copied().filter(|l| l.0 != 1).collect();
    match (out.is_empty(), levels.last()) {
        (true, Some(l)) => vec![*l],
        _ => out,
    }
}

/// Adjacent levels one walk steps as one merged.
fn merge(levels: &[(i64, i64)]) -> Vec<(i64, i64)> {
    let mut out: Vec<(i64, i64)> = Vec::new();
    for &(n, s) in levels.iter().rev() {
        match out.last() {
            Some(&(m, t)) if t * m == s => {
                out.pop();
                out.push((n * m, t));
            }
            _ => out.push((n, s)),
        }
    }
    out.reverse();
    out
}

/// Whether one mover command writing `levels` from byte `off` of its buffer
/// makes no one-beat write burst at any page offset the binding contract
/// allows (`line` bytes, the base alignment; `cap` words, the burst cap).
pub fn burst_safe(off: i64, levels: &[(i64, i64)], word: i64, line: i64, cap: i64) -> bool {
    if levels.iter().map(|l| l.0).product::<i64>() == 1 {
        return true;
    }
    let even = 2 * word;
    if line % even != 0 || cap % 2 != 0 {
        return false;
    }
    let lv = merge(&squeeze(levels));
    let (n, s) = lv[lv.len() - 1];
    let (run, outer) = if s == word {
        (n, &lv[..lv.len() - 1])
    } else {
        (1, &lv[..])
    };
    run % 2 == 0 && off % even == 0 && outer.iter().all(|&(n, s)| n == 1 || s % even == 0)
}

/// What one mover command needs to be free of F-001.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Need {
    /// Nothing: it writes no read data, or one word.
    Free,
    /// Its write base on a write line: the proof holds from there.
    Line,
    /// No proof.
    Unproven,
}

/// The machine facts the proof reads: word and write-line bytes, burst cap
/// in words.
#[derive(Clone, Copy, Debug)]
pub struct Facts {
    pub word: i64,
    pub line: i64,
    pub cap: i64,
}

impl Facts {
    /// The write engine's cap for this command: its encoded cap (flags[7:5],
    /// 0 the machine's), one word with coalescing (flags[3]) off.
    fn command(self, mv: &Move) -> Self {
        let encoded = (mv.flags >> BURST_LSB) & 7;
        let cap = if mv.flags & FLAG_WCOAL == 0 {
            1
        } else if encoded == 0 {
            self.cap
        } else {
            1i64 << (encoded - 1)
        };
        Self { cap, ..self }
    }
}

fn plain(w: &Walk) -> Option<Vec<(i64, i64)>> {
    let plain = w.windows == [(0, 0); 2] && w.dims.iter().all(|l| l.axis == 0);
    plain.then(|| w.dims.iter().map(|l| (l.count, l.stride)).collect())
}

/// Whether `mv` writes read data through the flawed FIFO: copies, gathers and
/// scatters do; fills, generators and transforms do not.
pub fn reads(mv: &Move) -> bool {
    matches!(
        mv.mode,
        MoveMode::Copy | MoveMode::Gather | MoveMode::Scatter
    )
}

/// What `mv` needs (L1.MOV.019).
pub fn need(mv: &Move, f: Facts) -> Need {
    let f = f.command(mv);
    let (Some(dst), true) = (&mv.dst, reads(mv)) else {
        return Need::Free;
    };
    let even = 2 * f.word;
    let off = dst.base.rem_euclid(f.line);
    if mv.mode == MoveMode::Scatter {
        let (_, n) = mv.index.unwrap_or((0, 1));
        let (pitch, words) = mv.row.unwrap_or((0, 1));
        let even_rows = words % 2 == 0 && pitch % even == 0 && off % even == 0;
        return match (
            n * words == 1,
            even_rows && f.line % even == 0 && f.cap % 2 == 0,
        ) {
            (true, _) => Need::Free,
            (false, true) => Need::Line,
            (false, false) => Need::Unproven,
        };
    }
    match plain(dst) {
        Some(lv) if lv.iter().map(|l| l.0).product::<i64>() == 1 => Need::Free,
        Some(lv) if burst_safe(off, &lv, f.word, f.line, f.cap) => Need::Line,
        _ => Need::Unproven,
    }
}

/// Each element of `w` in walk order: its address and whether its windows
/// hold it (ISA.MV.026).
fn elements(w: &Walk) -> Vec<(i64, bool)> {
    let mut out = vec![(w.base, [w.windows[0].0, w.windows[1].0])];
    for l in &w.dims {
        let mut next = Vec::with_capacity(out.len() * l.count.max(1) as usize);
        for (at, axes) in &out {
            for i in 0..l.count.max(1) {
                let mut a = *axes;
                if l.axis > 0 {
                    a[l.axis as usize - 1] += i * l.astep;
                }
                next.push((at + i * l.stride, a));
            }
        }
        out = next;
    }
    let inside =
        |a: &[i64; 2]| (0..2).all(|k| w.windows[k].1 == 0 || (0..w.windows[k].1).contains(&a[k]));
    out.into_iter().map(|(at, a)| (at, inside(&a))).collect()
}

/// One piece of a split: the command, what it needs, and how far its source
/// base and index list moved from the original's (a register patch of one of
/// those fields keeps its register, its constant shifted).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Piece {
    pub mv: Move,
    pub need: Need,
    pub src_shift: i64,
    pub index_shift: i64,
}

/// A contiguous stretch: `len` words from source `s` (None: padding) to
/// destination `d`; for a gather or scatter its row and the row's first word.
#[derive(Clone, Copy)]
struct Run {
    s: Option<i64>,
    d: i64,
    len: i64,
    row: i64,
    w0: i64,
}

impl Run {
    /// The first `n` words, `self` then the rest.
    fn take(&mut self, n: i64, word: i64) -> Run {
        let head = Run { len: n, ..*self };
        self.s = self.s.map(|s| s + n * word);
        self.d += n * word;
        self.w0 += n;
        self.len -= n;
        head
    }
}

/// `runs` as stretches each proven or one word: a one-word head where a run
/// starts on an odd word, even bodies of at most a move, a one-word tail.
/// `parity` is a run's start in its line, None where no compile knows it.
fn cut_even(runs: Vec<Run>, word: i64, parity: impl Fn(&Run) -> Option<i64>) -> Vec<Run> {
    let most = (MOVE_WORDS as i64) & !1;
    let mut out = Vec::new();
    for mut r in runs {
        let Some(at) = r.s.and(parity(&r)) else {
            if r.s.is_none() {
                out.push(r);
            }
            while r.s.is_some() && r.len > 0 {
                out.push(r.take(1, word));
            }
            continue;
        };
        if at % (2 * word) != 0 {
            out.push(r.take(1, word));
        }
        while r.len >= 2 {
            let n = (r.len & !1).min(most);
            out.push(r.take(n, word));
        }
        if r.len == 1 {
            out.push(r.take(1, word));
        }
    }
    out
}

/// The words the pieces after a body write and read; `opaque` where one's
/// addresses are not compile-time (an index's rows, a register's source).
#[derive(Default)]
struct Touched {
    writes: HashSet<i64>,
    reads: HashSet<i64>,
    opaque: bool,
    any: bool,
}

/// A piece's written and read word addresses, None where not compile-time.
fn touches(p: &Piece, word: i64, src_fixed: bool) -> Option<(Vec<i64>, Vec<i64>)> {
    if matches!(p.mv.mode, MoveMode::Gather | MoveMode::Scatter) {
        return None;
    }
    let w = writes(&p.mv, word);
    let reads: Vec<i64> = w.iter().filter_map(|x| x.1.map(|s| s.1)).collect();
    (src_fixed || reads.is_empty()).then(|| (w.iter().map(|x| x.0 .1).collect(), reads))
}

impl Touched {
    fn add(&mut self, p: &Piece, word: i64, src_fixed: bool) {
        self.any = true;
        match touches(p, word, src_fixed) {
            Some((w, r)) => {
                self.writes.extend(w);
                self.reads.extend(r);
            }
            None => self.opaque = true,
        }
    }

    /// Whether `p` may run before these pieces: it writes no word they write
    /// or read, and reads no word they write.
    fn past(&self, p: &Piece, word: i64, src_fixed: bool) -> bool {
        if !self.any {
            return true;
        }
        let Some((w, r)) = touches(p, word, src_fixed).filter(|_| !self.opaque) else {
            return false;
        };
        w.iter()
            .all(|a| !self.writes.contains(a) && !self.reads.contains(a))
            && r.iter().all(|a| !self.writes.contains(a))
    }
}

/// `mv`, which [`need`] does not prove, as pieces each of which it does
/// (L1.MOV.019): copies by contiguous stretches, gathers and scatters a row
/// at a time, each stretch an even body between one-word ends, a piece merged
/// into the body before it wherever the merge is proven and the pieces between
/// touch nothing it does. `src_fixed`: the source base is an address, not a
/// register's.
pub fn split(mv: &Move, f: Facts, src_fixed: bool) -> Vec<Piece> {
    let f = f.command(mv);
    let word = f.word;
    let one = Walk::new(0, &[(1, word)]);
    let dst = mv.dst.clone().unwrap_or_else(|| one.clone());
    let src = mv.src.clone().unwrap_or(one);
    let (_, n) = mv.index.unwrap_or((0, 1));
    let (pitch, words) = mv.row.unwrap_or((0, 1));
    let mut runs: Vec<Run> = Vec::new();
    let mut push = |s: Option<i64>, d: i64, row: i64, w0: i64| {
        if let Some(r) = runs.last_mut() {
            let next = r.s.map(|x| x + r.len * word);
            if r.row == row && r.d + r.len * word == d && next == s {
                r.len += 1;
                return;
            }
        }
        runs.push(Run {
            s,
            d,
            len: 1,
            row,
            w0,
        });
    };
    match mv.mode {
        MoveMode::Copy => {
            let from = elements(&src);
            for (k, (d, inside)) in elements(&dst).into_iter().enumerate() {
                let (s, held) = from[k % from.len()];
                if inside {
                    push(held.then_some(s), d, 0, 0);
                }
            }
        }
        MoveMode::Gather => {
            let to = elements(&dst);
            for (r, row) in to.chunks(words as usize).take(n as usize).enumerate() {
                for (w, (d, inside)) in row.iter().enumerate() {
                    if *inside {
                        push(Some(src.base + w as i64 * word), *d, r as i64, w as i64);
                    }
                }
            }
        }
        _ => {
            let from = elements(&src);
            for (r, row) in from.chunks(words as usize).take(n as usize).enumerate() {
                for (w, (s, _)) in row.iter().enumerate() {
                    push(Some(*s), dst.base + w as i64 * word, r as i64, w as i64);
                }
            }
        }
    }
    let provable = f.line % (2 * word) == 0 && f.cap % 2 == 0;
    // A scatter row's start keeps its table base's parity only at an even pitch.
    let known = provable && (mv.mode != MoveMode::Scatter || pitch % (2 * word) == 0);
    let parity = |r: &Run| known.then_some(r.d.rem_euclid(f.line));
    let mut out: Vec<Piece> = Vec::new();
    let mut body: Option<usize> = None;
    let mut after = Touched::default();
    for r in cut_even(runs, word, parity) {
        let p = piece(mv, &src, &dst, &r, word, f);
        let past = after.past(&p, word, src_fixed);
        if let Some(m) = body.filter(|_| past).and_then(|b| merged(&out[b], &p, f)) {
            out[body.unwrap()] = m;
            continue;
        }
        if p.need == Need::Line {
            body = Some(out.len());
            after = Touched::default();
        } else {
            after.add(&p, word, src_fixed);
        }
        out.push(p);
    }
    out.into_iter().map(|p| on_word(p, word)).collect()
}

/// `p` with its index list on a word (ISA.MV.054): a gather piece off one reads
/// the list from that word, the rows before its own masked by an axis window.
/// A masked element is K_SKIP (`mm_mover.v` `lt_kind`): no read, no write, so
/// the command's bursts and index faults are the unmasked piece's.
fn on_word(mut p: Piece, word: i64) -> Piece {
    let (MoveMode::Gather, Some((at, n))) = (p.mv.mode, p.mv.index) else {
        return p;
    };
    let skip = at.rem_euclid(word) / 4;
    if skip == 0 {
        return p;
    }
    let dst = p.mv.dst.take().unwrap();
    let (_, stride, len) = shape(&dst).unwrap();
    let mut rows = Walk::new(dst.base - skip * stride, &[(skip + n, stride), (len, word)]);
    rows.dims[0].axis = 1;
    rows.dims[0].astep = 1;
    rows.windows[0] = (-skip, n);
    p.mv.dst = Some(rows);
    p.mv.index = Some((at - 4 * skip, skip + n));
    p.index_shift -= 4 * skip;
    p
}

/// The command for one stretch.
fn piece(mv: &Move, src: &Walk, dst: &Walk, r: &Run, word: i64, f: Facts) -> Piece {
    let run = |base: i64| Walk::new(base, &[(r.len, word)]);
    let mut p = mv.clone();
    let (mut src_shift, mut index_shift) = (0, 0);
    match (mv.mode, r.s) {
        (MoveMode::Copy, None) => {
            p.mode = MoveMode::Fill;
            p.imm = Some(mv.imm.unwrap_or(0));
            p.src = None;
            p.dst = Some(run(r.d));
        }
        (MoveMode::Copy, Some(s)) => {
            p.src = Some(run(s));
            p.dst = Some(run(r.d));
            src_shift = s - src.base;
        }
        (MoveMode::Gather, _) => {
            p.src = Some(Walk {
                base: src.base + r.w0 * word,
                ..src.clone()
            });
            p.dst = Some(run(r.d));
            p.index = mv.index.map(|(i, _)| (i + 4 * r.row, 1));
            p.row = mv.row.map(|(pitch, _)| (pitch, r.len));
            src_shift = r.w0 * word;
            index_shift = 4 * r.row;
        }
        (_, s) => {
            let s = s.unwrap_or(src.base);
            p.src = Some(run(s));
            p.dst = Some(Walk {
                base: r.d,
                ..dst.clone()
            });
            p.index = mv.index.map(|(i, _)| (i + 4 * r.row, 1));
            p.row = mv.row.map(|(pitch, _)| (pitch, r.len));
            src_shift = s - src.base;
            index_shift = 4 * r.row;
        }
    }
    Piece {
        need: need(&p, f),
        mv: p,
        src_shift,
        index_shift,
    }
}

/// A one- or two-level walk as (outer count, outer stride, run words).
fn shape(w: &Walk) -> Option<(i64, i64, i64)> {
    match w.dims.as_slice() {
        [run] => Some((1, 0, run.count)),
        [o, run] => Some((o.count, o.stride, run.count)),
        _ => None,
    }
}

/// `a` then `b` as one command: `b` one more step of `a`'s outer level (the
/// next stretch of a copy, the next row of a gather or scatter), proven.
fn merged(a: &Piece, b: &Piece, f: Facts) -> Option<Piece> {
    let (am, bm) = (&a.mv, &b.mv);
    if am.mode != bm.mode || am.mode == MoveMode::Fill || b.need == Need::Free {
        return None;
    }
    let (asrc, adst, bsrc, bdst) = (
        am.src.as_ref()?,
        am.dst.as_ref()?,
        bm.src.as_ref()?,
        bm.dst.as_ref()?,
    );
    let next_row = am.index.map(|(i, n)| i + 4 * n) == bm.index.map(|(i, _)| i);
    let fixed_side = match am.mode {
        MoveMode::Gather => asrc.base == bsrc.base,
        MoveMode::Scatter => adst.base == bdst.base,
        _ => true,
    };
    if am.mode != MoveMode::Copy && !(am.row == bm.row && next_row && fixed_side) {
        return None;
    }
    let step = |a: &Walk, b: &Walk| -> Option<Walk> {
        let (n, stride, len) = shape(a)?;
        let (bn, _, blen) = shape(b)?;
        let d = b.base - a.base;
        if bn != 1 || blen != len || (n > 1 && d != n * stride) {
            return None;
        }
        Some(Walk::new(
            a.base,
            &[(n + 1, if n > 1 { stride } else { d }), (len, f.word)],
        ))
    };
    let mut m = am.clone();
    match am.mode {
        MoveMode::Copy => {
            m.src = Some(step(asrc, bsrc)?);
            m.dst = Some(step(adst, bdst)?);
        }
        MoveMode::Gather => {
            m.dst = Some(step(adst, bdst)?);
            m.index = am.index.map(|(i, n)| (i, n + 1));
        }
        _ => {
            m.src = Some(step(asrc, bsrc)?);
            m.index = am.index.map(|(i, n)| (i, n + 1));
        }
    }
    let walked = m
        .dst
        .as_ref()
        .map_or(0, Walk::words)
        .max(m.src.as_ref().map_or(0, Walk::words));
    (walked <= MOVE_WORDS as i64 && need(&m, f) == Need::Line).then(|| Piece { mv: m, ..a.clone() })
}

/// A written word's place and what it takes: `((index entry, address),
/// (index entry, address))`, entry -1 where no index list names it, the
/// source None for padding.
pub type Written = ((i64, i64), Option<(i64, i64)>);

/// Every word `mv` writes and what it takes: the split's witness, which
/// pieces must give word for word as the original.
pub fn writes(mv: &Move, word: i64) -> Vec<Written> {
    let one = Walk::new(0, &[(1, word)]);
    let src = mv.src.clone().unwrap_or_else(|| one.clone());
    let dst = mv.dst.clone().unwrap_or(one);
    let (i0, n) = mv.index.unwrap_or((0, 1));
    let (_, words) = mv.row.unwrap_or((0, 1));
    let mut out = Vec::new();
    match mv.mode {
        MoveMode::Fill => out.extend(
            elements(&dst)
                .into_iter()
                .filter(|e| e.1)
                .map(|(d, _)| ((-1, d), None)),
        ),
        MoveMode::Copy => {
            let from = elements(&src);
            for (k, (d, inside)) in elements(&dst).into_iter().enumerate() {
                let (s, held) = from[k % from.len()];
                if inside {
                    out.push(((-1, d), held.then_some((-1, s))));
                }
            }
        }
        MoveMode::Gather => {
            let to = elements(&dst);
            for (r, row) in to.chunks(words as usize).take(n as usize).enumerate() {
                for (w, (d, inside)) in row.iter().enumerate() {
                    if *inside {
                        let at = (i0 + 4 * r as i64, src.base + w as i64 * word);
                        out.push(((-1, *d), Some(at)));
                    }
                }
            }
        }
        _ => {
            let from = elements(&src);
            for (r, row) in from.chunks(words as usize).take(n as usize).enumerate() {
                for (w, (s, _)) in row.iter().enumerate() {
                    let at = (i0 + 4 * r as i64, dst.base + w as i64 * word);
                    out.push((at, Some((-1, *s))));
                }
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use ktpu_isa::unit::mover::{MoverIsa, W32};
    use ktpu_machine::Machine;
    use std::collections::BTreeMap;

    const W: i64 = 32;
    const F: Facts = Facts {
        word: W,
        line: 64,
        cap: 128,
    };

    #[test]
    fn even_runs_on_even_words_are_safe_at_any_page() {
        assert!(burst_safe(0, &[(8, 128), (2, 32)], W, 64, 128));
        assert!(burst_safe(64, &[(800, 32)], W, 64, 128));
    }

    #[test]
    fn an_odd_run_or_start_can_end_in_a_lone_beat() {
        // LeNet's 32 rows of 25 words 26 apart: at page word 104, row 4 is 24 + 1.
        assert!(!burst_safe(0, &[(32, 832), (25, 32)], W, 64, 128));
        assert!(!burst_safe(32, &[(4, 32)], W, 64, 128));
        assert!(!burst_safe(0, &[(4, 96), (2, 32)], W, 64, 128));
        assert!(!burst_safe(0, &[(256, 64)], W, 64, 128));
    }

    #[test]
    fn one_word_is_its_own_first_burst() {
        assert!(burst_safe(32, &[(1, 32)], W, 64, 128));
    }

    #[test]
    fn a_one_word_line_or_odd_cap_proves_nothing() {
        assert!(!burst_safe(0, &[(4, 32)], W, 32, 128));
        assert!(!burst_safe(0, &[(4, 32)], W, 64, 127));
    }

    fn copy(src: Walk, dst: Walk) -> Move {
        Move {
            mode: MoveMode::Copy,
            src: Some(src),
            dst: Some(dst),
            ewidth: W32,
            flags: FLAG_WCOAL,
            xform: (0, 0),
            imm: None,
            seed: None,
            index: None,
            row: None,
            fp: false,
        }
    }

    fn rows(mode: MoveMode, src: Walk, dst: Walk, n: i64, pitch: i64, words: i64) -> Move {
        Move {
            mode,
            index: Some((0x9000, n)),
            row: Some((pitch, words)),
            ..copy(src, dst)
        }
    }

    fn encoder() -> MoverIsa {
        MoverIsa::of(&Machine::named("multimesh_v9t8").unwrap())
    }

    /// Each written place's last writer, the writes taken in order.
    fn last(writes: &[Written]) -> BTreeMap<(i64, i64), Option<(i64, i64)>> {
        writes.iter().map(|&(at, from)| (at, from)).collect()
    }

    /// Unproven before, every piece proven and encodable after, and the pieces
    /// write exactly what the command does, each place's last writer the same.
    fn check(mv: &Move) -> Vec<Piece> {
        assert_eq!(need(mv, F), Need::Unproven, "{mv:?}");
        let pieces = split(mv, F, true);
        let isa = encoder();
        let mut got: Vec<Written> = Vec::new();
        for p in &pieces {
            assert_ne!(p.need, Need::Unproven, "{:?}", p.mv);
            if p.mv.dst.as_ref().is_some_and(|d| d.windows == [(0, 0); 2]) {
                assert_eq!(need(&p.mv, F), p.need, "{:?}", p.mv);
            }
            isa.move_writes(&p.mv).unwrap();
            got.extend(writes(&p.mv, W));
        }
        let mut want = writes(mv, W);
        assert_eq!(last(&got), last(&want));
        got.sort();
        want.sort();
        assert_eq!(got, want);
        pieces
    }

    #[test]
    fn a_body_merges_past_no_piece_writing_its_words() {
        // Destination words 0 1 2 1 2 3: the second run's body rewrites word 2
        // after the first run's tail, so it stays behind that tail.
        let dst = Walk::new(0x40000, &[(2, 32), (3, 32)]);
        let pieces = check(&copy(Walk::new(0x10000, &[(6, 32)]), dst));
        assert_eq!(pieces.len(), 4, "{pieces:?}");
    }

    #[test]
    fn a_page_tail_copy_splits_into_even_bodies_and_lone_tails() {
        // pagetail.ktpu: 32 rows of 25 words 26 apart.
        let dst = Walk::new(0x40000, &[(32, 832), (25, 32)]);
        let pieces = check(&copy(Walk::new(0x10000, &[(800, 32)]), dst));
        // Bodies of 24 merge into one command; a lone tail a row.
        assert_eq!(pieces.len(), 1 + 32, "{pieces:?}");
    }

    #[test]
    fn one_word_runs_and_an_odd_start_split() {
        check(&copy(
            Walk::new(0x10000, &[(256, 32)]),
            Walk::new(0x40000, &[(256, 64)]),
        ));
        let odd = check(&copy(
            Walk::new(0x10000, &[(6, 32)]),
            Walk::new(0x40020, &[(6, 32)]),
        ));
        assert_eq!(odd.len(), 3, "{odd:?}");
    }

    #[test]
    fn padding_outside_a_window_becomes_a_fill() {
        let mut src = Walk::new(0x10000, &[(5, 32)]);
        src.dims[0].axis = 1;
        src.dims[0].astep = 1;
        src.windows = [(0, 3), (0, 0)];
        let pieces = check(&copy(src, Walk::new(0x40020, &[(5, 32)])));
        assert!(pieces.iter().any(|p| p.mv.mode == MoveMode::Fill));
    }

    #[test]
    fn odd_rows_of_a_gather_split_a_row_at_a_time() {
        // oddrows.ktpu: two-word rows three words apart.
        let table = Walk::new(0x10000, &[(1, 32)]);
        let dst = Walk::new(0x40000, &[(12, 96), (2, 32)]);
        check(&rows(MoveMode::Gather, table.clone(), dst, 12, 64, 2));
        let dst = Walk::new(0x40000, &[(8, 128), (3, 32)]);
        check(&rows(MoveMode::Gather, table, dst, 8, 96, 3));
    }

    #[test]
    fn a_gather_row_off_an_index_word_reads_the_list_from_its_word() {
        // Two three-word rows 128 bytes apart: an even body and a lone tail a
        // row, the second row's entry 4 bytes into the list's word.
        let table = Walk::new(0x10000, &[(1, 32)]);
        let dst = Walk::new(0x40000, &[(2, 128), (3, 32)]);
        let pieces = check(&rows(MoveMode::Gather, table, dst, 2, 96, 3));
        assert_eq!(pieces.len(), 4, "{pieces:?}");
        let tail = pieces.last().unwrap();
        assert_eq!(tail.mv.index, Some((0x9000, 2)), "{tail:?}");
        assert_eq!(tail.index_shift, 0);
        assert_eq!(tail.mv.dst.as_ref().unwrap().windows[0], (-1, 1));
    }

    /// Whether a piece's index list starts off a word (ISA.MV.054): the node
    /// lowering refuses such a split (L1.MOV.019).
    fn off_word(pieces: &[Piece]) -> bool {
        pieces
            .iter()
            .any(|p| p.mv.index.is_some_and(|(at, _)| at % W != 0))
    }

    #[test]
    fn a_scatter_is_proven_only_on_even_rows_at_an_even_pitch() {
        let src = Walk::new(0x10000, &[(16, 32)]);
        let table = Walk::new(0x40000, &[(1, 32)]);
        let even = rows(MoveMode::Scatter, src.clone(), table.clone(), 8, 128, 2);
        assert_eq!(need(&even, F), Need::Line);
        // An odd-word pitch leaves every row's parity unknown: a word a command,
        // each row's entry its own list, off a word past the first.
        let odd = rows(MoveMode::Scatter, src, table.clone(), 8, 96, 2);
        assert_eq!(need(&odd, F), Need::Unproven);
        assert!(off_word(&split(&odd, F, true)));
        let src = Walk::new(0x10000, &[(24, 32)]);
        let three = rows(MoveMode::Scatter, src, table, 8, 128, 3);
        assert!(off_word(&split(&three, F, true)));
    }

    #[test]
    fn a_command_is_proven_under_its_own_burst_cap_and_coalescing() {
        let even = || {
            copy(
                Walk::new(0x10000, &[(4, 32)]),
                Walk::new(0x40000, &[(4, 32)]),
            )
        };
        assert_eq!(need(&even(), F), Need::Line);
        // burst=2 (cap 2) stays even; burst=1 (cap 1) and nocoal write a word a burst.
        let capped = |flags| Move { flags, ..even() };
        assert_eq!(need(&capped(FLAG_WCOAL | 2 << BURST_LSB), F), Need::Line);
        for flags in [FLAG_WCOAL | 1 << BURST_LSB, 0] {
            let pieces = check(&capped(flags));
            assert_eq!(pieces.len(), 4, "{pieces:?}");
            assert!(pieces.iter().all(|p| need(&p.mv, F) == Need::Free));
        }
    }

    #[test]
    fn fills_and_one_word_commands_need_nothing() {
        let mut fill = copy(Walk::new(0, &[(1, 32)]), Walk::new(0x40020, &[(9, 32)]));
        fill.mode = MoveMode::Fill;
        assert_eq!(need(&fill, F), Need::Free);
        let one = copy(Walk::new(0, &[(1, 32)]), Walk::new(0x40020, &[(1, 32)]));
        assert_eq!(need(&one, F), Need::Free);
    }
}
