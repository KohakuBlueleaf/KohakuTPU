//! Exact run selection and bounded work on already compressible suffixes.
use ka_package::build::periodic::{periodic, periodic_profiled};
use ka_package::word::Word;

#[test]
fn optimistic_gain_bound_preserves_period_head_and_increment_ties() {
    let fields: &[&[(u32, u32)]] = &[
        &[], &[(0, 8)], &[(0, 8), (8, 8)],
        &[(0, 8), (0, 8)], &[(0, 8), (0, 16)],
    ];
    // Complete enumeration includes decreases, broken runs and zero deltas.
    for length in 0..=7usize {
        for mut bits in 0..3usize.pow(length as u32) {
            let words: Vec<_> = (0..length).map(|_| {
                let value = bits % 3;
                bits /= 3;
                Word([value as u64, 0, 0, 0])
            }).collect();
            for &fs in fields {
                let expected = periodic_profiled(&words, fs, true).0;
                assert_eq!(periodic(&words, fs), expected);
            }
        }
    }
}

#[test]
fn repeated_suffixes_are_not_reextended_from_every_head() {
    let mut work = Vec::new();
    for n in [64, 256] {
        let words = vec![Word::ZERO; n];
        let (actual, extensions) = periodic_profiled(&words, &[], false);
        let (expected, old_extensions) = periodic_profiled(&words, &[], true);
        assert_eq!(actual, expected);
        assert_eq!(actual, Some((0, 1, n, vec![])));
        assert_eq!(extensions, (n - 2) as u64);
        assert!(old_extensions > extensions * 16);
        work.push(extensions);
    }
    assert!(work[1] <= 5 * work[0]);
}
