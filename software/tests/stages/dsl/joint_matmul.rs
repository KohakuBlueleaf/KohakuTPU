//! Global column ownership and local row/column waves preserve packed inputs.
use super::*;

#[test]
fn joint_tiles_keep_weights_with_their_column_owner() {
    let m = machine();
    let args = json!({
        "mm": {"made_by": {"gen": "matmul.plan", "args": {
            "bt": 1, "m": 8, "n": 256, "k": 64, "pad_m": false,
            "dense_n": true
        }}},
        "e": {"nodes": [{"load": 0}, {"load": 1},
            {"op": "add", "args": [0, 1]}], "root": 2},
        "extras": [{"kind": "col", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 0}],
        "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 256, "rows": 8},
        "ring_depth": 2, "tail_bands": true
    });
    let baseline = ktpu_kernels::call("matmul.mm_kernel", &args, &m).expect("local product");
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("physical grant");
    req.force = Some("n-tile-4x32".into());
    let got = call_split("matmul.mm_kernel", &args, &req, &m).expect("joint ownership");
    validate_ownership(&got.split.output).expect("output owners");
    let (want, _) = simulate_matmul(&baseline);
    let (actual, writes) = simulate_matmul(&got.kernel);
    assert_eq!(actual, want);
    assert!(writes.iter().all(|&n| n == 1));

    let b = baseline
        .given
        .iter()
        .find(|(name, _)| name == "b")
        .expect("weights");
    let bytes = b.1.shape.iter().product::<i64>() as usize;
    let mut owners = vec![None; bytes];
    for share in &got.split.shards {
        for region in share.read_regions.iter().filter(|r| r.allocation == "b") {
            for owner in &mut owners[region.bytes.start as usize..region.bytes.end as usize] {
                assert!(owner.is_none() || *owner == Some(share.logical_node));
                *owner = Some(share.logical_node);
            }
        }
    }
    assert!(owners.iter().all(Option::is_some));
    req.force = None;
    let selected = call_split("matmul.mm_kernel", &args, &req, &m).expect("priced choice");
    assert!(selected.split.cost.makespan <= got.split.cost.makespan);

    req.force = Some("p1-tile-4x32".into());
    req.nodes = NodeSet::new(vec![0], &m).expect("first physical owner");
    let first = call_split("matmul.mm_kernel", &args, &req, &m).expect("first-owner tile");
    req.nodes = NodeSet::new(vec![3], &m).expect("another physical owner");
    let other = call_split("matmul.mm_kernel", &args, &req, &m).expect("other-owner tile");
    assert_eq!(first.split.cost.makespan, other.split.cost.makespan);
}
