//! Allocation and rewriting of memory drains with page-crossing bursts.

use super::*;
use crate::text::Definition;

/// An exact copy, ordered after its producing drain.
#[derive(Clone, Debug)]
pub struct DrainCopy {
    pub source: String,
    pub destination: Term,
    pub bytes: u64,
    pub destination_offset: u64,
    pub dynamic_address: bool,
    pub count: Option<Term>,
    pub remote_mesh: Option<u32>,
}

/// An expanded body and the storage and binding contracts of its drains.
pub struct DrainPlan {
    pub module: Module,
    name: String,
    pub scratch: BTreeSet<String>,
    pub burst_ports: BTreeSet<String>,
    pub copies: Vec<DrainCopy>,
    pub vector_available: bool,
}

fn kw(name: &str, value: Term) -> Term {
    Term::Assign(Box::new(Term::name(name)), Box::new(value))
}

/// A buffer-relative address; the expander folds constant displacements.
fn relative(t: &Term, params: &BTreeSet<String>) -> Option<(String, u64)> {
    match t {
        Term::Name(n) if params.contains(n) => Some((n.clone(), 0)),
        Term::Offset(base, off) => {
            let (name, at) = relative(base, params)?;
            let off = u64::try_from(off.as_int()?).ok()?;
            Some((name, at.checked_add(off)?))
        }
        _ => None,
    }
}

fn destination(st: &Stmt) -> Option<Term> {
    if st.op == "gemm" {
        st.kw("emit").cloned()
    } else if st.op == "drain" && st.kw("to").is_none() {
        st.positional()
            .into_iter()
            .find(|t| t.as_name() != Some("fused") && t.as_name() != Some("signal"))
            .cloned()
    } else {
        None
    }
}

fn redirect(st: &mut Stmt, address: &Term) {
    if st.op == "gemm" {
        for arg in &mut st.args {
            if arg.as_kwarg().is_some_and(|(k, _)| k == "emit") {
                *arg = kw("emit", address.clone());
            }
        }
    } else if let Some(old) = destination(st) {
        if let Some(arg) = st.args.iter_mut().find(|arg| **arg == old) {
            *arg = address.clone();
        }
    }
}

/// Expand before allocating: memory drain decisions are shared by standalone
/// and fused sends, including hand-written L1 and macro-expanded producers.
/// `binds` supplies static addresses; None preserves buffer-relative addresses
/// and proves them under the returned per-port burst alignment contract.
pub fn plan_drains(
    module: &Module,
    name: &str,
    machine: &ktpu_machine::Machine,
    binds: Option<&BTreeMap<String, i128>>,
) -> Result<DrainPlan> {
    let mut plan = DrainPlan {
        module: module.clone(),
        name: name.into(),
        scratch: BTreeSet::new(),
        burst_ports: BTreeSet::new(),
        copies: Vec::new(),
        vector_available: true,
    };
    if !machine.flaws.f016.present {
        return Ok(plan);
    }
    let body = module.body(name, "l1")?;
    let params: BTreeSet<String> = body.params.iter().map(|(n, _)| n.clone()).collect();
    let env: Env = params
        .iter()
        .map(|n| (n.clone(), Val::Term(Term::name(n))))
        .collect();
    let macros = module.macro_table();
    let expanded = Expander::new(&macros, &module.reader(), true).stmts(&body.stmts, &env)?;
    let mut out = body.clone();
    out.stmts = if expanded.iter().any(|s| s.op == "node") {
        let mut nodes = Vec::new();
        for st in expanded {
            let node = st.positional().first().and_then(|t| t.as_int());
            nodes.push(st.with_body(plan_node(
                &mut plan, module, &st.body, &params, machine, binds, node,
            )?));
        }
        nodes
    } else {
        plan_node(&mut plan, module, &expanded, &params, machine, binds, None)?
    };
    for copy in &plan.copies {
        let saved = if copy.count.is_some() || copy.dynamic_address {
            32
        } else {
            0
        };
        // A tile layout asks the arena allocator for cluster-burst alignment.
        let ty = Term::Placed(
            Box::new(Term::View(
                "u8".into(),
                vec![Term::int(1), Term::int(i128::from(copy.bytes + saved))],
            )),
            Box::new(Term::Call(
                "dram".into(),
                vec![Term::Kw("tile".into(), Box::new(Term::Dims(vec![1, 1])))],
            )),
        );
        out.params.push((copy.source.clone(), ty));
    }
    plan.module.fns.insert((name.into(), "l1".into()), out);
    Ok(plan)
}

fn plan_node(
    plan: &mut DrainPlan,
    module: &Module,
    stmts: &[Stmt],
    params: &BTreeSet<String>,
    machine: &ktpu_machine::Machine,
    binds: Option<&BTreeMap<String, i128>>,
    node: Option<i128>,
) -> Result<Vec<Stmt>> {
    let target = Target::new(machine, Some(machine.nodes()), node.unwrap_or(0) as u32);
    let units = MeshUnits {
        target: target.clone(),
        reader: module.reader(),
    };
    let isa = Isa::new(machine)?;
    let mut reader = NodeReader::new(
        module,
        &target,
        &units,
        &isa,
        image::UNKNOWN,
        BTreeMap::new(),
    )?;
    let numeric: Env = params
        .iter()
        .enumerate()
        .map(|(i, p)| {
            let at = binds
                .and_then(|b| b.get(p))
                .copied()
                .unwrap_or(0x0100_0000 + (i as i128) * 0x0100_0000);
            (p.clone(), Val::Int(at))
        })
        .collect();
    let macros = module.macro_table();
    let concrete = Expander::new(&macros, &module.reader(), true).stmts(stmts, &numeric)?;
    for send in &concrete {
        if matches!(send.op.as_str(), "send" | "fetch" | "push") {
            if let Some(t) = send.positional().first() {
                let c = reader.unit(send, t)?;
                if target.units(UnitKind::Vc).contains(&c) {
                    plan.vector_available = false;
                }
            }
        }
    }
    reader.stmts(&concrete)?;
    reader.finish()?;
    let word = u64::from(machine.die.word_bytes);
    let burst = u64::from(machine.mg.wburst) * word;
    if burst == 0 {
        return Err(Error::new("MACHINE", "cluster write burst is zero"));
    }
    let labels: BTreeSet<&str> = stmts
        .iter()
        .filter(|s| s.op == "label")
        .filter_map(|s| s.args.first().and_then(Term::as_name))
        .collect();
    let mut expanded = stmts.to_vec();
    let mut pending: BTreeMap<Xy, Vec<(usize, usize)>> = BTreeMap::new();
    let mut after: BTreeMap<(usize, usize), usize> = BTreeMap::new();
    for si in 0..expanded.len() {
        if expanded[si].op != "send" {
            continue;
        }
        let unit = reader.unit(&expanded[si], reader.one(&expanded[si])?)?;
        if !target.units(UnitKind::Mg).contains(&unit) {
            continue;
        }
        for oi in 0..expanded[si].body.len() {
            let st = expanded[si].body[oi].clone();
            if st.op == "gemm"
                && (st.kw("emit").is_some()
                    || st.positional().iter().any(|t| t.as_name() == Some("emit")))
            {
                pending.entry(unit).or_default().push((si, oi));
            }
            if st.op != "drain" {
                continue;
            }
            let fused = st.positional().iter().any(|t| t.as_name() == Some("fused"));
            let emit = if fused {
                let queue = pending.entry(unit).or_default();
                Some(queue.remove(0))
            } else {
                None
            };
            let Some(dst) = destination(&st) else {
                continue;
            };
            let count = st.kw("n").filter(|t| t.as_int().is_none()).cloned();
            let n = st
                .kw("n")
                .and_then(Term::as_int)
                .unwrap_or((1i128 << ktpu_isa::unit::cluster::N_BITS) - 1);
            let bytes = u64::try_from(n)
                .ok()
                .and_then(|n| n.checked_mul(word))
                .ok_or_else(|| reader.fail(&st, "L1.MG.009", "drain extent is not nonnegative"))?;
            let base = relative(&dst, params);
            let address = match &base {
                Some((p, off)) => binds
                    .and_then(|b| b.get(p))
                    .and_then(|v| u64::try_from(*v).ok())
                    .and_then(|v| v.checked_add(*off)),
                None => dst.as_int().and_then(|v| u64::try_from(v).ok()),
            };
            let crossing = if let Some(address) = address {
                static_burst_proof(address, bytes, burst)? == BurstProof::Crossing
            } else if let Some((_, off)) = &base {
                // A burst-aligned base may occupy any burst in a page.
                bytes > burst - off % burst && off % burst != 0
            } else {
                true
            };
            if !crossing {
                if address.is_none() {
                    if let Some((p, _)) = base {
                        plan.burst_ports.insert(p);
                    }
                }
                continue;
            }
            let mut serial = 0;
            let scratch = loop {
                let candidate = format!("__drain_{}_{}_{}_{serial}", node.unwrap_or(0), si, oi);
                if !params.contains(&candidate)
                    && !plan.scratch.contains(&candidate)
                    && !labels.contains(format!("{candidate}_copy").as_str())
                    && !labels.contains(format!("{candidate}_copy_done").as_str())
                {
                    break candidate;
                }
                serial += 1;
            };
            let index = plan.copies.len();
            plan.scratch.insert(scratch.clone());
            plan.burst_ports.insert(scratch.clone());
            plan.copies.push(DrainCopy {
                source: scratch.clone(),
                destination: dst,
                bytes,
                destination_offset: address
                    .unwrap_or_else(|| base.as_ref().map_or(word, |(_, off)| *off)),
                dynamic_address: address.is_none() && base.is_none(),
                count,
                remote_mesh: st.kw("mesh").and_then(Term::as_int).map(|m| m as u32),
            });
            redirect(&mut expanded[si].body[oi], &Term::name(&scratch));
            expanded[si].body[oi]
                .args
                .retain(|a| !a.as_kwarg().is_some_and(|(k, _)| k == "mesh"));
            if let Some((s, o)) = emit {
                redirect(&mut expanded[s].body[o], &Term::name(&scratch));
            }
            after.insert((si, oi), index);
        }
    }
    let mut out = Vec::new();
    for (si, st) in expanded.into_iter().enumerate() {
        if !after.keys().any(|(s, _)| *s == si) {
            out.push(st);
            continue;
        }
        let mut batch = Vec::new();
        for (oi, op) in st.body.iter().enumerate() {
            batch.push(op.clone());
            if let Some(&index) = after.get(&(si, oi)) {
                out.push(st.with_body(std::mem::take(&mut batch)));
                out.push(Stmt::new(
                    "wait",
                    st.positional().into_iter().cloned().collect(),
                ));
                out.push(Stmt::new("drain_copy", vec![Term::int(index as i128)]));
            }
        }
        if !batch.is_empty() {
            out.push(st.with_body(batch));
        }
    }
    Ok(out)
}

impl DrainPlan {
    /// Install copies lowered by the ordinary memory-copy owner. Image names
    /// are scoped by the caller; waits precede the following original statement.
    pub fn install_copies(
        &mut self,
        copies: &[Vec<Stmt>],
        images: impl IntoIterator<Item = (String, Definition)>,
    ) {
        fn replace(stmts: &mut Vec<Stmt>, copies: &[Vec<Stmt>]) {
            let mut out = Vec::new();
            for mut st in std::mem::take(stmts) {
                if st.op == "drain_copy" {
                    if let Some(i) = st.args.first().and_then(Term::as_int) {
                        out.extend(copies[i as usize].clone());
                    }
                } else {
                    replace(&mut st.body, copies);
                    out.push(st);
                }
            }
            *stmts = out;
        }
        for body in self.module.fns.values_mut() {
            if body.name == self.name && body.level == "l1" {
                replace(&mut body.stmts, copies);
            }
        }
        self.module.images.extend(images);
    }
}

impl<'a> NodeReader<'a> {
    /// A register-counted extent uses one-word mover commands: each command
    /// starts from idle, independently of the destination's page and line.
    pub(super) fn drain_copy_dynamic(&mut self, st: &Stmt) -> Result<()> {
        let [source, destination, count, extent, label, mesh] = st.args.as_slice() else {
            return Err(self.fail(
                st,
                "L1.MOV.009",
                "drain copy source, destination, count, extent, label, mesh",
            ));
        };
        let source = self.const_(st, source)? as u64;
        let extent = self.const_(st, extent)? as u64;
        let (mut dc, dr) = self.val(st, destination)?;
        let mesh = self.const_(st, mesh)?;
        let (nc, nr) = self.val(st, count)?;
        let label = self.name(st, label)?.to_string();
        let done = format!("{label}_done");
        let saved = source + extent;
        let start = self.p.steps.len();
        self.p
            .store((0..3).map(|r| (saved + 8 * r, r)).collect(), true);
        if let Some(r) = nr {
            if r < 3 {
                self.p.load(0, saved + 8 * r, None, false);
            } else {
                self.p.alu(0, Alu::Mov, 0, r, false);
            }
            self.p.alu(0, Alu::Add, 0, nc as u64, true);
        } else {
            self.p.alu(0, Alu::Mov, 0, nc as u64, true);
        }
        self.p.alu(
            0,
            Alu::And,
            0,
            (1 << ktpu_isa::unit::cluster::N_BITS) - 1,
            true,
        );
        if let Some(r) = dr {
            if r < 3 {
                self.p.load(2, saved + 8 * r, None, false);
            } else {
                self.p.alu(2, Alu::Mov, 0, r, false);
            }
        } else {
            self.p.alu(2, Alu::Mov, 0, 0, true);
        }
        if mesh >= 0 {
            let address = self.target.machine.die.address.runtime();
            let mask = ((1u64 << address.mesh_bits) - 1) << address.mesh_shift;
            self.p.alu(2, Alu::Add, 2, dc as u64, true);
            self.p.alu(2, Alu::And, 2, !mask, true);
            self.p
                .alu(2, Alu::Or, 2, (mesh as u64) << address.mesh_shift, true);
            dc = 0;
        }
        self.p.alu(1, Alu::Mov, 0, 0, true);
        self.p.label(&label).map_err(ref_err)?;
        self.p.jump(&done, Cond::Eq, 0, 0, true);
        let word = i64::from(self.target.machine.die.word_bytes);
        let mv = Move {
            mode: MoveMode::Copy,
            src: Some(Walk::new(source as i64, &[(1, word)])),
            dst: Some(Walk::new(dc as i64, &[(1, word)])),
            ewidth: W32,
            flags: self
                .isa
                .mover
                .flag_byte(true, 0)
                .map_err(|e| self.at(st, e))?,
            xform: (0, 0),
            imm: None,
            seed: None,
            index: None,
            row: None,
            fp: false,
        };
        let fix = vec![
            ("src.base".into(), 1, source as i128),
            ("dst.base".into(), 2, dc),
        ];
        let (writes, patches) = self.checked(st, mv, &fix)?;
        self.p.move_(writes, patches);
        if mesh < 0 {
            self.local_move();
        }
        self.p.alu(1, Alu::Add, 1, word as u64, true);
        self.p.alu(2, Alu::Add, 2, word as u64, true);
        self.p.alu(0, Alu::Sub, 0, 1, true);
        self.p.jump(&label, Cond::Always, 0, 0, true);
        self.p.label(&done).map_err(ref_err)?;
        for r in 0..3 {
            self.p.load(r, saved + 8 * r, None, false);
        }
        let address = self.target.machine.die.address.runtime();
        let route = ktpu_l0::build::LocalRoute {
            shift: address.mesh_shift,
            bits: address.mesh_bits,
            special: address.special_bit,
            mesh: self.target.mesh,
        };
        for step in &mut self.p.steps[start..] {
            if let PStmt::Store { local, .. } | PStmt::Load { local, .. } = step {
                *local = Some(route);
            }
        }
        Ok(())
    }
}
