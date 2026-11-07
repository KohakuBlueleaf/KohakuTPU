//! Checked affine interval evaluation shared by typed scheduling proofs.

use ktpu_graph as g;
use std::collections::BTreeMap;

pub(super) fn interval(
    index: &g::Index,
    bounds: &BTreeMap<String, (i64, i64)>,
) -> Option<(i64, i64)> {
    fn atom(atom: &g::Atom, bounds: &BTreeMap<String, (i64, i64)>) -> Option<(i64, i64)> {
        let (a, b, op) = match atom {
            g::Atom::Var(name) => return bounds.get(name).copied(),
            g::Atom::Floor(a, b) => (a, b, '/'),
            g::Atom::Mod(a, b) => (a, b, '%'),
            g::Atom::Mul(a, b) => (a, b, '*'),
        };
        let (lo, hi) = interval(a, bounds)?;
        let (left, right) = interval(b, bounds)?;
        match op {
            '/' if left == right && left > 0 => Some((lo.div_euclid(left), hi.div_euclid(left))),
            '%' if left == right && left > 0 => {
                if lo.div_euclid(left) == hi.div_euclid(left) {
                    Some((lo.rem_euclid(left), hi.rem_euclid(left)))
                } else {
                    Some((0, left - 1))
                }
            }
            '*' => {
                let values = [
                    lo.checked_mul(left)?,
                    lo.checked_mul(right)?,
                    hi.checked_mul(left)?,
                    hi.checked_mul(right)?,
                ];
                Some((*values.iter().min()?, *values.iter().max()?))
            }
            _ => None,
        }
    }
    match index {
        g::Index::Int(n) => Some((*n, *n)),
        g::Index::Sym(sym) => {
            let (mut lo, mut hi) = (sym.konst, sym.konst);
            for (term, coefficient) in &sym.terms {
                let (a, b) = atom(term, bounds)?;
                let (a, b) = (a.checked_mul(*coefficient)?, b.checked_mul(*coefficient)?);
                lo = lo.checked_add(a.min(b))?;
                hi = hi.checked_add(a.max(b))?;
            }
            Some((lo, hi))
        }
    }
}

/// Normalize only atoms proved affine over the complete loop box. In particular,
/// a modulo is removable only when its quotient is invariant on that box.
fn affine(
    index: &g::Index,
    bounds: &BTreeMap<String, (i64, i64)>,
) -> Option<(i64, BTreeMap<String, i64>)> {
    let (lo, hi) = interval(index, bounds)?;
    if lo == hi {
        return Some((lo, BTreeMap::new()));
    }
    let g::Index::Sym(sym) = index else {
        return None;
    };
    let mut constant = sym.konst;
    let mut terms = BTreeMap::<String, i64>::new();
    for (atom, coefficient) in &sym.terms {
        let (value, variables) = match atom {
            g::Atom::Var(var) => {
                let &(lo, hi) = bounds.get(var)?;
                if lo == hi {
                    (lo, BTreeMap::new())
                } else {
                    (0, BTreeMap::from([(var.clone(), 1)]))
                }
            }
            g::Atom::Floor(a, b) | g::Atom::Mod(a, b) => {
                let (divisor, end) = interval(b, bounds)?;
                let (low, high) = interval(a, bounds)?;
                if divisor <= 0 || divisor != end {
                    return None;
                }
                let quotient = low.div_euclid(divisor);
                if quotient != high.div_euclid(divisor) {
                    return None;
                }
                if matches!(atom, g::Atom::Floor(_, _)) {
                    (quotient, BTreeMap::new())
                } else {
                    let (value, vars) = affine(a, bounds)?;
                    (value.checked_sub(quotient.checked_mul(divisor)?)?, vars)
                }
            }
            g::Atom::Mul(a, b) => {
                let (left, right) = interval(b, bounds)?;
                if left != right {
                    return None;
                }
                let (value, vars) = affine(a, bounds)?;
                let scaled = vars
                    .into_iter()
                    .map(|(var, coefficient)| Some((var, coefficient.checked_mul(left)?)))
                    .collect::<Option<BTreeMap<_, _>>>()?;
                (value.checked_mul(left)?, scaled)
            }
        };
        constant = constant.checked_add(value.checked_mul(*coefficient)?)?;
        for (var, scale) in variables {
            let entry = terms.entry(var).or_default();
            *entry = entry.checked_add(scale.checked_mul(*coefficient)?)?;
        }
    }
    terms.retain(|_, value| *value != 0);
    Some((constant, terms))
}

/// Prove that repeated contiguous runs cover their entire interval envelope.
/// Return the independently varying coordinates to check rectangle products.
pub(super) fn dense(
    index: &g::Index,
    extent: i64,
    bounds: &BTreeMap<String, (i64, i64)>,
) -> Option<(i64, i64, std::collections::HashSet<String>)> {
    if extent <= 0 {
        return None;
    }
    let (constant, terms) = affine(index, bounds)?;
    let (mut low, mut high) = (constant, constant);
    for (var, coefficient) in &terms {
        let &(a, b) = bounds.get(var)?;
        let a = a.checked_mul(*coefficient)?;
        let b = b.checked_mul(*coefficient)?;
        low = low.checked_add(a.min(b))?;
        high = high.checked_add(a.max(b))?;
    }
    let mut strides = terms
        .iter()
        .map(|(var, coefficient)| {
            let &(lo, hi) = bounds.get(var)?;
            Some((coefficient.checked_abs()?, hi.checked_sub(lo)?))
        })
        .collect::<Option<Vec<_>>>()?;
    strides.sort();
    let mut covered = extent;
    for (stride, steps) in strides {
        if stride > covered {
            return None;
        }
        covered = covered.checked_add(stride.checked_mul(steps)?)?;
    }
    Some((low, high.checked_add(extent)?, terms.into_keys().collect()))
}
