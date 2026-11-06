//! Image-owned descriptor walks with bounded contiguous AGU chunks.

use super::*;

/// Preserve the word address sequence and descriptor count. Two 128-word
/// chunks cannot merge into the core's eight-bit request count.
pub(super) fn fill_descs(stmts: &[Stmt], descs: &mut Descs, facts: &CoreFacts) -> Result<()> {
    if !facts.read_single {
        return Ok(());
    }
    for st in stmts {
        if st.op == "vfill" {
            if let Some(ad) = st
                .positional()
                .first()
                .and_then(|p| p.as_name())
                .and_then(|n| n.strip_prefix('a'))
                .and_then(|n| n.parse::<u32>().ok())
            {
                if (0..=facts.dims).all(|f| descs.contains_key(&(ad, f))) {
                    let mut dims: Vec<(i64, u32)> = Vec::new();
                    for f in 1..=facts.dims {
                        let value = descs[&(ad, f)];
                        let stride = ((value >> 16) as i64) << 46 >> 46;
                        let bound = (value as u32 & 0xffff).max(1);
                        if bound == 1 {
                            continue;
                        }
                        if let Some((s, n)) = dims.last_mut() {
                            if *s * i64::from(*n) == stride {
                                if let Some(product) = n.checked_mul(bound).filter(|n| *n <= 0xffff)
                                {
                                    *n = product;
                                    continue;
                                }
                            }
                        }
                        dims.push((stride, bound));
                    }
                    if let Some(&(32, n)) = dims.first() {
                        if n >= 256 && n % 128 == 0 && dims.len() < facts.dims as usize {
                            dims[0] = (32, 128);
                            dims.insert(1, (4096, n / 128));
                            for f in 1..=facts.dims {
                                let (stride, count) =
                                    dims.get(f as usize - 1).copied().unwrap_or((0, 0));
                                descs.insert((ad, f), dim(stride, count)?);
                            }
                        }
                    }
                }
            }
        }
        fill_descs(&st.body, descs, facts)?;
    }
    Ok(())
}
