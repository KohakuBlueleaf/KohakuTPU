//! The machine facts ktpu-l0 reads (`Facts::of`), witnessed on machine
//! variants: a fact changed in the machine changes what is built.

use super::*;
use ktpu_l0::mover::{self, Dim, Walk};
use ktpu_machine::Machine;

#[test]
fn the_movers_word_is_the_machines() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let narrow = m.with_fact("die.word_bytes", 16.into()).unwrap();
    let (wide, thin) = (Facts::of(&m), Facts::of(&narrow));
    assert_eq!((wide.word_bytes, thin.word_bytes), (32, 16));
    // 64 bytes are two 32-byte words, or four 16-byte ones.
    assert_ne!(
        mover::copy(&wide, 0, 0x1000, 64).unwrap(),
        mover::copy(&thin, 0, 0x1000, 64).unwrap()
    );
    let half = Walk::new(16, vec![Dim::new(1, 16)]);
    assert!(mover::walker(&wide, 0, &half, 0, 0).is_err());
    assert!(mover::walker(&thin, 0, &half, 0, 0).is_ok());
}
