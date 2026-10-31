//! What the hart loads and stores through.

/// A physical address that answers nothing.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Fault;

pub trait Bus {
    /// `bytes` (1, 2, 4 or 8) at `at`, zero-extended.
    fn load(&mut self, at: u64, bytes: u32) -> Result<u64, Fault>;
    fn store(&mut self, at: u64, bytes: u32, value: u64) -> Result<(), Fault>;
    /// The hart reached `cycle`; whether the external interrupt line is high.
    fn tick(&mut self, _cycle: u64) -> bool {
        false
    }
}

/// Little-endian bytes of `store` at offset `at` of `mem`.
pub fn put(mem: &mut [u8], at: usize, bytes: u32, value: u64) -> Result<(), Fault> {
    let n = bytes as usize;
    let slot = mem.get_mut(at..at + n).ok_or(Fault)?;
    slot.copy_from_slice(&value.to_le_bytes()[..n]);
    Ok(())
}

pub fn get(mem: &[u8], at: usize, bytes: u32) -> Result<u64, Fault> {
    let n = bytes as usize;
    let slot = mem.get(at..at + n).ok_or(Fault)?;
    let mut raw = [0u8; 8];
    raw[..n].copy_from_slice(slot);
    Ok(u64::from_le_bytes(raw))
}
