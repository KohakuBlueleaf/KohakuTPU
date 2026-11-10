//! A retained build's frontier work must add across launches, not multiply.
use ka_package::frontier_profile::{measure, Work};
use ktpu_l0::program::{BuildOpts, Program, Stmt};
use ktpu_l0::{Cond, Facts, Op, Package, Word};
use super::common::card;

fn launch(token: usize, mesh: usize, nodes: usize, coord: (u32, u32), branch: bool) -> Program {
    let mut p = Program::new();
    if token != 0 && nodes > 1 {
        if mesh == 0 {
            p.wait_slot(9, (nodes - 1) as u64, true);
            for other in 1..nodes { p.ring(other as u64, 9, 1, false); }
        } else {
            p.ring(0, 9, 1, false);
            p.wait_slot(9, 1, true);
        }
    }
    p.send(coord, vec![Word::ZERO], vec![]);
    p.steps.push(Stmt::Mark { coord, token });
    if branch { p.jump(&format!("join{token}"), Cond::Ne, 0, 0, true); }
    p.send(coord, vec![Word::ZERO], vec![]);
    if branch { p.steps.push(Stmt::Label(format!("join{token}"))); }
    p.steps.push(Stmt::Wait { coord, token: Some(token) });
    p.barrier();
    p
}

fn launches(n: usize, mesh: usize, nodes: usize, coord: (u32, u32), branch: bool) -> Program {
    let mut p = Program::new();
    for t in 0..n { p.steps.extend(launch(t, mesh, nodes, coord, branch).steps); }
    p
}

fn built(p: &Program, f: &Facts, composed: bool) -> (Package, Work) {
    measure(|| {
        let b = if composed { p.build_composed(f, BuildOpts::default()) }
            else { p.build(f, BuildOpts::default()) }.expect("frontier build");
        b.build(false, false)
    })
}

fn work(w: Work) -> u64 { w.entries + w.snapshots }

// Restricted independent layout oracle: these fixtures use only dispatch
// payloads, direct jumps and waits, with no relocations or runtime mark slots.
fn alone(n: usize, mesh: usize, nodes: usize, f: &Facts, branch: bool) -> Package {
    let coord = f.units["VC"][0];
    let mut out: Option<Package> = None;
    for t in 0..n {
        let mut part = built(&launch(t, mesh, nodes, coord, branch), f, false).0;
        if let Some(all) = &mut out {
            let payload = all.payloads.len() as u64;
            let step = all.steps.len() as u64;
            for s in &mut part.steps {
                match s.op {
                    Op::Dispatch => s.arg += payload,
                    Op::Jump => s.unit += step,
                    _ => {}
                }
            }
            all.payloads.extend(part.payloads);
            all.steps.extend(part.steps);
        } else { out = Some(part); }
    }
    out.unwrap()
}

#[test]
fn composed_frontier_work_is_sum_of_launches_even_with_branch_joins() {
    let machine = card();
    for nodes in [1, 2, 4] {
        for branch in [false, true] {
            let mut counts = Vec::new();
            for n in [8, 32] {
                let mut total = 0;
                for mesh in 0..nodes {
                    let f = Facts::of_mesh(&machine, mesh as u32).unwrap();
                    let p = launches(n, mesh, nodes, f.units["VC"][0], branch);
                    let (got, w) = built(&p, &f, true);
                    total += work(w);
                    let want = alone(n, mesh, nodes, &f, branch);
                    assert_eq!(got.to_bytes(&f).unwrap(), want.to_bytes(&f).unwrap(),
                        "P={nodes} mesh={mesh} launches={n} branch={branch}");
                }
                counts.push(total);
            }
            assert!(counts[1] <= 5 * counts[0], "4x launches must be linear: {counts:?}");
        }
    }
    // The unsegmented production matrix is a red control under the same meter;
    // a missing new API is not considered evidence that the behavior fails base.
    let f = Facts::of_mesh(&machine, 0).unwrap();
    let old: Vec<_> = [8, 32].into_iter().map(|n| {
        let p = launches(n, 0, 1, f.units["VC"][0], false);
        work(built(&p, &f, false).1)
    }).collect();
    assert!(old[1] > 8 * old[0], "the base DBM must fail the linear bound: {old:?}");
}
