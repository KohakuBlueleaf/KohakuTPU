//! One kernel's run over a host's address space: the parameters' spans copied
//! into a compact [`Dram`], run, and copied back.

use super::view::Buf;
use super::{run, Binds};
use crate::{Dram, R};
use ktpu_l1::text::read;

const ALIGN: i64 = 4096;

/// Spans that touch as one region each: parameters that overlap at their
/// addresses still overlap at their compact ones.
fn regions(mut spans: Vec<(i64, i64)>) -> Vec<(i64, i64)> {
    spans.sort_unstable();
    let mut out: Vec<(i64, i64)> = Vec::new();
    for (lo, hi) in spans {
        match out.last_mut() {
            Some(r) if lo <= r.1 => r.1 = r.1.max(hi),
            _ => out.push((lo, hi)),
        }
    }
    out
}

/// Runs `name`'s L2 of `text` with each parameter at its address in `binds`,
/// the bytes through `fetch` and `store`; `seed` picks an order the guards allow.
pub fn hosted(
    text: &str,
    name: &str,
    binds: &Binds,
    seed: u64,
    mut fetch: impl FnMut(i64, usize) -> R<Vec<u8>>,
    mut store: impl FnMut(i64, &[u8]) -> R<()>,
) -> R<()> {
    let module = read(text, "hosted").map_err(|e| e.to_string())?;
    let body = module.body(name, "l2").map_err(|e| e.to_string())?;
    let mut spans = Vec::new();
    for (p, t) in &body.params {
        let at = *binds.get(p).ok_or(format!("{p} is not bound"))?;
        spans.push((at, at + Buf::of(t, at)?.bytes().max(1)));
    }
    let regions = regions(spans);
    let mut placed = Vec::new();
    let mut end = ALIGN;
    for (lo, hi) in &regions {
        let at = (end + ALIGN - 1) / ALIGN * ALIGN + lo % ALIGN;
        placed.push(at);
        end = at + hi - lo;
    }
    let mut mem = Dram::new(end as usize);
    for ((lo, hi), at) in regions.iter().zip(&placed) {
        let raw = fetch(*lo, (hi - lo) as usize)?;
        mem.bytes[*at as usize..(*at + hi - lo) as usize].copy_from_slice(&raw);
    }
    let compact = |at: i64| {
        let k = regions.iter().position(|(lo, hi)| (*lo..*hi).contains(&at));
        k.map(|k| placed[k] + at - regions[k].0)
    };
    let mut local = Binds::new();
    for (p, _) in &body.params {
        local.insert(p.clone(), compact(binds[p]).ok_or("a parameter's span")?);
    }
    run(&module, name, &local, &mut mem, seed)?;
    for ((lo, hi), at) in regions.iter().zip(&placed) {
        store(*lo, &mem.bytes[*at as usize..(*at + hi - lo) as usize])?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn touching_and_overlapping_spans_are_one_region() {
        let got = regions(vec![(100, 200), (8192, 8200), (200, 300), (150, 160)]);
        assert_eq!(got, vec![(100, 300), (8192, 8200)]);
    }
}
