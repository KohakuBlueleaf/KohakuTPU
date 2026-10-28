//! The stage crates as the pipeline's [`Backend`].
//!
//! graph -> L2 (`ktpu_graph`, the tracer's text), the passes and the
//! lowering (`ktpu_l2`), the image scheduler and the node programs
//! (`ktpu_l1`), the dispatch order and the checker (`ktpu_verify`). A stage
//! whose crate publishes no version is not cached.

pub mod lines;

use crate::output::{Finding, PackageOut, Scratch};
use crate::pipeline::stage::{Backend, Stage};
use ktpu_l1::layout::scratch::Arena;
use ktpu_l1::lower::imem::Cores;
use ktpu_l1::text::Module as L1;
use ktpu_l2::{Program, Search, Target};
use ktpu_machine::{Machine, Refusal};
use std::collections::BTreeMap;

/// The Rust stack: packages built with no fetch port (units are sent their
/// words by the node).
#[derive(Clone, Copy, Debug, Default)]
pub struct Stack;

/// The Rust stack as a card runs it: [`Stack`] with packages built (and
/// checked) to fetch through the die's fetch port (`die.fetch_port`), as the
/// card runtime builds them; with `binds`, built at those addresses (each
/// parameter's) instead of relocatable.
#[derive(Clone, Debug, Default)]
pub struct CardStack {
    pub binds: Option<BTreeMap<String, i128>>,
    /// The node sends the units their words (no fetch port), as [`Stack`].
    pub node_fed: bool,
    /// The L2 lowered as written: no L2 -> L2 pass (a reference build the
    /// passes' output is compared against).
    pub no_passes: bool,
    /// Offline class derivation's representative search, then frozen replay.
    pub template_search: Option<std::sync::Arc<Search>>,
    /// All rotation operands belong to the template's bind-time domain.
    pub parameter_rotations: bool,
}

impl CardStack {
    fn fetch(&self, m: &Machine) -> Option<ktpu_l0::Coord> {
        (!self.node_fed).then(|| fetch_port(m))
    }
}

/// The clause a stage not wired yet refuses with (none now; kept for the
/// tests that count such stages).
pub const NOT_PORTED: &str = "STACK.PORT";

/// A text-layer error as a refusal (its message names the file and line).
fn refusal(e: ktpu_l1::Error) -> Refusal {
    Refusal::new(e.clause.clone(), e.to_string())
}

/// The file names texts are read under (in refusal messages).
const L2_FILE: &str = "<l2>";
const L1_FILE: &str = "<l1>";

impl Backend for Stack {
    type L2 = Program;
    type L1 = L1;
    type Programs = Programs;

    /// The stage's sources' digest (`build/digest.rs`), and the mutant on.
    fn version(&self, stage: Stage) -> Option<String> {
        let v = match stage {
            Stage::Graph => env!("KTPU_STAGE_GRAPH"),
            Stage::Passes => env!("KTPU_STAGE_PASSES"),
            Stage::Lower => env!("KTPU_STAGE_LOWER"),
            Stage::Schedule => env!("KTPU_STAGE_SCHEDULE"),
            Stage::Order => env!("KTPU_STAGE_ORDER"),
            Stage::Verify => env!("KTPU_STAGE_VERIFY"),
        };
        Some(match crate::mutant::active() {
            Some(m) => format!("{v}+mutant:{m}"),
            None => v.to_string(),
        })
    }

    fn graph_to_l2(&self, graph_json: &str, m: &Machine) -> Result<Program, Refusal> {
        let g = ktpu_graph::parse(graph_json)?;
        ktpu_graph::check_machine(&g, m)?;
        let g = lines::isolate(g, m);
        let text = ktpu_graph::lower(&g)?.text();
        Program::parse(&text, L2_FILE, None).map_err(refusal)
    }

    fn parse_l2(&self, text: &str) -> Result<Program, Refusal> {
        Program::parse(text, L2_FILE, None).map_err(refusal)
    }

    fn print_l2(&self, l2: &Program) -> String {
        l2.text().to_string()
    }

    fn l2_name(&self, l2: &Program) -> Result<String, Refusal> {
        Ok(l2.name.clone())
    }

    fn caller(&self, l2: &Program, name: &str) -> Result<Vec<String>, Refusal> {
        let body = l2.module.body(name, "l2").map_err(refusal)?;
        Ok(body.params.iter().map(|(p, _)| p.clone()).collect())
    }

    fn passes(&self, l2: &Program, name: &str, m: &Machine) -> Result<Program, Refusal> {
        let p = match l2.name == name {
            true => l2.clone(),
            false => Program::parse(l2.text(), L2_FILE, Some(name)).map_err(refusal)?,
        };
        let t = Target::of(m);
        let p = p.with_vregs(t.vregs, t.vcopies);
        ktpu_l2::passes::run(&p, &ktpu_l2::PASSES, &t, &Search::default()).map_err(refusal)
    }

    fn lower(&self, l2: &Program, name: &str, m: &Machine) -> Result<L1, Refusal> {
        let p = match l2.name == name {
            true => l2.clone(),
            false => Program::parse(l2.text(), L2_FILE, Some(name)).map_err(refusal)?,
        };
        let t = Target::of(m);
        let p = p.with_vregs(t.vregs, t.vcopies);
        let text = ktpu_l2::lower::lower(&p, &t, &Search::default()).map_err(refusal)?;
        ktpu_l1::text::read(&text, L1_FILE).map_err(refusal)
    }

    fn parse_l1(&self, text: &str) -> Result<L1, Refusal> {
        ktpu_l1::text::read(text, L1_FILE).map_err(refusal)
    }

    fn print_l1(&self, l1: &L1) -> String {
        l1.source.clone()
    }

    fn schedule(&self, l1: &L1, m: &Machine) -> Result<L1, Refusal> {
        ktpu_l1::sched::schedule_module(&on_core(l1, m)?, m).map_err(refusal)
    }

    fn programs(
        &self,
        l1: &L1,
        name: &str,
        caller: Option<&[String]>,
        m: &Machine,
    ) -> Result<Programs, Refusal> {
        programs(&on_core(l1, m)?, name, m, None, caller)
    }

    fn scratch(&self, p: &Programs) -> Vec<Scratch> {
        p.scratch.clone()
    }

    fn order(&self, p: &Programs, m: &Machine) -> Result<BTreeMap<u32, PackageOut>, Refusal> {
        order(p, m, None)
    }

    fn check(&self, p: &Programs, m: &Machine) -> Result<Vec<Finding>, Refusal> {
        check(p, m, None)
    }

    fn refuses(&self, f: &Finding) -> bool {
        ktpu_verify::REFUSE.contains(&f.kind.as_str())
    }
}

impl Backend for CardStack {
    type L2 = Program;
    type L1 = L1;
    type Programs = Programs;

    /// [`Stack`]'s, with the stages that build against the fetch port (and
    /// at `binds`) keyed apart from it.
    fn version(&self, stage: Stage) -> Option<String> {
        if self.template_search.is_some() {
            return None;
        }
        let v = Stack.version(stage)?;
        Some(match stage {
            Stage::Order | Stage::Verify => {
                format!("{v}+fetch:{}+{:?}", !self.node_fed, self.binds)
            }
            Stage::Passes if self.no_passes => format!("{v}+none"),
            _ => v,
        })
    }

    fn graph_to_l2(&self, graph_json: &str, m: &Machine) -> Result<Program, Refusal> {
        Stack.graph_to_l2(graph_json, m)
    }

    fn parse_l2(&self, text: &str) -> Result<Program, Refusal> {
        Stack.parse_l2(text)
    }

    fn print_l2(&self, l2: &Program) -> String {
        Stack.print_l2(l2)
    }

    fn l2_name(&self, l2: &Program) -> Result<String, Refusal> {
        Stack.l2_name(l2)
    }

    fn caller(&self, l2: &Program, name: &str) -> Result<Vec<String>, Refusal> {
        Stack.caller(l2, name)
    }

    fn passes(&self, l2: &Program, name: &str, m: &Machine) -> Result<Program, Refusal> {
        if !self.no_passes && self.template_search.is_none() {
            return Stack.passes(l2, name, m);
        }
        let p = match l2.name == name {
            true => l2.clone(),
            false => Program::parse(l2.text(), L2_FILE, Some(name)).map_err(refusal)?,
        };
        let t = Target::of(m);
        let p = p.with_vregs(t.vregs, t.vcopies);
        if self.no_passes {
            return Ok(p);
        }
        ktpu_l2::passes::run(
            &p,
            &ktpu_l2::PASSES,
            &t,
            self.template_search.as_ref().expect("search set above"),
        )
        .map_err(refusal)
    }

    fn lower(&self, l2: &Program, name: &str, m: &Machine) -> Result<L1, Refusal> {
        let Some(search) = &self.template_search else {
            if self.parameter_rotations {
                return Err(Refusal::new(
                    "STACK.TEMPLATE",
                    "parameter rotations require a pinned template search",
                ));
            }
            return Stack.lower(l2, name, m);
        };
        let p = if l2.name == name {
            l2.clone()
        } else {
            Program::parse(l2.text(), L2_FILE, Some(name)).map_err(refusal)?
        };
        let t = Target::of(m);
        let p = p.with_vregs(t.vregs, t.vcopies);
        let text = ktpu_l2::lower::lower(&p, &t, search).map_err(refusal)?;
        let mut module = ktpu_l1::text::read(&text, L1_FILE).map_err(refusal)?;
        if self.parameter_rotations {
            crate::templates::configuration::rotations(&mut module);
        }
        Ok(module)
    }

    fn parse_l1(&self, text: &str) -> Result<L1, Refusal> {
        Stack.parse_l1(text)
    }

    fn print_l1(&self, l1: &L1) -> String {
        Stack.print_l1(l1)
    }

    fn schedule(&self, l1: &L1, m: &Machine) -> Result<L1, Refusal> {
        Stack.schedule(l1, m)
    }

    fn programs(
        &self,
        l1: &L1,
        name: &str,
        caller: Option<&[String]>,
        m: &Machine,
    ) -> Result<Programs, Refusal> {
        let mut p = programs(&on_core(l1, m)?, name, m, self.binds.as_ref(), caller)?;
        p.parameter_images = self.parameter_rotations;
        Ok(p)
    }

    fn scratch(&self, p: &Programs) -> Vec<Scratch> {
        Stack.scratch(p)
    }

    fn order(&self, p: &Programs, m: &Machine) -> Result<BTreeMap<u32, PackageOut>, Refusal> {
        order(p, m, self.fetch(m))
    }

    fn check(&self, p: &Programs, m: &Machine) -> Result<Vec<Finding>, Refusal> {
        check(p, m, self.fetch(m))
    }

    fn refuses(&self, f: &Finding) -> bool {
        Stack.refuses(f)
    }
}

/// `fn name.l1`'s node programs: at `binds` when given (absolute), else at
/// the build addresses (`binds_of`) and again shifted by `SHIFT`, so `order`
/// can tell a package whose words hold an address outside its spans.
///
/// With `caller` (and no `binds`) the parameters outside it are scratch:
/// each arena (`ktpu_l1::layout::scratch`) is placed after the caller's
/// parameters as one buffer, its members at their offsets in it.
fn programs(
    l1: &L1,
    name: &str,
    m: &Machine,
    binds: Option<&BTreeMap<String, i128>>,
    caller: Option<&[String]>,
) -> Result<Programs, Refusal> {
    let body = l1.body(name, "l1").map_err(refusal)?;
    let binds = binds.filter(|_| crate::mutant::active() != Some(BINDS_IGNORED));
    let arenas = match (binds, caller) {
        (None, Some(c)) => {
            let c: std::collections::BTreeSet<String> = c.iter().cloned().collect();
            ktpu_l1::layout::scratch::arenas(l1, name, &c, m).map_err(refusal)?
        }
        _ => Vec::new(),
    };
    let members: std::collections::BTreeSet<&str> = arenas
        .iter()
        .flat_map(|a| a.members.iter().map(|(p, _)| p.as_str()))
        .collect();
    let own: Vec<(String, ktpu_l1::Term)> = body
        .params
        .iter()
        .filter(|(p, _)| !members.contains(p.as_str()))
        .cloned()
        .collect();
    // Each parameter its own span, then each arena one span.
    let place = |shift: u64| -> Result<Placed, Refusal> {
        let mut at = Place::new(shift);
        let mut binds = BTreeMap::new();
        for (p, t) in &own {
            let b = ktpu_l1::layout::buffer::Buffer::of(t).map_err(refusal)?;
            binds.insert(
                p.clone(),
                at.next(&b.space, b.bytes(m).map_err(refusal)?) as i128,
            );
        }
        let bases = arenas
            .iter()
            .map(|a| (a.clone(), at.next(&a.space, a.bytes)))
            .collect();
        Ok((binds, bases))
    };
    let build = |b: &BTreeMap<String, i128>, bases: &[(Arena, u64)]| {
        ktpu_l1::build::programs_in(l1, name, b, bases, m, Some(m.nodes()), None).map_err(refusal)
    };
    let (binds, bases, shifted) = match binds {
        Some(b) => (b.clone(), Vec::new(), None),
        None => {
            let (b, bases) = place(0)?;
            let (sb, sbases) = place(SHIFT)?;
            (b, bases, Some(build(&sb, &sbases)?))
        }
    };
    let mut params: BTreeMap<u64, String> = binds
        .iter()
        .map(|(p, at)| (*at as u64, p.clone()))
        .collect();
    params.extend(bases.iter().map(|(a, at)| (*at, a.name())));
    Ok(Programs {
        programs: build(&binds, &bases)?,
        params,
        shifted,
        name: name.to_string(),
        first: std::sync::Mutex::new(Vec::new()),
        parameter_images: false,
        scratch: arenas
            .iter()
            .map(|a| Scratch {
                space: a.space.clone(),
                node: a.node.map(|n| n as i64),
                bytes: a.bytes,
                members: a.members.clone(),
            })
            .collect(),
    })
}

/// Each own parameter's address and each arena's base.
type Placed = (BTreeMap<String, i128>, Vec<(Arena, u64)>);

/// The next span of a space: DRAM buffers from DRAM_AT, staging buffers
/// from STAGE_AT, each at its space's alignment.
struct Place {
    dram: u64,
    stage: u64,
}

impl Place {
    fn new(shift: u64) -> Place {
        Place {
            dram: DRAM_AT + shift,
            stage: STAGE_AT + shift,
        }
    }

    fn next(&mut self, space: &str, bytes: u64) -> u64 {
        let (next, align) = match space {
            "stage" => (&mut self.stage, STAGE_ALIGN),
            _ => (&mut self.dram, DRAM_ALIGN),
        };
        let at = *next;
        *next += bytes.div_ceil(align) * align;
        at
    }
}

/// `l1` on the machine's vector core (`vc.regs`, `vc.rf_copies`): a
/// `target` line that leaves `vregs` / `vcopies` out (L1.MOD.003) takes the
/// machine's; one that names another core is refused (STACK.CORE).
fn on_core(l1: &L1, m: &Machine) -> Result<L1, Refusal> {
    let t = Target::of(m);
    let line = l1
        .source
        .lines()
        .map(str::trim_start)
        .find(|s| s.starts_with("target "))
        .unwrap_or("");
    let named = |key: &str| line.split_whitespace().any(|w| w.starts_with(key));
    let mut out = l1.clone();
    for (key, have, want) in [
        ("vregs=", &mut out.vregs, t.vregs),
        ("vcopies=", &mut out.vcopies, t.vcopies),
    ] {
        match (named(key), *have == want) {
            (_, true) => {}
            (false, false) => *have = want,
            (true, false) => {
                return Err(Refusal::new(
                    "STACK.CORE",
                    format!("the module's target names {key}{have}, the machine's core {want}"),
                ))
            }
        }
    }
    Ok(out)
}

/// The die's fetch port, the memory port a card's units fetch through.
fn fetch_port(m: &Machine) -> ktpu_l0::Coord {
    let c = m.die.fetch_port;
    (c.0, c.1)
}

/// Each mesh's relocatable package, its dispatches in the dispatch
/// scheduler's order.
fn order(
    p: &Programs,
    m: &Machine,
    fetch: Option<ktpu_l0::Coord>,
) -> Result<BTreeMap<u32, PackageOut>, Refusal> {
    let builder = ktpu_l1::build::Builder::new(m).map_err(refusal)?;
    let first = first(p, m, fetch)?;
    if let Some(shifted) = &p.shifted {
        relocatable(&builder, p, &first.run, shifted, m, fetch)?;
    }
    let found = first
        .found
        .as_ref()
        .map_err(|e| Refusal::new("L1.VER.001", format!("{e:?}")))?;
    let mut out = BTreeMap::new();
    for (&mesh, (pkg, bindings)) in &first.run {
        let mut pkg = pkg.clone();
        pkg.steps = ordered_steps(&pkg.steps, &found.packages[&mesh].steps);
        let facts = ktpu_l0::Facts::of_mesh(m, mesh).map_err(l0)?;
        let bytes = pkg.to_bytes(&facts).map_err(l0)?;
        let params = bindings
            .iter()
            .map(|a| {
                p.params
                    .get(a)
                    .cloned()
                    .unwrap_or_else(|| format!("@{a:#x}"))
            })
            .collect();
        let isa = ktpu_isa::unit::vector::VecIsa::new(m)?;
        let images = ktpu_l1::lower::manifest::describe(&pkg, &isa);
        out.insert(
            mesh,
            PackageOut {
                bytes,
                params,
                images,
            },
        );
    }
    Ok(out)
}

/// Scheduling reorders dispatch segments; external fetches retain host address sites.
fn ordered_steps(source: &[ktpu_l0::Step], ordered: &[ktpu_l0::Step]) -> Vec<ktpu_l0::Step> {
    let mut out = ordered.to_vec();
    let fetches = source.iter().filter(|s| s.op == ktpu_l0::Op::Fetch);
    for (step, source) in out
        .iter_mut()
        .filter(|s| s.op == ktpu_l0::Op::Fetch)
        .zip(fetches)
    {
        if source.flags & ktpu_l0::format::F_MEM != 0 {
            step.arg = source.arg;
            step.flags = source.flags;
        }
    }
    out
}

#[cfg(test)]
mod relocation_tests {
    use super::ordered_steps;
    use ktpu_l0::format::F_MEM;
    use ktpu_l0::{Op, Step};

    #[test]
    fn scheduled_fetch_keeps_the_host_site_instead_of_the_probe_address() {
        let source = [
            Step::new(Op::Fetch, 0, 1, 3, F_MEM),
            Step::new(Op::Fetch, 0, 2, 0x1234, 0),
        ];
        let scheduled = [Step::new(Op::Fetch, 0, 1, 0x1000020, 0), source[1]];
        let restored = ordered_steps(&source, &scheduled);
        assert_eq!(restored, source);
        assert_ne!(
            scheduled.as_slice(),
            restored,
            "dropping host-site restoration must fail"
        );
    }
}

/// The first run (built on fresh cores) and its dispatch order, made once
/// for `order` and `check` alike.
struct First {
    run: Run,
    /// The residents after the first run.
    cores: BTreeMap<u32, Cores>,
    found: Result<ktpu_verify::Scheduled, ktpu_verify::ScheduleError>,
}

type Run = BTreeMap<u32, (ktpu_l0::Package, Vec<u64>)>;

/// The first run built with `fetch`, made on the first call with it.
fn first(
    p: &Programs,
    m: &Machine,
    fetch: Option<ktpu_l0::Coord>,
) -> Result<std::sync::Arc<First>, Refusal> {
    let mut made = p.first.lock().unwrap_or_else(|e| e.into_inner());
    if let Some((_, got)) = made.iter().find(|(f, _)| *f == fetch) {
        return got.clone();
    }
    let got = (|| {
        let builder = ktpu_l1::build::Builder::new(m).map_err(refusal)?;
        let mut cores = cores(p, m)?;
        let run = run(&builder, &p.programs, &mut cores, fetch)?;
        let found = scheduled(&bound(&run)?, &target(m)?);
        Ok(std::sync::Arc::new(First { run, cores, found }))
    })();
    made.push((fetch, got.clone()));
    got
}

/// `ktpu_verify::schedule` from empty cores, counted.
fn scheduled(
    bound: &ktpu_verify::Nodes,
    t: &ktpu_verify::Target,
) -> Result<ktpu_verify::Scheduled, ktpu_verify::ScheduleError> {
    count::SIMULATED.with(|c| c.set(c.get() + 1));
    ktpu_verify::schedule(bound, &ktpu_verify::Held::new(), t)
}

/// The checker's findings over the first run (its order the one `order`
/// found) and the reruns with the cores' programs resident.
fn check(
    p: &Programs,
    m: &Machine,
    fetch: Option<ktpu_l0::Coord>,
) -> Result<Vec<Finding>, Refusal> {
    let builder = ktpu_l1::build::Builder::new(m).map_err(refusal)?;
    let t = target(m)?;
    let found = match crate::mutant::active() == Some(CHECK_REBUILDS) {
        true => {
            let mut cores = cores(p, m)?;
            let runs = (0..CHECKED_RUNS)
                .map(|_| bound(&run(&builder, &p.programs, &mut cores, fetch)?))
                .collect::<Result<Vec<_>, Refusal>>()?;
            count::SIMULATED.with(|c| c.set(c.get() + runs.len()));
            ktpu_verify::check(&runs, &t)
        }
        false => {
            let first = first(p, m, fetch)?;
            let mut cores = first.cores.clone();
            let rest = (1..CHECKED_RUNS)
                .map(|_| bound(&run(&builder, &p.programs, &mut cores, fetch)?))
                .collect::<Result<Vec<_>, Refusal>>()?;
            match &first.found {
                Ok(s) => {
                    count::SIMULATED.with(|c| c.set(c.get() + rest.len()));
                    ktpu_verify::check_scheduled(s, &rest, &t)
                }
                Err(e) => e.findings.clone(),
            }
        }
    };
    Ok(found
        .into_iter()
        .map(|f| Finding {
            kind: f.kind.name().to_string(),
            clause: f.clause().to_string(),
            at: f.at.clone(),
            text: f.text.clone(),
        })
        .collect())
}

/// Each node's program of one kernel, and the parameter each bound address
/// stands for.
pub struct Programs {
    parameter_images: bool,
    programs: Vec<ktpu_l1::lower::node::NodeProgram>,
    params: BTreeMap<u64, String>,
    /// The same body built `SHIFT` higher (relocatable builds only).
    shifted: Option<Vec<ktpu_l1::lower::node::NodeProgram>>,
    name: String,
    /// The first run, by the fetch port it was built with.
    first: std::sync::Mutex<Vec<FirstBy>>,
    scratch: Vec<Scratch>,
}

type FirstBy = (
    Option<ktpu_l0::Coord>,
    Result<std::sync::Arc<First>, Refusal>,
);

/// The mutant under which a build given `binds` ignores them (relocatable).
pub const BINDS_IGNORED: &str = "ktpu.stack/binds_ignored";

/// The mutant under which `check` builds and orders the first run again
/// instead of taking `order`'s (`count` tells).
pub const CHECK_REBUILDS: &str = "ktpu.stack/check_rebuilds";

/// Per-thread counts of the work a compile does: the runs built
/// (`ktpu_l1` builder passes over every node) and the runs the dispatch
/// scheduler simulates.
pub mod count {
    use std::cell::Cell;

    thread_local! {
        pub(super) static BUILT: Cell<usize> = const { Cell::new(0) };
        pub(super) static SIMULATED: Cell<usize> = const { Cell::new(0) };
    }

    /// `(built, simulated)` on this thread since the last [`reset`].
    pub fn get() -> (usize, usize) {
        (BUILT.with(Cell::get), SIMULATED.with(Cell::get))
    }

    pub fn reset() {
        BUILT.with(|c| c.set(0));
        SIMULATED.with(|c| c.set(0));
    }
}

/// The runs the checker takes: the first and the rerun with the cores'
/// programs resident (the Python checker's two).
const CHECKED_RUNS: usize = 2;

/// Where parameters are bound while building: each its own span, DRAM
/// buffers from DRAM_AT and staging buffers from STAGE_AT at their
/// alignments. The packages are relocatable, so the addresses never reach
/// their bytes; distinct spans keep the dispatch order free of overlaps.
const DRAM_AT: u64 = 0x0100_0000;
const DRAM_ALIGN: u64 = 4096;
const STAGE_AT: u64 = 0x1000;
const STAGE_ALIGN: u64 = 256;
/// The second build's offset (`programs`): one staging alignment, so a
/// shifted staging span stays inside the store.
const SHIFT: u64 = STAGE_ALIGN;

fn l0(e: ktpu_l0::Refusal) -> Refusal {
    Refusal::new(e.clause, e.to_string())
}

fn target(m: &Machine) -> Result<ktpu_verify::Target, Refusal> {
    ktpu_verify::Target::from_machine(m, None).map_err(|e| Refusal::new("MACHINE", e))
}

/// Fresh residents for each mesh the programs run on.
fn cores(p: &Programs, m: &Machine) -> Result<BTreeMap<u32, Cores>, Refusal> {
    p.programs
        .iter()
        .map(|np| {
            let mut cores = Cores::new(m).map_err(refusal)?;
            cores.parameter_images = p.parameter_images;
            Ok((np.mesh, cores))
        })
        .collect()
}

/// One run: each mesh's relocatable package (unlowered, no buffer default)
/// and its bindings, built against the residents `cores` (updated).
fn run(
    builder: &ktpu_l1::build::Builder<'_>,
    programs: &[ktpu_l1::lower::node::NodeProgram],
    cores: &mut BTreeMap<u32, Cores>,
    fetch: Option<ktpu_l0::Coord>,
) -> Result<Run, Refusal> {
    count::BUILT.with(|c| c.set(c.get() + 1));
    let mut out = BTreeMap::new();
    for np in programs {
        let opts = ktpu_l1::build::Opts {
            fetch,
            cores: cores.get_mut(&np.mesh),
            relocate: true,
        };
        let b = builder.build(np, opts).map_err(refusal)?;
        out.insert(np.mesh, (b.build(false, false), b.bindings()));
    }
    Ok(out)
}

/// Refuses (STACK.RELOC) a build whose packages are not relocatable: bound
/// at the shifted build's addresses, each must equal the shifted build's
/// own, else a word holds an address outside the program's spans (the
/// compile then says so in `Compiled::bound`, and the card's packages are
/// built at each binding, `CardStack::binds`).
fn relocatable(
    builder: &ktpu_l1::build::Builder<'_>,
    p: &Programs,
    run_: &BTreeMap<u32, (ktpu_l0::Package, Vec<u64>)>,
    shifted: &[ktpu_l1::lower::node::NodeProgram],
    m: &Machine,
    fetch: Option<ktpu_l0::Coord>,
) -> Result<(), Refusal> {
    let mut cores = cores(p, m)?;
    let other = run(builder, shifted, &mut cores, fetch)?;
    for (mesh, (pkg, _)) in run_ {
        let (want, at) = &other[mesh];
        let (got, want) = (pkg.bind(at).map_err(l0)?, want.bind(at).map_err(l0)?);
        if got != want {
            return Err(Refusal::new(
                "STACK.RELOC",
                format!(
                    "fn {}.l1 on mesh {mesh}: a word holds an address outside the \
                     program's spans, so its package is not relocatable; build it \
                     at its bindings ({})",
                    p.name,
                    first_difference(pkg, &got, &want)
                ),
            ));
        }
    }
    Ok(())
}

/// Where two bound builds of one package first differ: the step or payload,
/// both values, and the relocations the unbound package (`pkg`) has on it.
fn first_difference(
    pkg: &ktpu_l0::Package,
    got: &ktpu_l0::Package,
    want: &ktpu_l0::Package,
) -> String {
    if got.steps != want.steps {
        let i = (0..got.steps.len().max(want.steps.len()))
            .find(|&i| got.steps.get(i) != want.steps.get(i))
            .unwrap_or(0);
        return format!(
            "step {i}: {:?} vs {:?}",
            got.steps.get(i),
            want.steps.get(i)
        );
    }
    if got.payloads.len() != want.payloads.len() {
        return format!("{} payloads vs {}", got.payloads.len(), want.payloads.len());
    }
    let Some(i) = (0..got.payloads.len()).find(|&i| got.payloads[i] != want.payloads[i]) else {
        return "units or header".into();
    };
    let relocs: Vec<_> = pkg
        .relocs
        .iter()
        .filter(|r| r.payload == i as u64)
        .collect();
    let step = got
        .steps
        .iter()
        .position(|s| (s.arg..s.arg + s.count.max(1)).contains(&(i as u64)));
    format!(
        "payload {i} (step {step:?}): {:x?} vs {:x?}; relocations on it {relocs:?}",
        got.payloads[i].0, want.payloads[i].0
    )
}

#[cfg(test)]
mod line_tests {
    use super::*;

    #[test]
    fn independent_build_spans_own_whole_machine_lines() {
        for name in ktpu_machine::names(&ktpu_machine::machines_dir()) {
            let m = Machine::named(&name).unwrap();
            let line = u64::from(m.card.dram.line_bytes);
            assert_eq!(DRAM_ALIGN % line, 0);
            assert_eq!(STAGE_ALIGN % line, 0);
            let mut place = Place::new(0);
            for space in ["dram", "stage"] {
                let mut end = 0;
                for bytes in [1, 32, 33, 64, 1568] {
                    let at = place.next(space, bytes);
                    assert_eq!(at % line, 0);
                    assert!(at >= end);
                    end = (at + bytes).div_ceil(line) * line;
                }
            }
        }
    }
}

fn bound(run: &BTreeMap<u32, (ktpu_l0::Package, Vec<u64>)>) -> Result<ktpu_verify::Nodes, Refusal> {
    run.iter()
        .map(|(mesh, (pkg, b))| Ok((*mesh, pkg.bind(b).map_err(l0)?)))
        .collect()
}
