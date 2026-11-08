//! Persistent append histories. A block bounds completion from above and start
//! from below, so a query skips only accesses proved ordered in one direction.
//! Starts need not be monotone: an instruction fetch can span earlier DMA work.

use std::rc::Rc;

use crate::model::clock::Clock;

use super::Access;

#[derive(Clone, PartialEq, Eq)]
struct Tree {
    len: usize,
    first: u32,
    earliest: Clock,
    last: Rc<Access>,
    children: Option<(Rc<Tree>, Rc<Tree>)>,
}

impl Tree {
    fn surviving(self: &Rc<Self>, floor: &Clock, out: &mut Vec<Rc<Tree>>) {
        if self.last.done <= floor.at(self.last.owner) {
            return;
        }
        if self.first > floor.at(self.last.owner) {
            out.push(self.clone());
            return;
        }
        if let Some((left, right)) = &self.children {
            left.surviving(floor, out);
            right.surviving(floor, out);
        }
    }

    fn leaf(access: &Rc<Access>) -> Rc<Self> {
        Rc::new(Self {
            len: 1,
            first: access.done,
            earliest: access.start.clone(),
            last: access.clone(),
            children: None,
        })
    }

    fn join(left: Rc<Self>, right: Rc<Self>) -> Rc<Self> {
        Rc::new(Self {
            len: left.len + right.len,
            first: left.first,
            earliest: left.earliest.meet(&right.earliest),
            last: right.last.clone(),
            children: Some((left, right)),
        })
    }

    fn conflicts(
        &self,
        current: &Access,
        visit: &mut impl FnMut(Option<&Access>) -> Result<(), ()>,
    ) -> Result<(), ()> {
        visit(None)?;
        if self.last.done <= current.start.at(self.last.owner)
            || current.done <= self.earliest.at(current.owner)
        {
            return Ok(());
        }
        match &self.children {
            Some((left, right)) => {
                left.conflicts(current, visit)?;
                right.conflicts(current, visit)
            }
            None => visit(Some(&self.last)),
        }
    }
}

#[derive(Clone, PartialEq, Eq)]
struct Block {
    first: u32,
    tree: Rc<Tree>,
    rest: History,
}

/// A binary forest, newest block first. Appending merges equal-sized blocks;
/// splitting a physical interval shares its entire history in O(1).
#[derive(Clone, Default, PartialEq, Eq)]
pub(super) struct History(Option<Rc<Block>>);

impl History {
    pub fn retained(&self, stats: &mut crate::sim::stats::Retained) {
        fn tree_visit(tree: &Rc<Tree>, stats: &mut crate::sim::stats::Retained) {
            if !stats.object("dram_trees", Rc::as_ptr(tree) as usize) {
                return;
            }
            stats.clock(&tree.earliest);
            if stats.object("dram_accesses", Rc::as_ptr(&tree.last) as usize) {
                stats.clock(&tree.last.start);
            }
            if let Some((left, right)) = &tree.children {
                tree_visit(left, stats);
                tree_visit(right, stats);
            }
        }
        let mut block = &self.0;
        while let Some(head) = block {
            if !stats.object("dram_blocks", Rc::as_ptr(head) as usize) {
                break;
            }
            tree_visit(&head.tree, stats);
            block = &head.rest.0;
        }
    }

    pub fn first(&self) -> Option<u32> {
        self.0.as_ref().map(|head| head.first)
    }

    /// Cut a completed prefix and share all untouched suffix subtrees.
    pub fn retire(&mut self, floor: &Clock) {
        crate::work::visit_retirement();
        if let Some(head) = &self.0 {
            let last = &head.tree.last;
            if last.done <= floor.at(last.owner) {
                *self = Self::default();
                return;
            }
        }
        let mut trees = Vec::new();
        let mut block = &self.0;
        while let Some(head) = block {
            trees.push(head.tree.clone());
            block = &head.rest.0;
        }
        let mut live = Vec::new();
        for tree in trees.into_iter().rev() {
            tree.surviving(floor, &mut live);
        }
        *self = Self::default();
        for tree in live {
            self.push_tree(tree);
        }
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_none()
    }

    pub fn push(&mut self, access: &Rc<Access>) {
        self.push_tree(Tree::leaf(access));
    }

    fn push_tree(&mut self, mut tree: Rc<Tree>) {
        let first = self.first().unwrap_or(tree.first);
        while let Some(head) = &self.0 {
            if head.tree.len != tree.len {
                break;
            }
            tree = Tree::join(head.tree.clone(), tree);
            *self = head.rest.clone();
        }
        self.0 = Some(Rc::new(Block {
            first,
            tree,
            rest: self.clone(),
        }));
    }

    pub fn conflicts(
        &self,
        current: &Access,
        mut visit: impl FnMut(Option<&Access>) -> Result<(), ()>,
    ) -> Result<(), ()> {
        let mut block = &self.0;
        while let Some(head) = block {
            head.tree.conflicts(current, &mut visit)?;
            block = &head.rest.0;
        }
        Ok(())
    }
}
