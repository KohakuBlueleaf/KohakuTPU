//! Module encoding and timing for vector search; text input for spec/reference only.

pub use ktpu_l1::lower::image::words_of;
#[cfg(feature = "witness")]
use ktpu_l1::lower::image::{Anywhere, Images, UNKNOWN};
use ktpu_l1::text::read;
use ktpu_l1::text::Module;
#[cfg(feature = "witness")]
use ktpu_l1::text::Stmt;
use ktpu_machine::Machine;

use crate::ir::Result;

/// Spec/reference text input, not the production candidate path.
pub fn read_image(text: &str, name: &str, vregs: u32) -> Result<Module> {
    let mut m = read(text, &format!("<{name}>"))?;
    m.vregs = vregs;
    Ok(m)
}

#[cfg(feature = "witness")]
pub(super) fn compare_images(a: &Module, b: &Module, name: &str, nparams: usize, m: &Machine) {
    for args in [
        vec![0; nparams],
        (0..nparams).map(|i| 4096 + i as i128 * 256).collect(),
    ] {
        let encode = |module| {
            let anywhere = Anywhere;
            let mut images = Images::new(module, m, UNKNOWN, Some(&anywhere))?;
            images.call(&Stmt::new("run", vec![]), name, &args)
        };
        assert_eq!(
            format!("{:?}", encode(a)),
            format!("{:?}", encode(b)),
            "IMEM words and descriptors/refusals"
        );
    }
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
