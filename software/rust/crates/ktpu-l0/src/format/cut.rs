//! The host's cut for a dump after step k (RUL.076, DBG.DUMP.001): the
//! package ending after its step `after`, everything else as it is.

use super::Package;
use crate::error::Result;
use crate::mutant;
use crate::refuse;

impl Package {
    /// This package ending after step `after` (DBG.DUMP.001; refused for a
    /// step the package lacks).
    pub fn cut(&self, after: usize) -> Result<Package> {
        if after >= self.steps.len() {
            return refuse!(
                "DBG.DUMP.001",
                "no step {after}; the package has {}",
                self.steps.len()
            );
        }
        let mut out = self.clone();
        if !mutant::on("dbg/no_cut") {
            out.steps.truncate(after + 1);
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::super::{Op, Step};
    use super::*;

    fn three() -> Package {
        Package {
            steps: vec![Step::bare(Op::Barrier); 3],
            ..Package::default()
        }
    }

    #[test]
    fn dbg_dump_001_cut_keeps_steps_through_after() {
        assert_eq!(three().cut(0).unwrap().steps.len(), 1);
        assert_eq!(three().cut(2).unwrap().steps.len(), 3);
        let e = three().cut(3).unwrap_err();
        assert_eq!(e.clause, "DBG.DUMP.001");
    }

    #[cfg(feature = "mutants")]
    #[test]
    fn dbg_dump_001_mutant_no_cut_runs_the_whole_package() {
        let got = mutant::with("dbg/no_cut", || three().cut(0).unwrap());
        assert_eq!(got.steps.len(), 3);
    }
}
