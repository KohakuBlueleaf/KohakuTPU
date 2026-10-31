//! An L1 `.ktpu` body to what a card receives (L1.LOW.*): a node program
//! built against the cores' residents, and the package for the node's
//! dispatch engine.

use std::collections::BTreeMap;

use ktpu_l0::{
    engine, Addresses, BuildOpts, Coord, Facts, IsaAddresses, Op, Package, PackageBuilder, Resident,
};
use ktpu_machine::Machine;

use crate::error::Result;
use crate::layout::buffer::Buffer;
use crate::layout::scratch::Arena;
use crate::lower::image::{State, UNKNOWN};
use crate::lower::imem::Cores;
use crate::lower::node::{self, NodeProgram};
use crate::lower::target::Target;
use crate::mutant::on;
use crate::text::Module;

thread_local! {
    /// The residents `l1.lower/hook-without-resident` builds against when the
    /// caller gives none, kept across builds as the Python mutant keeps one.
    static HELD: std::cell::Cell<Option<Cores>> = const { std::cell::Cell::new(None) };
}

/// Drop the residents a mutant held (each spec run starts without).
pub fn forget_held() {
    HELD.set(None);
}

/// How one node program is built.
#[derive(Default)]
pub struct Opts<'a> {
    /// The memory port units stream their words from.
    pub fetch: Option<Coord>,
    /// The cores' residents (None: no lowering hook runs, every word is sent).
    pub cores: Option<&'a mut Cores>,
    /// Address fields inside the program's spans become relocations.
    pub relocate: bool,
}

/// `{node: program}` of `fn name.l1` on `machine` (a card of `nodes`, or one
/// mesh), each parameter bound to an address.
pub fn programs(
    module: &Module,
    name: &str,
    binds: &BTreeMap<String, i128>,
    machine: &Machine,
    nodes: Option<u32>,
    entry: Option<State>,
) -> Result<Vec<NodeProgram>> {
    let target = Target::new(machine, nodes, 0);
    node::programs(module, name, binds, &target, entry.unwrap_or(UNKNOWN))
}

/// [`programs`] with the scratch parameters in arenas: `binds` holds the
/// caller's parameters, `arenas` each arena of `layout::scratch::arenas`
/// with its base. A member binds at base + offset, and the arena is one span
/// (one package buffer); each caller parameter is its own.
pub fn programs_in(
    module: &Module,
    name: &str,
    binds: &BTreeMap<String, i128>,
    arenas: &[(Arena, u64)],
    machine: &Machine,
    nodes: Option<u32>,
    entry: Option<State>,
) -> Result<Vec<NodeProgram>> {
    let mut all = binds.clone();
    let mut spans: BTreeMap<u64, u64> = BTreeMap::new();
    for (param, t) in &module.body(name, "l1")?.params {
        if let Some(&at) = binds.get(param) {
            let e = spans.entry(at as u64).or_insert(0);
            *e = (*e).max(Buffer::of(t)?.bytes(machine)?);
        }
    }
    for (a, base) in arenas {
        for (param, offset) in &a.members {
            all.insert(param.clone(), (base + offset) as i128);
        }
        let e = spans.entry(*base).or_insert(0);
        *e = (*e).max(a.bytes);
    }
    let target = Target::new(machine, nodes, 0);
    node::programs_spanned(module, name, &all, spans, &target, entry.unwrap_or(UNKNOWN))
}

/// What building reads of one machine: its address fields, made once.
pub struct Builder<'m> {
    pub machine: &'m Machine,
    pub addresses: IsaAddresses,
}

impl<'m> Builder<'m> {
    pub fn new(machine: &'m Machine) -> Result<Builder<'m>> {
        Ok(Builder {
            machine,
            addresses: IsaAddresses::new(machine)?,
        })
    }

    /// `Program.build` of one node program (L1.LOW.001-.011, .013).
    pub fn build(&self, np: &NodeProgram, opts: Opts<'_>) -> Result<PackageBuilder> {
        let mut f = Facts::of_mesh(self.machine, np.mesh)?;
        if on("l1.lower/credit") {
            f.inst_depth *= 2;
        }
        let mut held = match opts.cores.is_none() && on("l1.lower/hook-without-resident") {
            true => Some(match HELD.take() {
                Some(c) => c,
                None => Cores::new(self.machine)?,
            }),
            false => None,
        };
        let mut cores = opts.cores.or(held.as_mut());
        let explicit_repeat = np
            .program
            .steps
            .iter()
            .any(|s| matches!(s, ktpu_l0::Stmt::Repeat { .. } | ktpu_l0::Stmt::Jump { .. }));
        if explicit_repeat {
            if let Some(c) = cores.as_deref_mut() {
                c.cores.clear();
            }
        }
        let built = {
            let resident: Option<&mut dyn Resident> = match cores.as_deref_mut() {
                Some(_) if explicit_repeat || on("l1.lower/no-hook") => None,
                Some(c) => Some(c),
                None => None,
            };
            let o = BuildOpts {
                fetch: opts.fetch,
                resident,
                addresses: Some(&self.addresses as &dyn Addresses),
                spans: (opts.relocate && !on("l1.lower/no-spans")).then_some(&np.program.spans),
            };
            np.program.build(&f, o)
        };
        if let Some(c) = cores {
            c.take_error()?;
        }
        if held.is_some() {
            HELD.set(held);
        }
        let mut b = built?;
        if on("l1.lower/jump-unresolved") {
            for s in b.steps.iter_mut().filter(|s| s.op == Op::Jump) {
                s.unit = 0;
            }
        }
        if on("l1.lower/stream-reversed") {
            reverse_dispatches(&mut b);
        }
        Ok(b)
    }

    /// `build.package`: one node's package for the dispatch engine with no
    /// buffer defaults, and its bindings (L1.LOW.012).
    pub fn package(&self, np: &NodeProgram, opts: Opts<'_>) -> Result<(Package, Vec<u64>)> {
        let f = Facts::of_mesh(self.machine, np.mesh)?;
        let b = self.build(np, opts)?;
        let pkg = b.build(false, false);
        let pkg = if on("l1.lower/no-engine") {
            pkg
        } else {
            engine::lower(&pkg, &f)?
        };
        Ok((pkg, b.bindings()))
    }
}

/// Each DISPATCH's words in reverse order, its relocations moved with them:
/// the stream a builder dispatching each piece backwards writes.
fn reverse_dispatches(b: &mut PackageBuilder) {
    for s in b.steps.iter().filter(|s| s.op == Op::Dispatch) {
        let (first, n) = (s.arg, s.count);
        b.payloads[first as usize..(first + n) as usize].reverse();
        for r in b.relocs.iter_mut() {
            if (first..first + n).contains(&r.payload) {
                r.payload = first + (first + n - 1 - r.payload);
            }
        }
    }
}

/// [`Builder::build`] on a builder made for this one call.
pub fn build(np: &NodeProgram, machine: &Machine, opts: Opts<'_>) -> Result<PackageBuilder> {
    Builder::new(machine)?.build(np, opts)
}

/// [`Builder::package`] on a builder made for this one call.
pub fn package(np: &NodeProgram, machine: &Machine, opts: Opts<'_>) -> Result<(Package, Vec<u64>)> {
    Builder::new(machine)?.package(np, opts)
}
