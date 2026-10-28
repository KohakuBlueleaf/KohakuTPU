//! One test per ISA clause the encoders implement, named after it; bit
//! positions are the spec page's, written out here as the reference.

mod cluster;
mod mover;
mod sru;
mod vector;

use ktpu_isa::{Format, Isa, Machine, Refusal};

pub fn machine() -> Machine {
    Machine::named("multimesh_v9t8").unwrap()
}

pub fn isa() -> Isa {
    Isa::new(&machine()).unwrap()
}

/// Field `name` of `f` sits at `[hi:lo]`.
#[track_caller]
pub fn at(f: &Format, name: &str, hi: u32, lo: u32) {
    let (bit, width) = f.span(name);
    assert_eq!((bit + width - 1, bit), (hi, lo), "{} {name}", f.name);
}

#[track_caller]
pub fn refused<T: std::fmt::Debug>(got: Result<T, Refusal>, clause: &str) {
    match got {
        Err(r) => assert_eq!(r.clause, clause, "{r}"),
        Ok(v) => panic!("accepted {v:?}, want {clause}"),
    }
}
