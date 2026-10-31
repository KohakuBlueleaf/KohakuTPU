//! `fn NAME.l1` bodies to node programs (L1.BODY.*): one per `node N` block.

use super::*;

/// The body's statements per node after read-time expansion: `{None: ..}`
/// for a body with no `node` block (L1.BODY.001, .003, .005, .007).
pub fn bodies(
    module: &Module,
    name: &str,
    binds: &BTreeMap<String, i128>,
) -> Result<Vec<(Option<i128>, Vec<Stmt>)>> {
    let body = module.body(name, "l1")?;
    let r = module.reader();
    let missing: Vec<&String> = body
        .params
        .iter()
        .map(|(p, _)| p)
        .filter(|p| !binds.contains_key(*p))
        .collect();
    if !missing.is_empty() {
        return Err(Error::new(
            "L1.BODY.005",
            format!("{name}.l1: unbound parameters {missing:?}"),
        ));
    }
    let macros = module.macro_table();
    let env: Env = binds
        .iter()
        .filter(|_| !on("l1.body/unbound"))
        .map(|(k, v)| (k.clone(), Val::Int(*v)))
        .collect();
    let stmts = Expander::new(&macros, &r, true).stmts(&body.stmts, &env)?;
    if !stmts.iter().any(|s| s.op == "node") {
        return Ok(vec![(None, stmts)]);
    }
    if let Some(s) = stmts.iter().find(|s| s.op != "node") {
        return Err(r.fail(
            s,
            "L1.BODY.007",
            "with `node` blocks, every statement is in one",
        ));
    }
    let mut out: Vec<(Option<i128>, Vec<Stmt>)> = Vec::new();
    for s in &stmts {
        let pos = s.positional();
        let n = match pos.as_slice() {
            [Term::Int(v, _)] => *v,
            _ => return Err(r.fail(s, "L1.BODY.003", "node N (a block)")),
        };
        if out.iter().any(|(k, _)| *k == Some(n)) {
            return Err(r.fail(s, "L1.BODY.007", format!("node {n} twice")));
        }
        out.push((Some(n), s.body.clone()));
    }
    Ok(out)
}

/// One node's program and the mesh it runs on.
#[derive(Clone, Debug)]
pub struct NodeProgram {
    pub node: Option<i128>,
    pub mesh: u32,
    pub program: Program,
}

/// `{node: Program}` of `fn name.l1`, each parameter bound to an address: one
/// per `node N` block on `target` seen from mesh N; `None` for a body with
/// no node block.
pub fn programs(
    module: &Module,
    name: &str,
    binds: &BTreeMap<String, i128>,
    target: &Target<'_>,
    entry: State,
) -> Result<Vec<NodeProgram>> {
    let mut spans: BTreeMap<u64, u64> = BTreeMap::new();
    for (param, t) in &module.body(name, "l1")?.params {
        let Some(&at) = binds.get(param) else {
            continue;
        };
        let size = Buffer::of(t)?.bytes(target.machine)?;
        let e = spans.entry(at as u64).or_insert(0);
        *e = (*e).max(size);
    }
    programs_spanned(module, name, binds, spans, target, entry)
}

/// [`programs`] with the relocation spans given (`{base: size}`): an address
/// field inside one is relocated against it.
pub fn programs_spanned(
    module: &Module,
    name: &str,
    binds: &BTreeMap<String, i128>,
    spans: BTreeMap<u64, u64>,
    target: &Target<'_>,
    entry: State,
) -> Result<Vec<NodeProgram>> {
    let got = bodies(module, name, binds)?;
    let isa = Isa::new(target.machine)?;
    let mut out = Vec::new();
    for (node, stmts) in got {
        let t = match node {
            None => target.clone(),
            Some(n) => target.for_mesh(n as u32)?,
        };
        let units = MeshUnits {
            target: t.clone(),
            reader: module.reader(),
        };
        let mut reader = NodeReader::new(module, &t, &units, &isa, entry, spans.clone())?;
        reader.stmts(&stmts)?;
        reader.finish()?;
        out.push(NodeProgram {
            node,
            mesh: t.mesh,
            program: reader.p,
        });
    }
    Ok(out)
}
