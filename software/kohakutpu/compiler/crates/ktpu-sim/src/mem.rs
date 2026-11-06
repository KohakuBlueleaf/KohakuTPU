//! Memory as bytes, a 32-byte word at a time.

use std::ops::Range;

use crate::R;

pub const WORD: usize = 32;
pub type Word = [u8; WORD];

/// A flat memory from address 0.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Dram {
    pub bytes: Vec<u8>,
    pub reach: Option<ktpu_l1::reach::Census>,
    write_watch: Option<(Range<usize>, Vec<u32>)>,
}

impl Dram {
    pub fn new(size: usize) -> Dram {
        Dram {
            bytes: vec![0; size],
            reach: None,
            write_watch: None,
        }
    }

    /// Count writes to each byte of a selected allocation, from this point on.
    pub fn watch_writes(&mut self, range: Range<usize>) -> R<()> {
        if range.start > range.end || range.end > self.bytes.len() {
            return Err("write watch outside memory".into());
        }
        let counts = vec![0; range.len()];
        self.write_watch = Some((range, counts));
        Ok(())
    }

    pub fn write_counts(&self) -> Option<&[u32]> {
        self.write_watch
            .as_ref()
            .map(|(_, counts)| counts.as_slice())
    }

    fn count_write(&mut self, range: &Range<usize>) {
        if let Some((watched, counts)) = &mut self.write_watch {
            for at in range.start.max(watched.start)..range.end.min(watched.end) {
                counts[at - watched.start] += 1;
            }
        }
    }

    fn range(&self, at: i64, n: usize) -> R<Range<usize>> {
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
        self.count_write(&r);
        self.bytes[r].copy_from_slice(w);
        Ok(())
    }

    pub fn read(&self, at: i64, n: usize) -> R<&[u8]> {
        let r = self.range(at, n)?;
        Ok(&self.bytes[r])
    }

    pub fn write(&mut self, at: i64, b: &[u8]) -> R<()> {
        let r = self.range(at, b.len())?;
        self.count_write(&r);
        self.bytes[r].copy_from_slice(b);
        Ok(())
    }

    pub fn u64(&self, at: i64) -> R<u64> {
        let b = self.read(at, 8)?;
        Ok(u64::from_le_bytes(b.try_into().expect("8 bytes")))
    }
}
