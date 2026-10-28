//! Offline checker coverage: full corners, adjacent faces and digest-seeded interior.

use super::Class;
use ktpu_l0::format::template::{Params, Template};
use ktpu_verify::{gate, Mode, Nodes, Target};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};

/// Refuse excessive domains rather than silently omitting required corners.
pub fn samples(
    class: &Class,
    digest: &str,
    interior: usize,
    cap: usize,
) -> Result<Vec<Params>, String> {
    if cap == 0 {
        return Err("corner budget exceeded".into());
    }
    if class.values().any(|b| b.lo > b.hi) {
        return Err("invalid class box".into());
    }
    let mut corners = vec![Params::new()];
    for (name, bounds) in class {
        let values: Vec<_> = if bounds.lo == bounds.hi {
            vec![bounds.lo]
        } else {
            vec![bounds.lo, bounds.hi]
        };
        if corners.len().saturating_mul(values.len()) > cap {
            return Err("corner budget exceeded".into());
        }
        corners = corners
            .into_iter()
            .flat_map(|point| {
                values.iter().map(move |v| {
                    let mut p = point.clone();
                    p.insert(name.clone(), *v);
                    p
                })
            })
            .collect();
    }
    let mut out: BTreeSet<_> = corners.iter().cloned().collect();
    for corner in corners {
        for (name, bounds) in class {
            if bounds.lo == bounds.hi {
                continue;
            }
            for v in [bounds.lo + 1, bounds.hi - 1, 1] {
                if v < bounds.lo || v > bounds.hi {
                    continue;
                }
                let mut p = corner.clone();
                p.insert(name.clone(), v);
                out.insert(p);
                if out.len() > cap {
                    return Err("adjacent sample budget exceeded".into());
                }
            }
        }
    }
    for i in 0..interior {
        let mut p = Params::new();
        for (name, bounds) in class {
            let bytes = Sha256::digest(format!("{digest}:{i}:{name}"));
            let random = u64::from_le_bytes(bytes[..8].try_into().unwrap()) as i128;
            let lo = bounds.lo as i128;
            let width = bounds.hi as i128 - lo;
            let v = if width > 1 {
                lo + 1 + random % (width - 1)
            } else {
                lo
            };
            p.insert(name.clone(), v as i64);
        }
        out.insert(p);
        if out.len() > cap {
            return Err("interior sample budget exceeded".into());
        }
    }
    Ok(out.into_iter().collect())
}

/// Each binding runs twice so carried core state is checked across launches too.
pub fn verify(
    templates: &BTreeMap<u32, Template>,
    bindings: &BTreeMap<u32, Vec<u64>>,
    digest: &str,
    target: &Target,
    interior: usize,
    cap: usize,
) -> Result<Vec<Params>, String> {
    let class = &templates
        .values()
        .next()
        .ok_or("empty template")?
        .table
        .parameters;
    if templates.values().any(|t| &t.table.parameters != class) {
        return Err("mesh class boxes disagree".into());
    }
    let points = samples(class, digest, interior, cap)?;
    for point in &points {
        let nodes: Nodes = templates
            .iter()
            .map(|(mesh, template)| {
                let addresses = bindings.get(mesh).ok_or("missing mesh bindings")?;
                template
                    .bind(addresses, point)
                    .map(|p| (*mesh, p))
                    .map_err(|e| e.to_string())
            })
            .collect::<Result<_, String>>()?;
        gate(&[nodes.clone(), nodes], target, Mode::Refuse, None)
            .map_err(|e| format!("binding {point:?}: {e:?}"))?;
    }
    Ok(points)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ktpu_l0::format::template::Bounds;

    #[test]
    fn reproducible_all_faces_and_seeded_interiors() {
        let class = [
            ("x".into(), Bounds { lo: 0, hi: 100 }),
            ("y".into(), Bounds { lo: 2, hi: 90 }),
        ]
        .into();
        let a = samples(&class, "class-a", 16, 1000).unwrap();
        assert_eq!(a, samples(&class, "class-a", 16, 1000).unwrap());
        assert_ne!(a, samples(&class, "class-b", 16, 1000).unwrap());
        for x in [0, 1, 99, 100] {
            for y in [2, 90] {
                assert!(a.contains(&[("x".into(), x), ("y".into(), y)].into()));
            }
        }
        assert!(samples(&class, "a", 16, 3).is_err());
        assert!(samples(&class, "a", 16, 5).is_err());
        assert!(samples(&Class::new(), "a", 0, 0).is_err());
        assert_eq!(
            samples(&Class::new(), "a", 0, 1).unwrap(),
            vec![Params::new()]
        );
    }
}
