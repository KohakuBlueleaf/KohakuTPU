//! Module encoding and timing for vector search; text input for spec/reference only.

use ktpu_l1::lower::image::{Images, Units, UNKNOWN};
use ktpu_l1::text::read;
use ktpu_l1::text::{Module, Stmt, Term};
use ktpu_machine::Machine;

use crate::ir::Result;

/// Every unit name at one coordinate of the home mesh: a name fills a
/// descriptor's or a peer drain's field, never adds a word.
struct Anywhere;

impl Units for Anywhere {
    fn resolve(&self, _st: &Stmt, _t: &Term) -> ktpu_l1::Result<(u32, (u32, u32))> {
        Ok((0, (0, 0)))
    }

    fn home(&self) -> u32 {
        0
    }
}

/// Spec/reference text input, not the production candidate path.
pub fn read_image(text: &str, name: &str, vregs: u32) -> Result<Module> {
    let mut m = read(text, &format!("<{name}>"))?;
    m.vregs = vregs;
    Ok(m)
}

/// The IMEM words image `name` of `module` encodes to.
pub fn words_of(module: &Module, name: &str, nparams: usize, m: &Machine) -> Result<usize> {
    let anywhere = Anywhere;
    let mut images = Images::new(module, m, UNKNOWN, Some(&anywhere))?;
    let st = Stmt::new("run", vec![]);
    let (words, _) = images.call(&st, name, &vec![0; nparams])?;
    Ok(words.len())
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
