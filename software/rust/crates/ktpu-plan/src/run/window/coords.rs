//! A read's coordinates and the gates over them (`window.dims`, `coupled`,
//! `windowed`, `gate_of`, `boxed`).

use super::*;

impl Runner<'_> {
    pub(super) fn lo(&self, d: &Dim) -> i64 {
        d.terms
            .iter()
            .filter(|(_, m)| *m < 0)
            .map(|(r, m)| m * (self.extent(*r) - 1))
            .sum()
    }

    pub(super) fn span(&self, d: &Dim) -> i64 {
        1 + d
            .terms
            .iter()
            .map(|(r, m)| m.abs() * (self.extent(*r) - 1))
            .sum::<i64>()
    }

    pub(super) fn range_id_of(&self, r: Id) -> i64 {
        match self.g.arg(r) {
            Arg::Range { id, .. } => *id,
            _ => 0,
        }
    }

    /// `window.dims`: `acc`'s coordinates, largest stride first.
    pub(super) fn dims(&self, acc: &Access, coupled: &[Vec<Id>]) -> Vec<Dim> {
        let coef: Vec<(Id, i64)> = acc
            .coef
            .iter()
            .copied()
            .filter(|(r, c)| *c != 0 && self.extent(*r) > 1)
            .collect();
        let mut groups: Vec<Vec<Id>> = Vec::new();
        let mut by: Vec<i64> = Vec::new();
        for (r, c) in &coef {
            match by.iter().position(|s| *s == c.abs()) {
                Some(i) => groups[i].push(*r),
                None => {
                    by.push(c.abs());
                    groups.push(vec![*r]);
                }
            }
        }
        for s in coupled {
            let (hit, rest): (Vec<Vec<Id>>, Vec<Vec<Id>>) = groups
                .into_iter()
                .partition(|g| g.iter().any(|r| s.contains(r)));
            groups = rest;
            if !hit.is_empty() {
                groups.push(hit.into_iter().flatten().collect());
            }
        }
        let c = |r: Id| coef.iter().find(|(x, _)| *x == r).unwrap().1;
        let mut out: Vec<Dim> = groups
            .iter()
            .map(|g| {
                let stride = g.iter().fold(0, |a, r| gcd(a, c(*r).abs()));
                let terms = acc
                    .coef
                    .iter()
                    .filter(|(r, _)| g.contains(r))
                    .map(|(r, _)| (*r, c(*r) / stride))
                    .collect();
                Dim {
                    stride,
                    terms,
                    bx: (0, 0),
                }
            })
            .collect();
        out.sort_by_key(|d| -d.stride);
        for d in &mut out {
            d.bx = (0, self.span(d));
        }
        out
    }

    /// `window.coupled`: the sets of `rs` the gate `g` (over `rs`) couples.
    pub(super) fn coupled(&self, rs: &[Id], g: &[bool]) -> Vec<Vec<Id>> {
        let ext: Vec<i64> = rs.iter().map(|r| self.extent(*r)).collect();
        let on: Vec<Id> = rs.iter().copied().filter(|r| self.extent(*r) > 1).collect();
        let mut parent: HashMap<Id, Id> = on.iter().map(|r| (*r, *r)).collect();
        let root = |parent: &HashMap<Id, Id>, mut r: Id| {
            while parent[&r] != r {
                r = parent[&r];
            }
            r
        };
        let points = grid(&ext);
        for i in 0..rs.len() {
            for j in i + 1..rs.len() {
                if !parent.contains_key(&rs[i]) || !parent.contains_key(&rs[j]) {
                    continue;
                }
                let (ei, ej) = (ext[i] as usize, ext[j] as usize);
                let mut pair = vec![false; ei * ej];
                for (p, at) in points.iter().enumerate() {
                    if g[p] {
                        pair[at[i] as usize * ej + at[j] as usize] = true;
                    }
                }
                let row: Vec<bool> = (0..ei).map(|a| (0..ej).any(|b| pair[a * ej + b])).collect();
                let col: Vec<bool> = (0..ej).map(|b| (0..ei).any(|a| pair[a * ej + b])).collect();
                let product = (0..ei * ej).all(|x| pair[x] == (row[x / ej] && col[x % ej]));
                if !product {
                    let (ri, rj) = (root(&parent, rs[i]), root(&parent, rs[j]));
                    parent.insert(ri, rj);
                }
            }
        }
        let mut sets: Vec<(Id, Vec<Id>)> = Vec::new();
        for r in &on {
            let k = root(&parent, *r);
            match sets.iter_mut().find(|(x, _)| *x == k) {
                Some(s) => s.1.push(*r),
                None => sets.push((k, vec![*r])),
            }
        }
        sets.into_iter()
            .map(|(_, s)| s)
            .filter(|s| s.len() > 1)
            .collect()
    }

    /// `window.windowed`: a coordinate of more than one range.
    pub(super) fn windowed(&self, acc: &Access) -> bool {
        self.dims(acc, &[]).iter().any(|d| d.terms.len() > 1)
    }

    /// `window.gate_of`: `(ranges, the conjunction of the gate tables over
    /// them)`.
    pub(super) fn gate_of(&self, plan: &Plan, gates: &[usize]) -> (Vec<Id>, Vec<bool>) {
        let axes: Vec<Axes> = gates
            .iter()
            .map(|i| self.table_axes(&plan.accesses[*i]))
            .collect();
        let mut rs: Vec<Id> = Vec::new();
        for ax in &axes {
            for r in &ax.rs {
                if !rs.contains(r) {
                    rs.push(*r);
                }
            }
        }
        let ext: Vec<i64> = rs.iter().map(|r| self.extent(*r)).collect();
        let g = grid(&ext)
            .iter()
            .map(|at| {
                axes.iter().all(|ax| {
                    let st = ax.strides();
                    let flat: i64 = ax
                        .rs
                        .iter()
                        .enumerate()
                        .map(|(k, r)| st[k] * at[rs.iter().position(|x| x == r).unwrap()])
                        .sum();
                    ax.vals[flat as usize] != 0.0
                })
            })
            .collect();
        (rs, g)
    }

    /// `window.boxed`: `side` with the gate `g` over `rs` a box of its
    /// coordinates; not `exact`, each box bounds where the gate holds.
    pub(super) fn boxed(&self, side: &Side, rs: &[Id], g: &[bool], exact: bool) -> Option<Side> {
        let mine = side.reads();
        if rs.iter().any(|r| self.extent(*r) > 1 && !mine.contains(r)) {
            return None;
        }
        let touched: Vec<bool> = side
            .dims
            .iter()
            .map(|d| d.ranges().any(|r| rs.contains(&r)))
            .collect();
        let outside = side
            .dims
            .iter()
            .zip(&touched)
            .any(|(d, t)| *t && d.ranges().any(|r| !rs.contains(&r)));
        if outside {
            return None;
        }
        let ext: Vec<i64> = rs.iter().map(|r| self.extent(*r)).collect();
        let points = grid(&ext);
        let mut want = vec![true; points.len()];
        let mut out = side.clone();
        for (d, t) in out.dims.iter_mut().zip(&touched) {
            if !t {
                continue;
            }
            let lo = self.lo(d);
            let coord: Vec<i64> = points
                .iter()
                .map(|at| {
                    d.terms
                        .iter()
                        .map(|(r, s)| s * at[rs.iter().position(|x| x == r).unwrap()])
                        .sum::<i64>()
                        - lo
                })
                .collect();
            let on: Vec<i64> = coord
                .iter()
                .zip(g)
                .filter(|(_, g)| **g)
                .map(|(c, _)| *c)
                .collect();
            let (a, b) = (*on.iter().min()?, on.iter().max()? + 1);
            for (w, c) in want.iter_mut().zip(&coord) {
                *w &= a <= *c && *c < b;
            }
            d.bx = (a, b);
        }
        if exact && want != g {
            return None;
        }
        Some(out)
    }
}
