//! `isa.cluster.mm` (ISA.MG.081): the contraction kernel ktpu-kernels
//! generates (`mm_kernel`), compiled through the ktpu pipeline, which
//! ktpu-kernels cannot depend on: the `send`s each cluster gets.

use ktpu::{Backend, Stack};
use ktpu_machine::Machine;
use serde_json::{Map, Value};

const MM: &str = "isa.cluster.mm";

/// ktpu-kernels' ported runners and the hosted one.
pub fn kernels_runners() -> Vec<&'static str> {
    let mut out = ktpu_kernels::spec::runners();
    out.push(MM);
    out
}

/// ktpu-kernels' suite `run`, with `isa.cluster.mm` run here under a
/// ktpu-kernels mutant.
pub fn kernels_run(
    runner: &str,
    input: &Value,
    machine: Option<&Machine>,
    mutant: Option<&str>,
) -> Result<Value, String> {
    if runner != MM {
        return ktpu_kernels::spec::run(runner, input, machine, mutant);
    }
    let go = || cluster_mm(input, machine);
    let Some(name) = mutant else {
        return go();
    };
    let name = ktpu_kernels::mutants::NAMES
        .iter()
        .copied()
        .find(|n| *n == name)
        .ok_or_else(|| format!("unknown mutant {name}"))?;
    ktpu_kernels::mutants::with(name, go)
}

/// `mm_kernel` for `plan` (bt, M, N, K; B shared, rows in fours, as the
/// Python runner's `plan` defaults), out [bt * M, N], compiled: the count of
/// `send mg[i]` lines in its L1 by cluster.
fn cluster_mm(inp: &Value, m: Option<&Machine>) -> Result<Value, String> {
    let m = m.ok_or("the runner needs a machine")?;
    let p: Vec<i64> = inp["plan"]
        .as_array()
        .ok_or("no plan")?
        .iter()
        .map(|x| x.as_i64().ok_or("a plan entry"))
        .collect::<Result<_, _>>()?;
    let [bt, rows, n, k] = p[..] else {
        return Err(format!("a plan of 4, not {}", p.len()));
    };
    let mm = ktpu_kernels::gen::matmul::plan(bt, rows, n, k, true, 4, ktpu_machine::PAIRING, m)
        .map_err(|e| e.to_string())?;
    let out = ktpu_kernels::Operand::full(mm.n, mm.bt * mm.m);
    let kernel = ktpu_kernels::mm_kernel(&mm, &ktpu_kernels::expr::load(0), &[], &out, m)
        .map_err(|e| e.to_string())?;
    let graph = serde_json::to_string(&kernel.graph).map_err(|e| e.to_string())?;
    let b = Stack;
    let l2 = b.graph_to_l2(&graph, m).map_err(|e| e.to_string())?;
    let name = l2.name.clone();
    let l1 = b
        .passes(&l2, &name, m)
        .and_then(|p| b.lower(&p, &name, m))
        .and_then(|l| b.schedule(&l, m))
        .map_err(|e| e.to_string())?;
    let re = regex::Regex::new(r"(?m)^\s*send (mg\[\d+\])").unwrap();
    let mut sends = Map::new();
    for c in re.captures_iter(&b.print_l1(&l1)) {
        let n = sends.get(&c[1]).and_then(Value::as_i64).unwrap_or(0);
        sends.insert(c[1].to_string(), Value::from(n + 1));
    }
    Ok(Value::Object(sends))
}
