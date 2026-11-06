//! The fill throttle (L2.VC.027) on descriptors set outside the image.

use ktpu_l2::lower::fills::throttle;
use ktpu_l2::target::Target;

use super::common::card;

/// An image of fills with no sync, the `i`-th through `a{reg(i)}`.
fn fills(n: usize, reg: impl Fn(usize) -> usize) -> String {
    let mut text = String::from("image held()\n");
    for i in 0..n {
        text += &format!("    vfill a{} -> l1[{}]\n", reg(i), 8 * i);
    }
    text
}

#[test]
fn fills_through_another_outside_descriptor_are_held_to_the_queue() {
    let target = Target::of(&card());
    let depth = target.fill_track_depth as usize;
    // Past the queue's depth a fill through a1 takes the entry of one through
    // a0, whose length may differ: it waits for a landing.
    let mixed = fills(depth + 1, |i| usize::from(i >= depth));
    assert!(throttle(mixed, &target)
        .expect("throttle")
        .contains("vsync fe on=fill"));
    // Through one register every entry it takes holds the same length.
    let same = fills(depth + 1, |_| 0);
    assert_eq!(throttle(same.clone(), &target).expect("throttle"), same);
}
