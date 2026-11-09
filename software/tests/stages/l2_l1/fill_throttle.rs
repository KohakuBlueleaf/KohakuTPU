//! The fill throttle (L2.VC.027) on descriptors set outside the image.

use ktpu_l1::text::{read, Stmt, Term};
use ktpu_l2::lower::fills::{throttle, throttled, throttled_structure};
use ktpu_l2::lower::vector::{encode, gen::print};
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

#[test]
fn structural_fit_inserts_the_same_waits_without_printing_trial_images() {
    let target = Target::of(&card());
    let depth = target.fill_track_depth as usize;
    let source = fills(depth + 1, |i| usize::from(i >= depth));
    let mut reference = read(&source, "held").expect("fill image");
    reference
        .images
        .get_mut("held")
        .expect("image")
        .stmts
        .push(Stmt::new("halt", vec![]));
    // Retain a source-based oracle with the same terminating instruction.
    reference.source.push_str("    halt\n");
    let baseline = throttled(&reference, &target).expect("source throttle");
    let definition = &reference.images["held"];
    let before = print::emitted();
    let structured = print::structured_image(
        "held",
        definition.params.clone(),
        definition.stmts.clone(),
        reference.vregs,
    )
    .expect("structured image");
    let trial = throttled_structure(&structured, &target);
    let words = encode::words_of(&trial, "held", 0, &target.machine).expect("structural fit");
    assert_eq!(
        print::emitted(),
        before,
        "construction, throttling and fit do not spell trial operands"
    );
    assert_eq!(
        words,
        encode::words_of(&baseline, "held", 0, &target.machine).expect("source fit")
    );
    let mut winner = trial.into_owned();
    print::materialize(&mut winner).expect("winning image");
    assert_eq!(
        ktpu_l1::text::emit(&winner.images["held"].stmts),
        ktpu_l1::text::emit(&baseline.images["held"].stmts),
        "the encoded sequence includes exactly the same throttling waits"
    );
    assert!(
        winner.images["held"]
            .stmts
            .iter()
            .any(|st| st.op == "vsync" && st.kw("on") == Some(&Term::name("fill"))),
        "this trial needs a throttle"
    );
}
