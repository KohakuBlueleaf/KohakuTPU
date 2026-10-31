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
