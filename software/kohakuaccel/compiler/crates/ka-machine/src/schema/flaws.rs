//! `flaws`: the image's silicon flaws (`docs/spec/stack/flaws.md`). Each is a
//! defect of the RTL the image was built from, recorded with its evidence,
//! handling status and an RTL fix.

use super::Runtime;
use serde::{Deserialize, Serialize};

/// One silicon flaw.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Flaw {
    /// Whether this image carries the flaw, read from its RTL and inventory.
    pub present: bool,
    /// The block that has it.
    pub unit: Runtime<String>,
    /// What goes wrong.
    pub summary: Runtime<String>,
    /// The bench or source reasoning that shows it.
    pub witness: Runtime<String>,
    /// Software handling, accuracy disposition or an open requirement.
    pub handling: Runtime<String>,
    /// The RTL change that removes it.
    pub fix: Runtime<String>,
}

/// Every flaw the schema knows, by id.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Flaws {
    /// The mover's write engine pops its data FIFO before the word shows.
    #[serde(rename = "F-001")]
    pub f001: Flaw,
    /// The interlink sends a one-flit packet before its flit shows.
    #[serde(rename = "F-002")]
    pub f002: Flaw,
    /// A dispatch engine RING has no room check.
    #[serde(rename = "F-003")]
    pub f003: Flaw,
    /// A completed WAIT at least half the counter range behind reads as ahead.
    #[serde(rename = "F-004")]
    pub f004: Flaw,
    /// Under Sv39 a data access right after a multi-cycle one reuses its
    /// physical address.
    #[serde(rename = "F-005")]
    pub f005: Flaw,
    /// The signal word names the selected slot a cycle before its count.
    #[serde(rename = "F-006")]
    pub f006: Flaw,
    /// A node's interlink slots and sent count outlive its processor reset.
    #[serde(rename = "F-007")]
    pub f007: Flaw,
    /// Console pointer equality aliases full with empty.
    #[serde(rename = "F-008")]
    pub f008: Flaw,
    /// FP22 subtraction treats discarded residue as positive sticky.
    #[serde(rename = "F-009")]
    pub f009: Flaw,
    /// SIMT SLD is admitted without a scalar load path.
    #[serde(rename = "F-010")]
    pub f010: Flaw,
    /// SIMT BAR has no arrival/release consumer.
    #[serde(rename = "F-011")]
    pub f011: Flaw,
    /// Vector fill admission does not reserve a completion-tracker slot.
    #[serde(rename = "F-012")]
    pub f012: Flaw,
    /// SRU RUN does not admit the complete retained configuration.
    #[serde(rename = "F-013")]
    pub f013: Flaw,
    /// A one-beat Xache read's duplicate beat-zero lookup can be taken by the next burst.
    #[serde(rename = "F-014")]
    pub f014: Flaw,
    /// A remote vector burst past one packet reads a data flit as a descriptor (ACK0).
    #[serde(rename = "F-015")]
    pub f015: Flaw,
    /// A cluster drain's write burst is not split at a 4 KiB boundary.
    #[serde(rename = "F-016")]
    pub f016: Flaw,
}

impl Flaws {
    /// `(id, flaw)` of every flaw, in id order.
    pub fn all(&self) -> [(&'static str, &Flaw); 16] {
        [
            ("F-001", &self.f001),
            ("F-002", &self.f002),
            ("F-003", &self.f003),
            ("F-004", &self.f004),
            ("F-005", &self.f005),
            ("F-006", &self.f006),
            ("F-007", &self.f007),
            ("F-008", &self.f008),
            ("F-009", &self.f009),
            ("F-010", &self.f010),
            ("F-011", &self.f011),
            ("F-012", &self.f012),
            ("F-013", &self.f013),
            ("F-014", &self.f014),
            ("F-015", &self.f015),
            ("F-016", &self.f016),
        ]
    }
}
