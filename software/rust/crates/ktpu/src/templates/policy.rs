//! Compile-time width policy. Bounds come from the machine/generator contract.

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum ShapeClass {
    Exact,
    #[default]
    Pow2,
    Coarse,
}

impl ShapeClass {
    /// None is the wide class, which requires a column-block loop.
    pub fn width(self, width: u64, kblock: u64, max_block: u64) -> Result<Option<u64>, String> {
        if width == 0
            || kblock == 0
            || max_block < kblock
            || !kblock.is_power_of_two()
            || !max_block.is_power_of_two()
        {
            return Err("invalid width policy domain".into());
        }
        if width > max_block {
            return Ok(None);
        }
        Ok(Some(match self {
            Self::Exact => width,
            Self::Pow2 => width
                .max(kblock)
                .checked_next_power_of_two()
                .ok_or("width overflow")?,
            Self::Coarse => {
                if width <= max_block / 2 {
                    (max_block / 2).max(kblock)
                } else {
                    max_block
                }
            }
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::ShapeClass;

    #[test]
    fn policies_are_compile_time_knobs() {
        assert_eq!(ShapeClass::default(), ShapeClass::Pow2);
        assert_eq!(ShapeClass::Exact.width(96, 32, 256).unwrap(), Some(96));
        assert_eq!(ShapeClass::Pow2.width(96, 32, 256).unwrap(), Some(128));
        assert_eq!(ShapeClass::Coarse.width(32, 32, 256).unwrap(), Some(128));
        assert_eq!(ShapeClass::Pow2.width(288, 32, 256).unwrap(), None);
        assert!(ShapeClass::Pow2.width(0, 32, 256).is_err());
    }
}
