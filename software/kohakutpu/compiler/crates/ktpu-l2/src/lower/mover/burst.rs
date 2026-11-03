//! Silicon flaw F-001 (`docs/spec/stack/flaws.md`): which mover write walks
//! can make a one-beat K_RD write burst that chains off the burst before it.
//!
//! The mover cuts a maximal contiguous destination run into bursts at its cap
//! and at every 4 KB page: `b = min(left, cap, 128 - word % 128)`. A run of
//! more than one word therefore still makes a one-beat burst when it starts at
//! a page's last word or ends on a page's first word, and the compiler does
//! not know a binding's page offset. It knows the binding contract: every
//! buffer base is a whole DRAM write line (`dram.line_bytes`, 64 bytes, an
//! even word count). A walk whose every run starts on an even word from the
//! base and holds an even number of words, under an even cap, cuts into even
//! bursts at any page offset, so it makes no one-beat burst. A command of one
//! word is its own first burst, which the mover admits from idle and never
//! chains.

use super::*;

/// Whether one mover command writing `levels` from byte `off` of its buffer
/// makes no one-beat write burst at any page offset the binding contract
/// allows (`line` bytes, the base alignment; `cap` words, the burst cap).
pub fn burst_safe(off: i64, levels: &[(i64, i64)], word: i64, line: i64, cap: i64) -> bool {
    if count(levels) == 1 {
        return true;
    }
    if on("l2.mover/burst-run-only") {
        return run_words(levels, word) > 1;
    }
    let even = 2 * word;
    if line % even != 0 || cap % 2 != 0 {
        return false;
    }
    let lv = merge(&squeeze(levels));
    let (n, s) = *lv.last().unwrap();
    let (run, outer) = if s == word {
        (n, &lv[..lv.len() - 1])
    } else {
        (1, &lv[..])
    };
    run % 2 == 0 && off % even == 0 && outer.iter().all(|&(n, s)| n == 1 || s % even == 0)
}

#[cfg(test)]
mod tests {
    use super::burst_safe;

    const W: i64 = 32;

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
}
