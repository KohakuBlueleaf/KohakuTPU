//! Memory as bytes, a 32-byte word at a time.

use crate::R;

pub const WORD: usize = 32;
pub type Word = [u8; WORD];

/// A flat memory from address 0.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Dram {
    pub bytes: Vec<u8>,
}

impl Dram {
    pub fn new(size: usize) -> Dram {
        Dram {
            bytes: vec![0; size],
        }
    }

    fn range(&self, at: i64, n: usize) -> R<std::ops::Range<usize>> {
        let lo = usize::try_from(at).map_err(|_| format!("address {at}"))?;
        if lo + n > self.bytes.len() {
            return Err(format!("{n} bytes at {at:#x} past {:#x}", self.bytes.len()));
        }
        Ok(lo..lo + n)
    }

    pub fn word(&self, at: i64) -> R<Word> {
        let r = self.range(at, WORD)?;
        Ok(self.bytes[r].try_into().expect("a word"))
    }

    pub fn set_word(&mut self, at: i64, w: &Word) -> R<()> {
        let r = self.range(at, WORD)?;
        self.bytes[r].copy_from_slice(w);
        Ok(())
    }

    pub fn read(&self, at: i64, n: usize) -> R<&[u8]> {
        let r = self.range(at, n)?;
        Ok(&self.bytes[r])
    }

    pub fn write(&mut self, at: i64, b: &[u8]) -> R<()> {
        let r = self.range(at, b.len())?;
        self.bytes[r].copy_from_slice(b);
        Ok(())
    }

    pub fn u64(&self, at: i64) -> R<u64> {
        let b = self.read(at, 8)?;
        Ok(u64::from_le_bytes(b.try_into().expect("8 bytes")))
    }
}
