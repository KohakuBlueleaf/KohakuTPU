//! Structurally equal subexpressions as one node: [`intern`] rebuilds an
//! expression bottom-up, each node keyed by its op and its operands'
//! canonical ids in operand order, a constant by its exact bits. No
//! reassociation: `sub(a, b)` and `sub(b, a)` stay two nodes, as do `0.0`
//! and `-0.0` and two NaN payloads.

use super::{Expr, OpName, RedOp, E};
use std::collections::HashMap;
use std::rc::Rc;

/// The reverse mutant: a key blind to operand order, which merges
/// `sub(a, b)` with `sub(b, a)`.
pub const UNORDERED_MUTANT: &str = "expr/cse-unordered";

#[derive(Hash, PartialEq, Eq)]
enum Key {
    Load(usize),
    Const(u64),
    Op(OpName, Vec<usize>),
    Reduce(RedOp, usize),
}

#[derive(Default)]
struct Table {
    /// A visited node's canonical id, by its pointer.
    seen: HashMap<*const Expr, usize>,
    ids: HashMap<Key, usize>,
    nodes: Vec<E>,
}

impl Table {
    fn id(&mut self, e: &E) -> usize {
        if let Some(id) = self.seen.get(&Rc::as_ptr(e)) {
            return *id;
        }
        let (key, node) = match &**e {
            Expr::Load(i) => (Key::Load(*i), Expr::Load(*i)),
            Expr::Const(v) => (Key::Const(v.to_bits()), Expr::Const(*v)),
            Expr::Op(op, args) => {
                let mut ids: Vec<usize> = args.iter().map(|a| self.id(a)).collect();
                let args = ids.iter().map(|i| self.nodes[*i].clone()).collect();
                if crate::mutants::on(UNORDERED_MUTANT) {
                    ids.sort_unstable();
                }
                (Key::Op(*op, ids), Expr::Op(*op, args))
            }
            Expr::Reduce(op, x) => {
                let i = self.id(x);
                (
                    Key::Reduce(*op, i),
                    Expr::Reduce(*op, self.nodes[i].clone()),
                )
            }
        };
        let next = self.nodes.len();
        let id = *self.ids.entry(key).or_insert(next);
        if id == next {
            self.nodes.push(Rc::new(node));
        }
        self.seen.insert(Rc::as_ptr(e), id);
        id
    }
}

/// `e` with each set of structurally equal subexpressions one shared node.
pub fn intern(e: &E) -> E {
    let mut t = Table::default();
    let id = t.id(e);
    t.nodes[id].clone()
}

/// Whether [`intern`] merges any two of `e`'s nodes.
pub fn merges(e: &E) -> bool {
    let mut t = Table::default();
    t.id(e);
    t.nodes.len() < t.seen.len()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn c(v: f64) -> E {
        Rc::new(Expr::Const(v))
    }

    fn add(a: E, b: E) -> E {
        Rc::new(Expr::Op(OpName::Add, vec![a, b]))
    }

    /// `add(a, b)` interned has one node for both operands.
    fn merged(a: E, b: E) -> bool {
        let Expr::Op(_, args) = &*intern(&add(a, b)) else {
            unreachable!()
        };
        Rc::ptr_eq(&args[0], &args[1])
    }

    #[test]
    fn equal_bits_and_ordered_operands_merge_and_nothing_else() {
        assert!(merged(c(1.0), c(1.0)));
        assert!(!merged(c(0.0), c(-0.0)));
        let nan = |p: u64| c(f64::from_bits(0x7ff8_0000_0000_0000 | p));
        assert!(merged(nan(1), nan(1)));
        assert!(!merged(nan(1), nan(2)));
        assert!(merged(add(c(1.0), c(2.0)), add(c(1.0), c(2.0))));
        assert!(!merged(add(c(1.0), c(2.0)), add(c(2.0), c(1.0))));
    }

    #[test]
    fn a_merged_tree_counts_its_nodes_once() {
        let x = || Rc::new(Expr::Load(0));
        let sq = |a: E| Rc::new(Expr::Op(OpName::Mul, vec![a.clone(), a]));
        let e = add(sq(add(x(), c(1.0))), sq(add(x(), c(1.0))));
        assert_eq!(super::super::count(&e), 5);
        assert_eq!(super::super::count(&intern(&e)), 3);
        assert!(merges(&e));
        assert!(!merges(&intern(&e)));
    }

    #[cfg(feature = "mutants")]
    #[test]
    fn the_unordered_mutant_merges_swapped_operands() {
        let swapped = || merged(add(c(1.0), c(2.0)), add(c(2.0), c(1.0)));
        assert!(!swapped());
        assert!(crate::mutants::with(UNORDERED_MUTANT, swapped));
    }
}
