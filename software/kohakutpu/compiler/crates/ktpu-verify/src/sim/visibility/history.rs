//! Persistent response prefixes indexed by the issuer's monotone event count.

use std::rc::Rc;

use crate::model::clock::Clock;

#[derive(Clone, PartialEq, Eq)]
struct Tree {
    len: usize,
    first: u32,
    last: (u32, Clock),
    children: Option<(Rc<Tree>, Rc<Tree>)>,
}

impl Tree {
    fn second(&self) -> Option<u32> {
        let (left, right) = self.children.as_ref()?;
        if left.len == 1 {
            Some(right.first)
        } else {
            left.second()
        }
    }

    fn surviving(&self, issued: u32, out: &mut Vec<(u32, Clock)>) {
        if self.last.0 <= issued {
            out.clear();
            out.push(self.last.clone());
        } else if let Some((left, right)) = &self.children {
            left.surviving(issued, out);
            right.surviving(issued, out);
        } else {
            out.push(self.last.clone());
        }
    }

    fn through(&self, issued: u32) -> Option<&Clock> {
        if issued < self.first {
            return None;
        }
        if self.last.0 <= issued {
            return Some(&self.last.1);
        }
        let (left, right) = self.children.as_ref()?;
        if right.first <= issued {
            right.through(issued)
        } else {
            left.through(issued)
        }
    }
}

#[derive(Clone, PartialEq, Eq)]
struct Block {
    tree: Rc<Tree>,
    rest: Marks,
}

/// Newest binary block first; snapshots share every completed prefix.
#[derive(Clone, Default, PartialEq, Eq)]
pub(super) struct Marks(Option<Rc<Block>>);

impl Marks {
    pub fn retained(&self, stats: &mut crate::sim::stats::Retained) -> usize {
        fn visit(tree: &Rc<Tree>, stats: &mut crate::sim::stats::Retained) {
            if !stats.object("response_trees", Rc::as_ptr(tree) as usize) {
                return;
            }
            stats.clock(&tree.last.1);
            if let Some((left, right)) = &tree.children {
                visit(left, stats);
                visit(right, stats);
            }
        }
        let mut count = 0;
        let mut block = &self.0;
        while let Some(head) = block {
            count += head.tree.len;
            stats.object("response_blocks", Rc::as_ptr(head) as usize);
            visit(&head.tree, stats);
            block = &head.rest.0;
        }
        count
    }

    /// Retain the predecessor at the query floor and every later response.
    pub fn retire(&mut self, issued: u32) {
        let mut trees = Vec::new();
        let mut block = &self.0;
        let mut count = 0;
        while let Some(head) = block {
            count += head.tree.len;
            trees.push(&head.tree);
            block = &head.rest.0;
        }
        // Collection keeps one predecessor. Until a second mark reaches
        // the floor there is no discardable prefix, even if the first does.
        let second = trees.last().and_then(|tree| {
            tree.second()
                .or_else(|| trees.iter().rev().nth(1).map(|next| next.first))
        });
        if second.is_none_or(|epoch| epoch > issued) {
            return;
        }
        let mut live = Vec::new();
        for tree in trees.into_iter().rev() {
            tree.surviving(issued, &mut live);
        }
        if live.len() != count {
            *self = Self::default();
            for mark in live {
                self.push(mark);
            }
        }
    }

    pub fn push(&mut self, mark: (u32, Clock)) {
        let mut tree = Rc::new(Tree {
            len: 1,
            first: mark.0,
            last: mark,
            children: None,
        });
        while let Some(head) = &self.0 {
            if head.tree.len != tree.len {
                break;
            }
            tree = Rc::new(Tree {
                len: head.tree.len + tree.len,
                first: head.tree.first,
                last: tree.last.clone(),
                children: Some((head.tree.clone(), tree)),
            });
            *self = head.rest.clone();
        }
        self.0 = Some(Rc::new(Block {
            tree,
            rest: self.clone(),
        }));
    }

    pub fn through(&self, issued: u32) -> Option<&Clock> {
        let mut block = &self.0;
        while let Some(head) = block {
            if head.tree.first <= issued {
                return head.tree.through(issued);
            }
            block = &head.rest.0;
        }
        None
    }

    pub fn last(&self) -> Option<&Clock> {
        self.0.as_ref().map(|head| &head.tree.last.1)
    }
}
