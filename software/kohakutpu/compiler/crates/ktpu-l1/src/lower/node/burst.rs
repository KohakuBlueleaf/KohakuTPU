//! Memory-drain burst proofs and their address dependencies.

use super::*;

const PAGE_BYTES: u64 = 4096;

/// Whether a drain's write bursts stay within their starting pages.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BurstProof {
    Never,
    Crossing,
    /// A relocatable base, with the drain's displacement from that base.
    Base {
        base: u64,
        offset: u64,
    },
    /// An address or extent supplied by a node register.
    Register,
}

/// One memory drain's proof, before address rewriting.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DrainBurst {
    pub coord: Xy,
    pub address: u64,
    pub bytes: Option<u64>,
    pub proof: BurstProof,
}

/// Classify a static byte extent. Bursts flush at `burst` bytes and at the
/// extent's end; ending at a page boundary does not cross that boundary.
pub fn static_burst_proof(address: u64, bytes: u64, burst: u64) -> Result<BurstProof> {
    if burst == 0 {
        return Err(Error::new("MACHINE", "cluster write burst is zero"));
    }
    let first = PAGE_BYTES - address % PAGE_BYTES;
    let crosses = bytes > first
        && (first % burst != 0 || (bytes - first > PAGE_BYTES && PAGE_BYTES % burst != 0));
    Ok(if crosses {
        BurstProof::Crossing
    } else {
        BurstProof::Never
    })
}

impl<'a> NodeReader<'a> {
    /// Record the proof at the same boundary that encodes memory drains.
    /// Register patches cannot be proved using their constant placeholders.
    pub(super) fn record_drain_burst(
        &mut self,
        coord: Xy,
        st: &Stmt,
        op: &ClusterInst,
        patches: &[Patch],
    ) -> Result<()> {
        if !self.target.machine.flaws.f016.present {
            return Ok(());
        }
        let ClusterInst::Drain(d) = op else {
            return Ok(());
        };
        if d.dst.is_some() {
            return Ok(());
        }
        let patched = |name: &str| {
            let segments = self.isa.cluster.segments(name);
            patches.iter().any(|p| {
                p.segments.iter().any(|&(bit, width, shift)| {
                    segments
                        .iter()
                        .any(|s| (s.bit, s.width, s.shift) == (bit, width, shift))
                })
            })
        };
        let bytes =
            (!patched("n")).then(|| u64::from(d.n) * u64::from(self.target.machine.die.word_bytes));
        let proof = if patched("addr") || bytes.is_none() {
            BurstProof::Register
        } else if let Some((&base, _)) = self
            .p
            .spans
            .range(..=d.addr)
            .next_back()
            .filter(|(base, size)| d.addr - **base < **size)
        {
            BurstProof::Base {
                base,
                offset: d.addr - base,
            }
        } else {
            let burst = u64::from(self.target.machine.mg.wburst)
                * u64::from(self.target.machine.die.word_bytes);
            static_burst_proof(d.addr, bytes.unwrap_or(0), burst).map_err(|e| self.at(st, e))?
        };
        self.drain_bursts.push(DrainBurst {
            coord,
            address: d.addr,
            bytes,
            proof,
        });
        Ok(())
    }
}
