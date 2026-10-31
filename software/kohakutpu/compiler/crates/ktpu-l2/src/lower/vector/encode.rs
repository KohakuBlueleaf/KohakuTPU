//! Module encoding and timing for vector search; text input for spec/reference only.

pub use ktpu_l1::lower::image::words_of;
use ktpu_l1::text::read;
use ktpu_l1::text::Module;
use ktpu_machine::Machine;

use crate::ir::Result;

/// Spec/reference text input, not the production candidate path.
pub fn read_image(text: &str, name: &str, vregs: u32) -> Result<Module> {
    let mut m = read(text, &format!("<{name}>"))?;
    m.vregs = vregs;
    Ok(m)
}

/// The core's timing of image `name` of `module` (L2.SEARCH.002).
pub fn cycles(
    module: &Module,
    name: &str,
    vregs: u32,
    copies: u32,
    fill: (i64, i64),
    m: &Machine,
) -> Result<i64> {
    ktpu_l1::sched::timing::cycles_of(module, name, vregs, copies, fill, m)
}
