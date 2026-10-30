//! Fixed engine topology with address relocation sites compiled to wire offsets.

use ktpu_l0::format::bind::{bind_addresses, environment, translate, Translation};
use ktpu_l0::format::template::{Params, Template};
use ktpu_l0::format::{Buffer, MOVER_SKIP};
use ktpu_l0::word::WordOps;
use ktpu_l0::{Op, Package, Reloc};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

struct Patch {
    at: usize,
    bit: u32,
    width: u32,
    buffer: usize,
    addend: i128,
    shift: u32,
    top: bool,
}

pub(super) struct WireRecipe {
    raw: Vec<u8>,
    buffers: Vec<Buffer>,
    patches: Vec<Patch>,
    environment: Option<Vec<[i64; 3]>>,
    last: Option<(Vec<u64>, Py<PyBytes>)>,
}

impl WireRecipe {
    pub(super) fn build(
        t: &Template,
        params: &Params,
        p: &Package,
        low: &Package,
        raw: &[u8],
    ) -> Option<Self> {
        // Mailbox deduplication, dynamic fetch and node control need a different topology.
        if (p.steps.iter().any(|s| s.op == Op::Engine)
            && p.steps
                .iter()
                .any(|s| !matches!(s.op, Op::Engine | Op::End)))
            || p.checksum
            || p.units.iter().any(|u| !u.has_port())
            || p.steps.iter().any(|s| {
                !matches!(
                    s.op,
                    Op::Dispatch
                        | Op::Mover
                        | Op::Await
                        | Op::Barrier
                        | Op::Mwait
                        | Op::Ring
                        | Op::Engine
                        | Op::End
                ) || (s.op == Op::Await && s.flags != 0)
            })
        {
            return None;
        }
        let mut relocs = t.package.relocs.clone();
        for site in &t.table.sites {
            if let Some(buffer) = site.buffer {
                if site.step.is_some() {
                    return None;
                }
                let addend = site.constant as i128
                    + site
                        .coefficients
                        .iter()
                        .map(|(name, coefficient)| *coefficient as i128 * params[name] as i128)
                        .sum::<i128>();
                relocs.push(Reloc {
                    payload: site.payload as u64,
                    bit: site.bit as u64,
                    width: site.width as u64,
                    buffer: buffer as u64,
                    addend,
                    shift: site.shift as u64,
                });
            }
        }
        let offset = u64::from_le_bytes(raw[72..80].try_into().unwrap()) as usize;
        let mut patches = Vec::new();
        let mut push = |r: &Reloc, payload: usize, bit: u32| {
            patches.push(Patch {
                at: offset + payload * 32 + (bit / 64) as usize * 8,
                bit: bit % 64,
                width: r.width as u32,
                buffer: r.buffer as usize,
                addend: r.addend,
                shift: r.shift as u32,
                top: !relocs.iter().any(|o| {
                    o.payload == r.payload
                        && o.buffer == r.buffer
                        && o.addend == r.addend
                        && o.shift == r.shift + r.width
                }),
            });
        };
        // A relocation may straddle two limbs; the byte patch uses a 128-bit window.
        for r in &relocs {
            push(r, r.payload as usize, r.bit as u32);
        }
        let destinations: Vec<_> = low
            .steps
            .iter()
            .filter(|s| s.op == Op::Engine)
            .flat_map(|s| (0..s.count).map(move |k| ((s.arg + k / 3) as usize, (k % 3) as u32)))
            .filter(|&(i, j)| (8..24).contains(&low.payloads[i].get(8 * j, 8)))
            .collect();
        let mut destinations = destinations.into_iter();
        for s in &p.steps {
            if s.op == Op::End {
                break;
            }
            if s.op != Op::Mover {
                continue;
            }
            for i in s.arg..s.arg + s.count {
                for half in [0u32, 128] {
                    if p.payloads[i as usize].limb(half) == MOVER_SKIP {
                        continue;
                    }
                    let (dst, j) = destinations.next()?;
                    for r in relocs.iter().filter(|r| {
                        r.payload == i
                            && r.bit >= (half + 64) as u64
                            && r.bit + r.width <= (half + 128) as u64
                    }) {
                        push(r, dst, 64 * (j + 1) + r.bit as u32 - half - 64);
                    }
                    // Register/opcode relocation changes the engine's structure.
                    if relocs.iter().any(|r| {
                        r.payload == i
                            && r.bit < (half + 64) as u64
                            && r.bit + r.width > half as u64
                    }) {
                        return None;
                    }
                    if relocs.iter().any(|r| {
                        r.payload == i
                            && r.bit < (half + 128) as u64
                            && r.bit + r.width > (half + 128) as u64
                    }) {
                        return None;
                    }
                }
            }
        }
        Some(Self {
            raw: raw.to_vec(),
            buffers: t.effective_buffers().ok()?,
            patches,
            environment: t.table.environment.clone(),
            last: None,
        })
    }

    pub(super) fn bind<'py>(
        &mut self,
        py: Python<'py>,
        handles: &[u64],
        translations: Option<&[Translation]>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let addresses = match translations {
            Some(table) => translate(&self.buffers, handles, table)
                .map_err(|e| PyValueError::new_err(e.to_string()))?,
            None => bind_addresses(&self.buffers, handles),
        };
        if let Some((previous, bytes)) = &self.last {
            if previous == &addresses {
                return Ok(bytes.bind(py).clone());
            }
        }
        if let Some(expected) = &self.environment {
            let actual = environment(&self.buffers, &addresses)
                .map_err(|e| PyValueError::new_err(e.to_string()))?;
            if &actual != expected {
                return Err(PyValueError::new_err(
                    "L0.BIND.044: recipe alias environment changed",
                ));
            }
        }
        let raw = PyBytes::new_with(py, self.raw.len(), |raw| {
            raw.copy_from_slice(&self.raw);
            for patch in &self.patches {
                let address = addresses[patch.buffer] as i128 + patch.addend;
                let value = address >> patch.shift;
                if address < 0
                    || address > u64::MAX as i128
                    || (patch.top && patch.width < 64 && value >> patch.width != 0)
                {
                    return Err(PyValueError::new_err(
                        "RUL.039: recipe address does not fit relocation field",
                    ));
                }
                let bytes = ((patch.bit + patch.width).div_ceil(8)) as usize;
                let mut word = [0u8; 16];
                word[..bytes].copy_from_slice(&raw[patch.at..patch.at + bytes]);
                let old = u128::from_le_bytes(word);
                let mask = ((1u128 << patch.width) - 1) << patch.bit;
                let new = (old & !mask) | (((value as u128) << patch.bit) & mask);
                raw[patch.at..patch.at + bytes].copy_from_slice(&new.to_le_bytes()[..bytes]);
            }
            Ok(())
        })?;
        self.last = Some((addresses, raw.clone().unbind()));
        Ok(raw)
    }
}
