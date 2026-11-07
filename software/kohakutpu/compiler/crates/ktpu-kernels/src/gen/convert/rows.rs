//! Logical row boundaries for capacity-shaped stages (DSL.MOV.001, .005).
//!
//! Source and destination rows are whole transfer words. Expansion initializes
//! padding before copying the logical prefix; contraction copies only that
//! prefix. Each destination owner retains global source and destination origins.

use super::*;
use crate::split::{PlannedKernel, SplitRequest};
use crate::trace::fail;

#[derive(Clone, Copy)]
pub struct Boundary {
    pub src_rows: i64,
    pub dst_rows: i64,
    pub width: i64,
    /// Fixed mover streams and a full-capacity initialization on expansion.
    pub classed: bool,
    /// Initialize the destination capacity before copying input rows.
    pub initialize: bool,
}

impl Boundary {
    pub fn read(a: &serde_json::Value, m: &Machine) -> R<Self> {
        let integer = |key: &str| {
            a.get(key)
                .and_then(serde_json::Value::as_i64)
                .ok_or_else(|| {
                    ktpu_machine::Refusal::new("API.OP.001", format!("row boundary integer {key}"))
                })
        };
        let classed = match a.get("classed") {
            None => false,
            Some(value) => value.as_bool().ok_or_else(|| {
                ktpu_machine::Refusal::new("API.OP.001", "row boundary classed must be boolean")
            })?,
        };
        let src_rows = integer("src_rows")?;
        let dst_rows = integer("dst_rows")?;
        let initialize = match a.get("initialize") {
            None => dst_rows > src_rows,
            Some(value) => value.as_bool().ok_or_else(|| {
                ktpu_machine::Refusal::new("API.OP.001", "row boundary initialize must be boolean")
            })?,
        };
        if initialize && dst_rows < src_rows || !initialize && dst_rows > src_rows {
            return fail(
                "API.OP.001",
                "initialization expands or preserves the row domain",
            );
        }
        let boundary = Self {
            src_rows,
            dst_rows,
            width: integer("width")?,
            classed,
            initialize,
        };
        let word = Hw::of(m).word_bytes;
        let bytes = boundary.width.checked_mul(2);
        if boundary.src_rows <= 0
            || boundary.dst_rows <= 0
            || boundary.width <= 0
            || bytes.is_none_or(|n| n % word != 0)
            || bytes
                .and_then(|n| n.checked_mul(boundary.src_rows))
                .is_none()
            || bytes
                .and_then(|n| n.checked_mul(boundary.dst_rows))
                .is_none()
        {
            return fail(
                "API.OP.001",
                "row boundaries require positive whole-word rows",
            );
        }
        Ok(boundary)
    }

    pub(crate) fn prefix(&self) -> super::prefix::Prefix {
        super::prefix::Prefix {
            src_rows: self.src_rows,
            dst_rows: self.dst_rows,
            src_pitch: self.width,
            dst_pitch: self.width,
            src_first: 0,
            dst_first: 0,
            width: self.width,
            rows: self.dst_rows,
            copied_rows: self.src_rows.min(self.dst_rows),
            classed: self.classed,
            initialize: self.initialize,
        }
    }

    pub fn kernel(&self, m: &Machine) -> R<Kernel> {
        self.prefix().kernel(m)
    }

    pub fn split(&self, request: &SplitRequest, m: &Machine) -> R<PlannedKernel> {
        self.prefix().split(request, m)
    }
}
