//! The L2 simulator: a body's values as arrays, each op on them directly
//! (`docs/spec/stack/l2/program.md`); instances of a `par` in a seeded order.

pub mod hosted;
pub mod map;
pub mod view;

mod cluster;
mod math;
mod node;

use std::collections::{HashMap, VecDeque};

use ktpu_l1::text::{Module, Term};

use crate::l1::term::Env;
use crate::mem::Dram;
use crate::rng::Rng;
use crate::units::cluster::{Cluster, Word};
use crate::units::link::Data;
use crate::R;

use view::{view, Buf, View};

/// A parameter's byte address.
pub type Binds = HashMap<String, i64>;

/// Keys of one node's units, its cores from [`CORE_KEY`].
const NODE_UNITS: i64 = 256;
const CORE_KEY: i64 = 128;

/// A value of an instance: `rows` x `cols` lanes in registers, or f16 codes
/// in L1 (a row statistic has one column).
#[derive(Clone, Debug)]
pub struct Rows {
    pub rows: usize,
    pub cols: usize,
    pub v: Vec<u32>,
}

impl Rows {
    fn at(&self, r: usize, c: usize) -> u32 {
        self.v[r * self.cols + c]
    }
}

pub struct Sim<'a> {
    mem: &'a mut Dram,
    bufs: HashMap<String, Buf>,
    placed: Binds,
    env: Env,
    rng: Rng,
    clusters: HashMap<i64, Cluster>,
    /// The node a `par ... on node` has reached.
    node: i64,
    core: Option<i64>,
    /// What units sent each other: rows to a core or a cluster, by [`Sim::key`].
    links: HashMap<i64, VecDeque<Rows>>,
    /// In a stage that adds accumulators: its adds and drains, for its end.
    deferring: bool,
    adds: Vec<(i64, Data)>,
    drains: Vec<(i64, Word)>,
}

/// Runs `fn name.l2`, a buffer `binds` names placed there and the others past
/// `mem`, `seed` ordering each `par`'s instances.
pub fn run(module: &Module, name: &str, binds: &Binds, mem: &mut Dram, seed: u64) -> R<()> {
    let body = module.body(name, "l2").map_err(|e| e.to_string())?;
    let mut bufs = HashMap::new();
    for (p, t) in &body.params {
        let at = *binds.get(p).ok_or(format!("{p} is not bound"))?;
        bufs.insert(p.clone(), Buf::of(t, at)?);
    }
    let mut sim = Sim {
        mem,
        bufs,
        placed: binds.clone(),
        env: Env::new(),
        rng: Rng::new(seed),
        clusters: HashMap::new(),
        node: 0,
        core: None,
        links: HashMap::new(),
        deferring: false,
        adds: Vec::new(),
        drains: Vec::new(),
    };
    sim.block(&body.stmts)
}

impl Sim<'_> {
    fn view(&self, t: &Term) -> R<View> {
        let shape = |n: &str| {
            let b = self.bufs.get(n).ok_or(format!("no buffer {n}"))?;
            Ok(b.shape.clone())
        };
        view(t, shape, &self.env)
    }

    fn buf(&self, name: &str) -> R<&Buf> {
        self.bufs.get(name).ok_or(format!("no buffer {name}"))
    }

    /// Unit `u` of `node`, as a map key.
    fn key(&self, node: i64, u: i64) -> i64 {
        node * NODE_UNITS + u
    }

    /// The core a vector instance runs on.
    fn env_core(&self) -> R<i64> {
        self.core.ok_or("a core's op outside a core".to_string())
    }

    /// The index in a `vc[I]` or `mg[I]` term.
    fn unit(&self, t: &Term) -> R<i64> {
        match t {
            Term::View(_, s) if s.len() == 1 => crate::l1::term::int(&s[0], &self.env),
            other => Err(format!("a unit, got {}", ktpu_l1::text::fmt(other))),
        }
    }

    /// The next `rows` rows sent to the unit at `key`.
    fn received(&mut self, key: i64, rows: usize) -> R<Rows> {
        let queue = self.links.get_mut(&key).ok_or("a recv before its send")?;
        let mut got: Option<Rows> = None;
        while got.as_ref().map_or(0, |g| g.rows) < rows {
            let next = queue.pop_front().ok_or("a recv past what was sent")?;
            match &mut got {
                None => got = Some(next),
                Some(g) if g.cols == next.cols => {
                    g.rows += next.rows;
                    g.v.extend(next.v);
                }
                Some(g) => return Err(format!("a recv of {} and {} columns", g.cols, next.cols)),
            }
        }
        got.ok_or("a recv of nothing".to_string())
    }
}
