//! The fill throttle (L2.VC.027) on hand-written images: where each sync goes,
//! and an image that needs none comes back byte for byte.

use ktpu_l2::lower::fills::throttle;
use ktpu_l2::Target;
use ktpu_machine::Machine;

fn target() -> Target {
    Target::of(&Machine::named("multimesh_v9t6").unwrap())
}

/// One fill of `head` words, then `behind` fills of one word.
fn window(head: i64, behind: usize) -> String {
    let fills: String = (0..behind)
        .map(|i| format!("    vfill a1 -> l1[{}] rel\n", 256 + i))
        .collect();
    format!(
        "image over(x, y)\n    desc a0 = x walk=(32, {head})\n    desc a1 = y walk=(32, 1)\n    \
         ainc a1 += 32\n    vfill a0 -> l1[0]\n{fills}    vsync fe on=fill\n    halt"
    )
}

fn thrown(image: &str) -> String {
    throttle(image.to_string(), &target()).unwrap()
}

/// The slacks of the syncs `image` gained, each with the fills before it.
fn gained(image: &str) -> Vec<(usize, i64)> {
    let mut fills = 0;
    let mut got = Vec::new();
    for line in thrown(image).lines().map(str::trim) {
        if line.starts_with("vfill ") {
            fills += 1;
        } else if let Some(s) = line.strip_prefix("vsync fe on=fill slack=") {
            got.push((fills, s.parse().unwrap()));
        }
    }
    got
}

#[test]
fn a_different_word_count_keeps_its_entry_at_the_depth() {
    let depth = target().fill_track_depth as usize;
    let fits = window(256, depth - 1);
    assert_eq!(thrown(&fits), fits);
    assert_eq!(gained(&window(256, depth)), [(depth, depth as i64 - 1)]);
}

#[test]
fn equal_word_counts_run_to_the_wrap_limit() {
    let depth = target().fill_track_depth as usize;
    let fits = window(1, 2 * depth - 2);
    assert_eq!(thrown(&fits), fits);
    assert_eq!(
        gained(&window(1, 2 * depth - 1)),
        [(2 * depth - 1, 2 * depth as i64 - 2)]
    );
}

#[test]
fn a_sync_on_the_landing_clears_the_window() {
    let image = "image over(y)\n    desc a1 = y walk=(32, 1)\n    seti s0 = 100\n    loop s0\n        \
                 vfill a1 -> l1[0] rel\n        vfill a1 -> l1[8] rel\n        vsync fe on=fill\n    halt";
    assert_eq!(thrown(image), image);
}

#[test]
fn a_loop_holds_its_sync_in_the_body() {
    let image =
        "image over(y)\n    desc a1 = y walk=(32, 1)\n    seti s0 = 100\n    loop s0\n        \
                 vfill a1 -> l1[0] rel\n    halt";
    let got = thrown(image);
    assert_eq!(got.matches("vsync").count(), 1, "{got}");
    assert!(
        got.contains("\n        vsync fe on=fill slack=30\n"),
        "{got}"
    );
}

#[test]
fn an_unset_count_is_taken_past_the_limit() {
    let image = "image over(y, trips)\n    desc a1 = y walk=(32, 1)\n    seti s0 = trips\n    loop s0\n        \
                 vfill a1 -> l1[0] rel\n    halt";
    assert_eq!(thrown(image).matches("vsync").count(), 1);
}

#[test]
fn a_queue_sync_chain_counts_as_a_landing() {
    let image =
        "image over(y)\n    desc a1 = y walk=(32, 1)\n    seti s0 = 100\n    loop s0\n        \
                 vsync fill on=unpack slack=0\n        vfill a1 -> l1[0] rel\n        \
                 vsync unpack on=fill\n        vunpk v0 <- l1[0] f16\n    halt";
    assert_eq!(thrown(image), image);
}

#[cfg(feature = "mutants")]
#[test]
fn the_mutant_drops_every_sync() {
    use ktpu_l2::mutant::{scoped, State};
    let image = window(256, 40);
    let state = State {
        active: Some("l2.fill/no-throttle"),
        ..Default::default()
    };
    assert_ne!(thrown(&image), image);
    assert_eq!(scoped(state, || thrown(&image)), image);
}
