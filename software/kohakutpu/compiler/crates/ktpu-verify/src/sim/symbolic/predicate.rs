//! A conjunction of BDDs with disjoint fresh-input supports.
//!
//! Keeping bits of one input together is conservative: it can merge more than
//! necessary, but never treats correlated predicates as independent.

use std::collections::{BTreeMap, BTreeSet};

use super::{Arena, Bit};

#[derive(Clone, Debug, Default)]
pub(crate) struct Predicate {
    impossible: bool,
    factors: BTreeMap<Bit, Vec<usize>>,
    owners: BTreeMap<usize, Bit>,
}

impl Predicate {
    pub(crate) fn is_false(&self) -> bool {
        self.impossible
    }

    fn overlapping(&self, support: &[usize]) -> BTreeSet<Bit> {
        support
            .iter()
            .filter_map(|input| self.owners.get(input).copied())
            .collect()
    }

    pub(super) fn restrict(&mut self, arena: &mut Arena, condition: Bit) {
        if !self.try_restrict(arena, condition) {
            self.impossible = true;
            self.factors.clear();
            self.owners.clear();
        }
    }

    /// Only factors sharing an input with the new condition need conjunction.
    /// All other factors are satisfiable over disjoint variables. On failure,
    /// retain the incoming predicate, including for terminal-state identity.
    pub(super) fn try_restrict(&mut self, arena: &mut Arena, condition: Bit) -> bool {
        if self.impossible || condition == 1 || self.factors.contains_key(&condition) {
            return !self.impossible;
        }
        let mut root = condition;
        let mut support = arena.support(condition);
        let touched = self.overlapping(&support);
        for &factor in &touched {
            root = arena.and(root, factor);
            if root == 0 {
                break;
            }
        }
        if root == 0 {
            return false;
        }
        if touched.len() == 1 && touched.contains(&root) {
            return true;
        }
        if !touched.is_empty() {
            // Reduction can remove variables. Index the resulting diagram, not
            // the union of its operands' supports.
            support = arena.support(root);
            for factor in touched {
                for input in self.factors.remove(&factor).expect("indexed factor") {
                    self.owners.remove(&input);
                }
            }
        }
        if root != 1 {
            for &input in &support {
                self.owners.insert(input, root);
            }
            self.factors.insert(root, support);
        }
        true
    }

    fn implies(&self, other: &Self, arena: &mut Arena) -> bool {
        if self.impossible {
            return true;
        }
        if other.impossible {
            return false;
        }
        for (&factor, support) in &other.factors {
            if self.factors.contains_key(&factor) {
                continue;
            }
            // P => q iff P & !q is unsatisfiable. Factors of P disjoint
            // from q are nonfalse and independent, so cannot change this test.
            let mut counterexample = arena.not(factor);
            for own in self.overlapping(support) {
                counterexample = arena.and(counterexample, own);
                if counterexample == 0 {
                    break;
                }
            }
            if counterexample != 0 {
                return false;
            }
        }
        true
    }

    /// Factor grouping is not canonical: a conjunction may be supplied in one
    /// operation or several. Compare semantics, not grouping, at state joins.
    pub(super) fn equivalent(&self, other: &Self, arena: &mut Arena) -> bool {
        if self.impossible == other.impossible && self.factors.keys().eq(other.factors.keys()) {
            return true;
        }
        self.implies(other, arena) && other.implies(self, arena)
    }
}
