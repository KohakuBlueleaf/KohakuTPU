//! The longest run of a word stream that repeats with only given fields
//! stepping by a constant: one REPEAT instead of its words.

use crate::word::{mask64, Word, WordOps};

/// The longest template tried: a K step is 3 words, a vector RUN a handful,
/// and the search is quadratic in the kick's length.
pub const MAX_PERIOD: usize = 16;

/// `(index, bit, width, delta)`: repetition r adds r * delta to the field.
pub type Increment = (usize, u32, u32, u64);

/// A run: `(head, period, repeats, increments)`.
pub type Run = (usize, usize, usize, Vec<Increment>);

/// The run saving the most payload slots, or None when no run saves any.
pub fn periodic(words: &[Word], fields: &[(u32, u32)]) -> Option<Run> {
    periodic_profiled(words, fields, false).0
}

/// Exact search reference and extension-work counter for stage witnesses.
/// Exhaustive mode disables only the optimistic payload-saving bound.
#[doc(hidden)]
pub fn periodic_profiled(
    words: &[Word], fields: &[(u32, u32)], exhaustive: bool,
) -> (Option<Run>, u64) {
    let mut extensions = 0;
    let mut rest = Word::ZERO.not();
    for &(bit, width) in fields {
        rest = rest.cleared(bit, width);
    }
    let differs = |a: &Word, b: &Word| !(a.xor(*b) & rest).is_zero();
    let mut best = None;
    let mut saved: i64 = 0;
    let n = words.len();
    for period in 1..=MAX_PERIOD.min(n / 2) {
        for head in 0..=(n - 2 * period) {
            // Even a perfect suffix cannot save more than all its complete
            // repetitions minus the template, with zero increment overhead.
            // Later heads have no larger bound. Equality cannot replace the
            // incumbent because the choice below requires strict improvement.
            let upper = ((n - head) / period - 1) * period;
            if !exhaustive && upper as u128 <= saved as u128 {
                break;
            }
            if differs(&words[head], &words[head + period]) {
                continue;
            }
            let mut incs: Vec<Increment> = Vec::new();
            let mut ok = true;
            'pairs: for j in 0..period {
                let (a, b) = (&words[head + j], &words[head + period + j]);
                if differs(a, b) {
                    ok = false;
                    break;
                }
                for &(bit, width) in fields {
                    let (fa, fb) = (a.get(bit, width), b.get(bit, width));
                    if fb < fa {
                        ok = false;
                        break 'pairs;
                    }
                    if fb > fa {
                        incs.push((j, bit, width, fb - fa));
                    }
                }
            }
            if !ok {
                continue;
            }
            let mut repeats = 2;
            while head + (repeats + 1) * period <= n {
                extensions += 1;
                let base = head + repeats * period;
                let mut good = (0..period).all(|j| !differs(&words[base + j], &words[head + j]));
                for j in 0..period {
                    for &(bit, width) in fields {
                        let d = incs
                            .iter()
                            .find(|&&(jj, bb, _, _)| jj == j && bb == bit)
                            .map_or(0, |i| i.3);
                        let want =
                            words[head + j].get(bit, width) as u128 + repeats as u128 * d as u128;
                        good = good && words[base + j].get(bit, width) as u128 == want;
                    }
                }
                if !good {
                    break;
                }
                repeats += 1;
            }
            let gain = ((repeats - 1) * period) as i64 - (incs.len() as i64 + 1) / 2;
            if gain > saved {
                best = Some((head, period, repeats, incs));
                saved = gain;
            }
        }
    }
    (best, extensions)
}

/// `w` with field `[bit, bit+width)` plus `add`, modulo 2^width.
pub fn stepped(w: Word, bit: u32, width: u32, add: u64) -> Word {
    let mut out = w;
    out.set(
        bit,
        width,
        w.get(bit, width).wrapping_add(add) & mask64(width),
    );
    out
}
