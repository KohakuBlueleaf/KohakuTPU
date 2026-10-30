//! L2: the typed IR, its text, the L2 -> L2 passes and the lowering to L1
//! (`docs/spec/stack/l2/program.md`).
//!
//! ```text
//! Program (L2 text) -> select place space share split step pipeline retile stage -> lower -> L1
//! ```

pub mod ir;
pub mod lower;
pub mod mutant;
pub mod passes;
pub mod spec;
pub mod target;
pub mod work;

use ktpu_machine::Machine;

pub use ir::{Body, Buffer, Error, Node, Op, Par, Program, Result, Type};
pub use lower::vector::search::Strategy;
pub use lower::vector::{Ranges, Search, ZeroSign};
pub use passes::PASSES;
pub use target::Target;

/// What `compile_l1` makes: the program after the passes, the lowering's L1
/// text, and the L1 module (scheduled when asked).
#[derive(Clone, Debug)]
pub struct Compiled {
    pub l2: Program,
    pub l1_text: String,
    pub l1: ktpu_l1::text::Module,
}

/// `fn name.l2` of `p` to L1 (L2.PASS.001): the machine's vector register
/// file becomes the module's, `passes` in order, the lowering, then the image
/// scheduler when `schedule`.
pub fn compile_l1(
    p: &Program,
    m: &Machine,
    passes: &[&str],
    schedule: bool,
    search: &Search,
) -> Result<Compiled> {
    let t = Target::of(m);
    compile_with(p, &t, passes, schedule, search)
}

/// [`compile_l1`] for a target already taken from a machine (its node count
/// overridden, say).
pub fn compile_with(
    p: &Program,
    t: &Target,
    passes: &[&str],
    schedule: bool,
    search: &Search,
) -> Result<Compiled> {
    let p = p.with_vregs(t.vregs, t.vcopies);
    let l2 = passes::run(&p, passes, t, search)?;
    let l1_text = lower::lower(&l2, t, search)?;
    let file = format!("{}:{}.l1", l2.module.file, l2.name);
    let read = ktpu_l1::text::read(&l1_text, &file)?;
    let l1 = if schedule {
        ktpu_l1::sched::schedule_module(&read, &t.machine)?
    } else {
        read
    };
    Ok(Compiled { l2, l1_text, l1 })
}
