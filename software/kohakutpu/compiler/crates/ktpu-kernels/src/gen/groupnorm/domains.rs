//! Logical normalization domains and checked extents.

use super::*;
use serde::{Deserialize, Serialize};

/// Logical NCHW population domains; affine parameters are per channel.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct GroupNormSpec {
    pub batch: i64,
    pub channels: i64,
    pub height: i64,
    pub width: i64,
    pub groups: i64,
    pub eps: f64,
}

impl GroupNormSpec {
    pub fn extents(&self) -> R<(i64, i64, i64)> {
        if [
            self.batch,
            self.channels,
            self.height,
            self.width,
            self.groups,
        ]
        .iter()
        .any(|&v| v <= 0)
            || self.channels % self.groups != 0
            || !self.eps.is_finite()
            || self.eps < 0.0
        {
            return fail("API.OP.001", "invalid GroupNorm population domain");
        }
        let multiply = |a: i64, b: i64| {
            a.checked_mul(b).ok_or_else(|| {
                ktpu_machine::Refusal::new("API.OP.001", "GroupNorm extent overflow")
            })
        };
        let spatial = multiply(self.height, self.width)?;
        let width = multiply(self.channels / self.groups, spatial)?;
        let rows = multiply(self.batch, self.groups)?;
        multiply(multiply(rows, width)?, 2)?;
        Ok((rows, width, spatial))
    }
}
