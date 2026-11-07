//! Complete folds retain every partial boundary within an independent row owner.
use super::*;

#[test]
fn complete_fold_row_owners_preserve_partial_and_bias_bytes() {
    let m = machine();
    let args = json!({
        "R": 97, "W": 768,
        "e": {"nodes": [
            {"load": 0}, {"load": 1}, {"op": "mul", "args": [0, 1]},
            {"reduce": "sum", "arg": 2}, {"load": 2},
            {"op": "add", "args": [3, 4]}
        ], "root": 5},
        "operands": [
            {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 768, "rows": 97},
            {"kind": "col", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 0},
            {"kind": "row", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 112}
        ],
        "out": {"kind": "row", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 112}
    });
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("physical grant");
    let plan = |force: Option<&str>| {
        let mut request = req.clone();
        request.force = force.map(str::to_string);
        call_split("reductions.wide_fold_kernel", &args, &request, &m)
            .expect("complete independent fold")
    };
    let local = plan(Some("p1"));
    let split = plan(Some("rows"));
    let chosen = plan(None);
    assert_eq!(split.split.active_nodes, req.nodes.ids().len());
    assert_eq!(
        chosen.split.cost.makespan,
        local.split.cost.makespan.min(split.split.cost.makespan)
    );
    validate_ownership(&split.split.output).expect("statistic line owners");
    let simulate = |k: &ktpu_kernels::kernel::Kernel| {
        simulate_named_inputs(k, "out", &|_, _, _, _, _| 0, &|i| {
            0x2400 | ((i * 37) as u16 & 0x3ff) | (((i / 7) as u16 & 1) << 15)
        })
    };
    let (want, _) = simulate(&local.kernel);
    let (actual, writes) = simulate(&split.kernel);
    assert_eq!(&actual[..194], &want[..194]);
    assert!(writes.iter().all(|&n| n == 1));
}
