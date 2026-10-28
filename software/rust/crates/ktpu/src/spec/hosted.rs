//! Runner forms that need the dispatch scheduler (ktpu-verify), which the
//! stage crates cannot depend on: `l0.program` `through: packages`,
//! `l1.package` `all` and `l1.order`. The extension's `spec_run` and the
//! ktpu-spec suites both run these; the caller sets the mutant scope.

use ktpu_l0::spec::program::through_packages;
use ktpu_l0::{BuildOpts, Facts, IsaAddresses, Package, Program, Step};
use ktpu_machine::Machine;
use serde_json::Value;
use std::collections::BTreeMap;

/// An `l0.*` runner: `l0.program` with `through` scheduled here, the rest
/// ktpu-l0's own.
pub fn l0(runner: &str, input: &Value, machine: Option<&Machine>) -> Result<Value, String> {
    if runner == "l0.named" {
        return ktpu_l0::spec::named::run(input, machine);
    }
    if runner != "l0.program" || input.get("through").is_none() {
        return ktpu_l0::spec::run(runner, input, machine, None);
    }
    let m = machine.ok_or("l0.program needs a machine")?;
    let schedule = |p: &Program, f: &Facts, fetch, spans: &BTreeMap<u64, u64>| {
        schedule_one(m, p, f, fetch, spans)
    };
    through_packages(input, machine, &schedule)
}

/// An `l1.*` runner: `l1.order` and `l1.package` `all` scheduled here, the
/// rest ktpu-l1's own.
pub fn l1(runner: &str, input: &Value, machine: Option<&Machine>) -> Result<Value, String> {
    if runner == "l1.order" {
        return ktpu_l1::spec::order_all(input, machine, None, &order);
    }
    if runner == "l1.package" && input["all"] == Value::Bool(true) {
        return ktpu_l1::spec::package_all(input, machine, None, &schedule_all);
    }
    ktpu_l1::spec::run(runner, input, machine, None)
}

/// Python's `verify.schedule` for one node: the program built against
/// fresh residents with its spans relocated, its dispatches in the order the
/// abstract machine finds, and its bindings.
fn schedule_one(
    m: &Machine,
    p: &Program,
    f: &Facts,
    fetch: Option<ktpu_l0::Coord>,
    spans: &BTreeMap<u64, u64>,
) -> Result<(Package, Vec<u64>), String> {
    let isa = IsaAddresses::new(m).map_err(|e| e.to_string())?;
    let mut cores = ktpu_l1::lower::imem::Cores::new(m).map_err(|e| e.to_string())?;
    let b = p
        .build(
            f,
            BuildOpts {
                fetch,
                resident: Some(&mut cores),
                addresses: Some(&isa),
                spans: Some(spans),
            },
        )
        .map_err(|e| e.to_string())?;
    let mut pkg = b.build(false, false);
    let bindings = b.bindings();
    let bound = pkg.bind(&bindings).map_err(|e| e.to_string())?;
    let t = ktpu_verify::Target::from_machine(m, None)?;
    let found = ktpu_verify::schedule(&BTreeMap::from([(0, bound)]), &ktpu_verify::Held::new(), &t)
        .map_err(|e| format!("{e:?}"))?;
    pkg.steps = found.packages[&0].steps.clone();
    Ok((pkg, bindings))
}

/// `verify.schedule` over every node's bound package, its cores holding
/// `held`: each node's steps in the order the abstract machine finds.
fn order(
    pkgs: &BTreeMap<u32, Package>,
    held: &ktpu_l1::spec::Held,
    m: &Machine,
) -> Result<BTreeMap<u32, Vec<Step>>, String> {
    let t = ktpu_verify::Target::from_machine(m, None)?;
    let held: ktpu_verify::Held = held
        .iter()
        .map(|(mesh, cores)| {
            let cores = cores.iter().map(|(c, imem, desc)| ktpu_verify::HeldCore {
                coord: (c.0 as u8, c.1 as u8),
                imem: imem.clone(),
                desc: desc.clone(),
                scalars: BTreeMap::new(),
            });
            (*mesh, cores.collect())
        })
        .collect();
    let found = ktpu_verify::schedule(pkgs, &held, &t).map_err(|e| format!("{e:?}"))?;
    Ok(found
        .packages
        .into_iter()
        .map(|(mesh, p)| (mesh, p.steps))
        .collect())
}

/// `verify.schedule` over every node's bound package: each node's steps in
/// the order the abstract machine finds.
fn schedule_all(
    pkgs: &BTreeMap<u32, Package>,
    m: &Machine,
) -> Result<BTreeMap<u32, Vec<Step>>, String> {
    let t = ktpu_verify::Target::from_machine(m, None)?;
    let found =
        ktpu_verify::schedule(pkgs, &ktpu_verify::Held::new(), &t).map_err(|e| format!("{e:?}"))?;
    Ok(found
        .packages
        .into_iter()
        .map(|(mesh, p)| (mesh, p.steps))
        .collect())
}
