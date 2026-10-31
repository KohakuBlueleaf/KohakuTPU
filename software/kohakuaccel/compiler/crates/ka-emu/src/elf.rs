//! ELF64 images: each loadable segment's bytes at its physical address.

/// One `PT_LOAD`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Segment {
    pub at: u64,
    pub bytes: Vec<u8>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Image {
    pub entry: u64,
    pub segments: Vec<Segment>,
}

const PT_LOAD: u32 = 1;

fn u16_at(b: &[u8], at: usize) -> u16 {
    u16::from_le_bytes([b[at], b[at + 1]])
}

fn u32_at(b: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(b[at..at + 4].try_into().expect("four bytes"))
}

fn u64_at(b: &[u8], at: usize) -> u64 {
    u64::from_le_bytes(b[at..at + 8].try_into().expect("eight bytes"))
}

pub fn read(b: &[u8]) -> Result<Image, String> {
    if b.len() < 64 || &b[..4] != b"\x7fELF" || b[4] != 2 || b[5] != 1 {
        return Err("not a little-endian ELF64".into());
    }
    let (phoff, phentsize, phnum) = (
        u64_at(b, 32) as usize,
        u16_at(b, 54) as usize,
        u16_at(b, 56),
    );
    let mut segments = Vec::new();
    for i in 0..phnum as usize {
        let h = phoff + i * phentsize;
        if u32_at(b, h) != PT_LOAD {
            continue;
        }
        let (offset, paddr) = (u64_at(b, h + 8) as usize, u64_at(b, h + 24));
        let filesz = u64_at(b, h + 32) as usize;
        let bytes = b
            .get(offset..offset + filesz)
            .ok_or("a segment past the file")?
            .to_vec();
        segments.push(Segment { at: paddr, bytes });
    }
    Ok(Image {
        entry: u64_at(b, 24),
        segments,
    })
}
