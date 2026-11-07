//! A loop retains its enclosing sysnode ownership while local units are queued.

use ktpu_graph::{Role, UnitKind};
use ktpu_kernels::kernel::Kernel;
use ktpu_kernels::trace::buffer::{f16, sl, ALL};
use ktpu_kernels::trace::control::At;
use ktpu_kernels::trace::{Facts, Trace};

use super::{machine, request, simulate_named_inputs};

#[test]
fn looped_shares_queue_vector_instances_on_the_granted_nodes() {
    let m = machine();
    let request = request(&m);
    let facts = Facts::of(&m);
    let instances = facts.vc * 2;
    let rows = request.nodes.ids().len() as i64 * 2 * instances * 4;
    let spec = f16(&[rows, 32]).expect("rows");
    let given = vec![("x".into(), spec.clone()), ("out".into(), spec.clone())];
    let mut t = Trace::new("looped_shares", facts, &m.die.target);
    let x = t.port("x", Role::In, spec.clone()).expect("input");
    let out = t.port("out", Role::Out, spec).expect("output");
    t.on_nodes(&request.nodes, |t, rank| {
        t.range_pipe(2, Some(2), |t, wave| {
            t.par(instances, UnitKind::Vc, At::Any, |t, core| {
                let first = rank.mul(2).add(&wave).mul(instances).add(core).mul(4);
                t.range(2, |t, trip| {
                    let row = first.add(&trip.mul(2));
                    let view = [sl(&row, row.add(2)), ALL];
                    let value = t.load(&x.at(&view)?, true)?;
                    t.store(&out.at(&view)?, value.into())
                })
            })
        })
    })
    .expect("looped shares");
    let mut graph = serde_json::to_value(t.graph().expect("graph")).expect("graph JSON");
    fn symbolic_origins(value: &mut serde_json::Value) {
        match value {
            serde_json::Value::Object(object) => {
                if let Some(serde_json::Value::Array(axes)) = object.get_mut("axes") {
                    axes[1] = serde_json::json!({"run": [{"const": 0, "terms": []}, 32]});
                }
                for child in object.values_mut() {
                    symbolic_origins(child);
                }
            }
            serde_json::Value::Array(array) => array.iter_mut().for_each(symbolic_origins),
            _ => {}
        }
    }
    symbolic_origins(&mut graph);
    let graph = serde_json::from_value(graph).expect("symbolic view origins");
    let text = ktpu_graph::lower(&graph)
        .expect("symbolic-origin text")
        .text();
    ktpu_l1::text::read(&text, "symbolic-origins.l2").expect("readable view operands");
    let program = ktpu_graph::program(&graph, &m).expect("program");
    let body = program.body().expect("body");
    for stage in ktpu_l2::ir::instances(&body).expect("instances") {
        assert!(request.nodes.ids().contains(&stage.node));
    }
    let tag = |i: i64| 0x3000 | (i as u16 & 0x3ff);
    let kernel = Kernel { graph, given };
    let (actual, writes) = simulate_named_inputs(&kernel, "out", &|_, _, _, _, _| 0, &tag);
    let expected: Vec<_> = (0..rows * 32).flat_map(|i| tag(i).to_le_bytes()).collect();
    assert_eq!(actual, expected);
    assert!(writes.iter().all(|&n| n == 1));
}
