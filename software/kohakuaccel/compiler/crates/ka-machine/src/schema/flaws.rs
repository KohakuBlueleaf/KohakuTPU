//! `flaws`: the image's silicon flaws (`docs/spec/stack/flaws.md`). Each is a
//! defect of the RTL the image was built from, recorded with its witness
//! bench and an RTL fix; the stack works around it while `present`.

use super::Runtime;
use serde::{Deserialize, Serialize};

/// One silicon flaw.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Flaw {
    /// Whether this image carries the flaw, read from its RTL and defines.
    /// The handling keys on it.
    pub present: bool,
    /// The block that has it.
    pub unit: Runtime<String>,
    /// What goes wrong, in one sentence.
    pub summary: Runtime<String>,
    /// The bench that shows it.
    pub witness: Runtime<String>,
    /// Where the stack works around it.
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
    /// A dispatch engine WAIT on a count more than half its range behind
    /// holds forever.
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
}

impl Flaws {
    /// `(id, flaw)` of every flaw, in id order.
    pub fn all(&self) -> [(&'static str, &Flaw); 7] {
        [
            ("F-001", &self.f001),
            ("F-002", &self.f002),
            ("F-003", &self.f003),
            ("F-004", &self.f004),
            ("F-005", &self.f005),
            ("F-006", &self.f006),
            ("F-007", &self.f007),
        ]
    }
}
