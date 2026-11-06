//! Exact memory copies for the L1 memory-drain plan.

use super::*;
use ktpu_l1::lower::node::DrainPlan;
use ktpu_l1::text::{Definition, Env, Expander, Stmt, Term, Val};

pub(super) fn copies(plan: &mut DrainPlan, m: &Machine) -> Result<(), Refusal> {
    let mut code = Vec::new();
    let mut images: Vec<(String, Definition)> = Vec::new();
    for (i, copy) in plan.copies.iter().enumerate() {
        let count = copy.count.clone().or_else(|| {
            (copy.dynamic_address
                && (!plan.vector_available
                    || m.die.units.vc.is_empty()
                    || copy.remote_mesh.is_some()))
            .then(|| Term::int(i128::from(copy.bytes / u64::from(m.die.word_bytes))))
        });
        if let Some(count) = count {
            code.push(vec![Stmt::new(
                "drain_copy_dynamic",
                vec![
                    Term::name(&copy.source),
                    copy.destination.clone(),
                    count,
                    Term::int(i128::from(copy.bytes)),
                    Term::name(format!("{}_copy", copy.source)),
                    Term::int(copy.remote_mesh.map_or(-1, i128::from)),
                ],
            )]);
            continue;
        }
        let mut serial = i;
        let name = loop {
            let name = format!("__drain_copy_{serial}");
            let used = plan.module.images.keys().chain(plan.module.macros.keys());
            if !used.into_iter().any(|n| n.starts_with(&name))
                && !images.iter().any(|(n, _)| n.starts_with(&name))
            {
                break name;
            }
            serial += 1;
        };
        let off = copy.destination_offset;
        let n = copy.bytes;
        let header = if plan.module.target.is_some() {
            plan.module.target_line()
        } else {
            String::new()
        };
        let text = format!(
            "{header}\nfn {name}.l2(src: u8[{n}] @dram, dst: u8[{}] @dram)\n    copy dst[{off}:{}] <- src\n",
            off + n,
            off + n,
        );
        let p = Program::parse(&text, "<drain-copy>", Some(&name)).map_err(refusal)?;
        let mut target = Target::of(m);
        // A copy may use vector storage only when the surrounding L1 body
        // does not own it across sends.
        if !plan.vector_available {
            target.vc.clear();
        }
        let text = ktpu_l2::lower::lower(&p, &target, &Search::default()).map_err(refusal)?;
        let module = ktpu_l1::text::read(&text, "<drain-copy>").map_err(refusal)?;
        let module = ktpu_l1::sched::schedule_module(&module, m).map_err(refusal)?;
        // The copy selector sees the destination's actual line displacement;
        // substituting this base restores its symbolic address after lowering.
        let destination = match copy.remote_mesh {
            Some(mesh) => {
                let stride = 1i128 << m.die.address.runtime().mesh_shift;
                let above = stride << m.die.address.runtime().mesh_bits;
                let bin = |op, a, b| Term::BinOp(op, Box::new(a), Box::new(b));
                let low = bin('%', copy.destination.clone(), Term::int(stride));
                let high = bin(
                    '*',
                    bin('/', copy.destination.clone(), Term::int(above)),
                    Term::int(above),
                );
                Term::Offset(
                    Box::new(high),
                    Box::new(Term::Offset(
                        Box::new(low),
                        Box::new(Term::int(i128::from(mesh) * stride)),
                    )),
                )
            }
            None => copy.destination.clone(),
        };
        let base = Term::Offset(Box::new(destination), Box::new(Term::int(-i128::from(off))));
        let env: Env = [
            ("src".into(), Val::Term(Term::name(&copy.source))),
            ("dst".into(), Val::Term(base)),
        ]
        .into_iter()
        .collect();
        let macros = module.macro_table();
        let stmts = Expander::new(&macros, &module.reader(), true)
            .stmts(&module.body(&name, "l1").map_err(refusal)?.stmts, &env)
            .map_err(refusal)?;
        code.push(stmts);
        images.extend(module.images);
    }
    plan.install_copies(&code, images);
    Ok(())
}
