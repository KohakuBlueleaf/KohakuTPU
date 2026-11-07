//! Stage dependency queries over resource/owner buckets and interval trees.
//!
//! The original accesses retain their order and footprints. Bounds prune a
//! query; `overlap` decides its leaves after the shared mode predicate.
//! Hoisting queries bounds without applying the intra-unit or flowing-transfer
//! exemptions.

use std::collections::BTreeMap;

use super::{clash, may_clash, on, overlap, Access};

struct Entry<'a> {
    ordinal: usize,
    access: &'a Access,
    lo: i64,
    hi: i64,
    max_hi: i64,
}

struct Bucket<'a> {
    representative: &'a Access,
    ordered: Vec<(usize, &'a Access)>,
    tree: Vec<Entry<'a>>,
}

impl<'a> Bucket<'a> {
    fn finish(&mut self) {
        self.tree = self
            .ordered
            .iter()
            .map(|&(ordinal, access)| {
                let (lo, hi) = access.range();
                Entry {
                    ordinal,
                    access,
                    lo,
                    hi,
                    max_hi: hi,
                }
            })
            .collect();
        self.tree.sort_by_key(|e| (e.lo, e.ordinal));
        fn augment(tree: &mut [Entry<'_>]) -> i64 {
            if tree.is_empty() {
                return i64::MIN;
            }
            let mid = tree.len() / 2;
            let (left, rest) = tree.split_at_mut(mid);
            let (root, right) = rest.split_at_mut(1);
            root[0].max_hi = root[0].hi.max(augment(left)).max(augment(right));
            root[0].max_hi
        }
        augment(&mut self.tree);
    }

    fn meets(&self, access: &Access, exact: bool) -> bool {
        if exact && on("l2.sync/whole-buffer") {
            return self.ordered.iter().any(|(_, b)| overlap(access, b));
        }
        fn query(tree: &[Entry<'_>], access: &Access, lo: i64, hi: i64, exact: bool) -> bool {
            if tree.is_empty() {
                return false;
            }
            let mid = tree.len() / 2;
            let root = &tree[mid];
            if root.max_hi <= lo || tree[0].lo >= hi {
                return false;
            }
            if root.lo < hi && lo < root.hi && (!exact || overlap(access, root.access)) {
                return true;
            }
            query(&tree[..mid], access, lo, hi, exact)
                || query(&tree[mid + 1..], access, lo, hi, exact)
        }
        let (lo, hi) = access.range();
        query(&self.tree, access, lo, hi, exact)
    }
}

/// One immutable stage's effects, partitioned by allocation and access mode.
/// Every returned reference is an original access, never a widened union.
pub(crate) struct AccessIndex<'a> {
    accesses: &'a [Access],
    keys: BTreeMap<(&'a str, &'a str), Vec<Bucket<'a>>>,
}

impl<'a> AccessIndex<'a> {
    pub(crate) fn new(accesses: &'a [Access]) -> Self {
        // A short stage uses its borrowed access list without allocating trees.
        if accesses.len() <= 32 {
            return Self {
                accesses,
                keys: BTreeMap::new(),
            };
        }
        let mut keys: BTreeMap<(&str, &str), Vec<Bucket<'a>>> = BTreeMap::new();
        for (ordinal, access) in accesses.iter().enumerate() {
            let buckets = keys.entry((&access.key.0, &access.key.1)).or_default();
            let at = buckets.iter().position(|b| {
                let a = b.representative;
                a.unit == access.unit && a.write == access.write && a.flow == access.flow
            });
            let at = match at {
                Some(at) => at,
                None => {
                    buckets.push(Bucket {
                        representative: access,
                        ordered: Vec::new(),
                        tree: Vec::new(),
                    });
                    buckets.len() - 1
                }
            };
            buckets[at].ordered.push((ordinal, access));
        }
        for buckets in keys.values_mut() {
            for bucket in buckets {
                bucket.finish();
            }
        }
        Self { accesses, keys }
    }

    /// First conflicting original access per bucket, in original access order.
    /// Wait emission consumes only the owner and stage of these representatives.
    pub(crate) fn pending(
        &self,
        next: &AccessIndex<'_>,
        skip: impl Fn(&str) -> bool,
    ) -> Vec<&'a Access> {
        if self.keys.is_empty() || next.keys.is_empty() {
            return self
                .accesses
                .iter()
                .filter(|a| !skip(&a.unit) && next.accesses.iter().any(|b| clash(a, b)))
                .collect();
        }
        let mut found = Vec::new();
        if on("l2.sync/never-clash") {
            return Vec::new();
        }
        for (key, buckets) in &self.keys {
            let Some(others) = next.keys.get(key) else {
                continue;
            };
            for bucket in buckets {
                let a = bucket.representative;
                if skip(&a.unit) {
                    continue;
                }
                let eligible: Vec<_> = others
                    .iter()
                    .filter(|other| may_clash(a, other.representative))
                    .collect();
                if eligible.is_empty() {
                    continue;
                }
                if let Some(&(ordinal, access)) = bucket
                    .ordered
                    .iter()
                    .find(|(_, a)| eligible.iter().any(|b| b.meets(a, true)))
                {
                    found.push((ordinal, access));
                }
            }
        }
        found.sort_by_key(|(ordinal, _)| *ordinal);
        found.into_iter().map(|(_, access)| access).collect()
    }

    /// Hoisting is conservative over the original access bounds, not footprints.
    pub(crate) fn blocks(&self, next: &AccessIndex<'_>) -> bool {
        if self.keys.is_empty() || next.keys.is_empty() {
            return self.accesses.iter().any(|a| {
                next.accesses.iter().any(|b| {
                    let (alo, ahi) = a.range();
                    let (blo, bhi) = b.range();
                    a.key == b.key && (a.write || b.write) && alo < bhi && blo < ahi
                })
            });
        }
        for (key, buckets) in &self.keys {
            let Some(others) = next.keys.get(key) else {
                continue;
            };
            for bucket in buckets {
                for other in others {
                    if !(bucket.representative.write || other.representative.write) {
                        continue;
                    }
                    if bucket.ordered.iter().any(|(_, a)| other.meets(a, false)) {
                        return true;
                    }
                }
            }
        }
        false
    }
}
