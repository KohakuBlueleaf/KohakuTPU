//! Unit read request geometry and the single-beat MAG reach census.

use std::collections::BTreeMap;

/// One unit request containing at least one single 64-byte MAG piece.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Read {
    pub class: String,
    pub unit: String,
    pub ordinal: u64,
    pub address: u64,
    pub bytes: u64,
    pub single_beats: Vec<u64>,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Census {
    pub unit: String,
    /// Requests, affected requests, single-beat pieces, by class.
    pub counts: BTreeMap<String, (u64, u64, u64)>,
    pub reads: Vec<Read>,
    /// Maximal source runs whose actual AXI cuts need an issue trace.
    pub unresolved: Vec<Read>,
    next: u64,
}

/// AXI beats covering a byte request, cut from its start at RD_BEATS and 4 KiB.
pub fn mag_pieces(address: u64, bytes: u64) -> Vec<(u64, u64)> {
    if bytes == 0 {
        return Vec::new();
    }
    let mut at = address & !63;
    let mut beats = (bytes + (address & 63)).div_ceil(64);
    let mut out = Vec::new();
    while beats != 0 {
        let n = beats.min(16).min((4096 - (at & 4095)) / 64);
        out.push((at, n));
        at += n * 64;
        beats -= n;
    }
    out
}

impl Census {
    pub fn request(&mut self, class: &str, address: i64, bytes: usize) {
        let address = address as u64;
        let pieces = mag_pieces(address, bytes as u64);
        let singles: Vec<_> = pieces
            .into_iter()
            .filter(|p| p.1 == 1)
            .map(|p| p.0)
            .collect();
        let count = self.counts.entry(class.into()).or_default();
        count.0 += 1;
        let ordinal = self.next;
        self.next += 1;
        if !singles.is_empty() {
            count.1 += 1;
            count.2 += singles.len() as u64;
            self.reads.push(Read {
                class: class.into(),
                unit: self.unit.clone(),
                ordinal,
                address,
                bytes: bytes as u64,
                single_beats: singles,
            });
        }
    }

    /// Vector AGU chunks and the core's eight-bit run accumulator. `inner`
    /// is the dimension-zero bound when its stride is 32, otherwise one.
    pub fn vector(&mut self, addresses: &[i64], landing: usize, inner: usize) {
        for (address, words) in vector_requests(addresses, landing, inner) {
            self.request("vc_fill", address, words * 32);
        }
    }

    fn candidate(&mut self, class: &str, address: i64, bytes: usize) {
        let ordinal = self.next;
        self.next += 1;
        self.unresolved.push(Read {
            class: class.into(),
            unit: self.unit.clone(),
            ordinal,
            address: address as u64,
            bytes: bytes as u64,
            single_beats: Vec::new(),
        });
    }

    /// Consecutive mover source words, before timing-dependent issue cuts.
    /// None is a padding element, which closes the current read run.
    pub fn mover(&mut self, class: &str, addresses: impl IntoIterator<Item = Option<i64>>) {
        let mut run: Option<(i64, usize)> = None;
        for address in addresses {
            if let (Some((start, n)), Some(at)) = (run, address) {
                if at == start + n as i64 * 32 {
                    run = Some((start, n + 1));
                    continue;
                }
            }
            if let Some((start, n)) = run.take() {
                self.candidate(class, start, n * 32);
            }
            run = address.map(|a| (a, 1));
        }
        if let Some((start, n)) = run {
            self.candidate(class, start, n * 32);
        }
    }
}

pub fn vector_requests(addresses: &[i64], landing: usize, inner: usize) -> Vec<(i64, usize)> {
    let mut out = Vec::new();
    let mut at = 0;
    let mut run: Option<(i64, usize)> = None;
    while at < addresses.len() {
        let off = (landing + at) % 256;
        let n = (addresses.len() - at)
            .min(255)
            .min(256 - off)
            .min(inner.max(1) - at % inner.max(1));
        let address = addresses[at];
        if let Some((start, count)) = run {
            if start + count as i64 * 32 == address && count + n <= 255 && off != 0 {
                run = Some((start, count + n));
            } else {
                out.push((start, count));
                run = Some((address, n));
            }
        } else {
            run = Some((address, n));
        }
        at += n;
    }
    out.extend(run);
    out
}
