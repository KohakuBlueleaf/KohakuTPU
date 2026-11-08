//! Owner-local divisors and column-grain ownership compete by complete price.
use super::*;

#[test]
fn unequal_owners_keep_their_local_waves_and_ordered_output() {
    let m = machine();
    let args = json!({
        "mm": {"made_by": {"gen": "matmul.plan", "args": {
            "bt": 1, "m": 36, "n": 128, "k": 64,
            "pad_m": false, "dense_n": true
        }}},
        "e": {"nodes": [{"load": 0}, {"load": 1},
            {"op": "add", "args": [0, 1]}], "root": 2},
        "extras": [{"kind": "col", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 0}],
        "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 128, "rows": 36},
        "ring_depth": 2
    });
    let baseline = ktpu_kernels::call("matmul.mm_kernel", &args, &m).expect("ordered product");
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("grant");
    req.force = Some("m-tile-4x32".into());
    let common = call_split("matmul.mm_kernel", &args, &req, &m).expect("common rectangles");
    req.force = Some("m-owner-waves-1".into());
    let local = call_split("matmul.mm_kernel", &args, &req, &m).expect("owner rectangles");
    validate_ownership(&local.split.output).expect("owners");
    let (want, _) = simulate_matmul(&baseline);
    let (actual, writes) = simulate_matmul(&local.kernel);
    assert_eq!(actual, want);
    assert!(writes.iter().all(|&count| count == 1));
    assert!(local.split.cost.makespan < common.split.cost.makespan);
    req.force = None;
    let selected = call_split("matmul.mm_kernel", &args, &req, &m).expect("minimum price");
    assert!(selected.split.cost.makespan <= local.split.cost.makespan);
}

#[test]
fn column_grains_preserve_global_operand_origins_on_unequal_owners() {
    let m = machine();
    let args = json!({
        "mm": {"made_by": {"gen": "matmul.plan", "args": {
            "bt": 1, "m": 8, "n": 640, "k": 32,
            "pad_m": false, "dense_n": true
        }}},
        "e": {"nodes": [{"load": 0}], "root": 0},
        "extras": [],
        "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 640, "rows": 8}
    });
    let baseline = ktpu_kernels::call("matmul.mm_kernel", &args, &m).expect("ordered product");
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("grant");
    req.force = Some("n-grain-128-owner-waves-1".into());
    let local = call_split("matmul.mm_kernel", &args, &req, &m).expect("grain ownership");
    validate_ownership(&local.split.output).expect("owners");
    let (want, _) = simulate_matmul(&baseline);
    let (actual, writes) = simulate_matmul(&local.kernel);
    assert_eq!(actual, want);
    assert!(writes.iter().all(|&count| count == 1));
    req.force = None;
    let selected = call_split("matmul.mm_kernel", &args, &req, &m).expect("minimum price");
    assert!(selected.split.cost.makespan <= local.split.cost.makespan);
}

#[test]
fn row_grains_preserve_short_batches_with_independent_weights() {
    use ktpu_kernels::gen::matmul::{self, split as matrix};

    let m = machine();
    let mut knobs = matmul::unrecorded();
    knobs.pad_m = false;
    let mm = matmul::plan(
        2,
        37,
        64,
        64,
        false,
        4,
        ktpu_machine::Pairing::Unpaired,
        &m,
        &knobs,
    )
    .expect("short batched product");
    let e = ktpu_kernels::expr::load(0);
    let out = ktpu_kernels::Operand::full(mm.n, mm.bt * mm.mp);
    let made = BTreeMap::new();
    let shape = matrix::Shape {
        mm: &mm,
        class: None,
        e: &e,
        extras: &[],
        out: &out,
        knobs: &knobs,
        made: &made,
        demands: &[],
    };
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("permuted owners");
    req.force = Some("p1".into());
    let ordered = matrix::kernel(&shape, &req, &m).expect("ordered original product");
    let (want, _) = simulate_matmul(&ordered.kernel);
    let options = matrix::split_options(&shape, &req, &m).expect("ownership alternatives");
    let grain = format!(
        "m-row-grain-{}",
        i64::try_from(m.die.units.mg.len()).expect("MG count") * mm.quantum
    );
    let mut best_forced = i64::MAX;
    for name in [grain.clone(), format!("{grain}-owner-waves-1")] {
        let option = options
            .iter()
            .find(|o| o.id == name)
            .expect("row-grain candidate");
        let floor = matrix::resource_floor(&shape, &req, option, &m).expect("geometry bound");
        req.force = Some(name);
        let planned = matrix::kernel(&shape, &req, &m).expect("priced row-grain product");
        assert!(floor <= planned.split.cost.makespan);
        validate_ownership(&planned.split.output).expect("exact owner regions");
        let (actual, writes) = simulate_matmul(&planned.kernel);
        assert_eq!(actual, want);
        assert!(writes.iter().all(|&n| n == 1));
        best_forced = best_forced.min(planned.split.cost.makespan);
    }
    req.force = None;
    let selected = matrix::kernel(&shape, &req, &m).expect("automatic complete price");
    assert!(selected.split.cost.makespan <= best_forced);
}

#[test]
fn resident_weights_reduce_reads_without_changing_ordered_products_or_rings() {
    use ktpu_kernels::gen::matmul::{self, split as matrix};
    use ktpu_kernels::split::SplitAxis;
    use ktpu_l2::{compile_l1, Program, Search, PASSES};

    for depth in [0, 2] {
        let m = if depth > 0 {
            machine()
                .with_fact("die.staging.entries", 64.into())
                .expect("staging variant")
        } else {
            machine()
        };
        let mut knobs = matmul::unrecorded();
        knobs.pad_m = false;
        knobs.ring_depth = depth;
        let mm = matmul::plan(2, 20, 256, 128, false, 4, ktpu_machine::PAIRING, &m, &knobs)
            .expect("original K chunks and independent batches");
        let expression = json!({
            "nodes": [{"load": 0}, {"load": 1}, {"op": "add", "args": [0, 1]}],
            "root": 2
        });
        // Use the public expression parser through the existing call interface.
        let args = json!({
            "mm": {"made_by": {"gen": "matmul.plan", "args": {
                "bt": 2, "m": 20, "n": 256, "k": 128, "shared": false,
                "pad_m": false
            }}},
            "e": expression,
            "extras": [{"kind": "col", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 0}],
            "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 256, "rows": 40},
            "ring_depth": depth
        });
        let baseline =
            ktpu_kernels::call("matmul.mm_kernel", &args, &m).expect("ordered biased product");
        let mut req = request(&m);
        req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("physical grant");
        req.force = Some("n-resident-b-2".into());
        let resident = call_split("matmul.mm_kernel", &args, &req, &m).expect("retained B chunks");
        let SplitAxis::ResidentB { tiles } = &resident.split.axis else {
            panic!("forced resident ownership");
        };
        let tile = tiles.first().expect("active owner");
        req.force = Some(format!("n-tile-{}x{}", tile.tm, tile.tn));
        let ordinary =
            call_split("matmul.mm_kernel", &args, &req, &m).expect("same independent tiles");
        let report = |kernel: &ktpu_kernels::kernel::Kernel| {
            let text = ktpu_graph::lower(&kernel.graph).expect("lowered graph");
            let program =
                Program::parse(&text.text(), "resident B report", Some(&text.name)).expect("L2");
            let compiled = compile_l1(&program, &m, &PASSES, true, &Search::default())
                .expect("scheduled product");
            let resources = ktpu_l2::cost::resource::kernel(
                &compiled.l1,
                &text.name,
                &m,
                &ktpu_l2::cost::resource::Params::planning(&m),
            )
            .expect("resource report");
            (resources, compiled.l1.source)
        };
        let (retained, executable) = report(&resident.kernel);
        let (separate, _) = report(&ordinary.kernel);
        assert!(
            retained["work"]["read_bytes"].as_f64().expect("reads")
                < separate["work"]["read_bytes"].as_f64().expect("reads")
        );
        assert_eq!(retained["work"]["macs"], separate["work"]["macs"]);
        let (want, _) = simulate_matmul(&baseline);
        let (actual, writes) = simulate_matmul(&resident.kernel);
        assert_eq!(actual, want);
        assert!(writes.iter().all(|&count| count == 1));
        let (delivered, writes) = super::simulate_matmul_executable(
            &resident.kernel,
            Some(&executable),
            &|side, group, block, lane, k| {
                let tag = group * 71 + block * 29 + lane * 13 + k * 3 + i64::from(side);
                0x2400 | (tag as u16 & 0x3ff) | (((tag / 5) as u16 & 1) << 15)
            },
            &|i| 0x2800u16 | ((i * 37) as u16 & 0x3ff),
            &m,
        );
        assert_eq!(delivered, want);
        assert!(writes.iter().all(|&count| count == 1));
        req.force = None;
        let selected =
            call_split("matmul.mm_kernel", &args, &req, &m).expect("minimum complete price");
        assert!(selected.split.cost.makespan <= resident.split.cost.makespan);
        // Exercise the analytical floor with the same typed resident geometry.
        let e = ktpu_kernels::expr::load(0);
        let out = ktpu_kernels::Operand::full(mm.n, mm.bt * mm.mp);
        let made = BTreeMap::new();
        let shape = matrix::Shape {
            mm: &mm,
            class: None,
            e: &e,
            extras: &[],
            out: &out,
            knobs: &knobs,
            made: &made,
            demands: &[],
        };
        let options = matrix::split_options(&shape, &req, &m).expect("resident geometry");
        let option = options
            .iter()
            .find(|o| o.id == "n-resident-b-2")
            .expect("resident option");
        let floor = matrix::resource_floor(&shape, &req, option, &m).expect("shared-fill bound");
        req.force = Some(option.id.clone());
        let priced = matrix::kernel(&shape, &req, &m).expect("forced resident bound witness");
        assert!(floor <= priced.split.cost.makespan);
    }
}

#[test]
fn identity_output_layout_conversion_has_a_priced_product_ring() {
    let m = machine();
    let args = json!({
        "mm": {"made_by": {"gen": "matmul.plan", "args": {
            "bt": 1, "m": 31, "n": 384, "k": 64, "pad_m": false
        }}},
        "e": {"nodes": [{"load": 0}], "root": 0},
        "extras": [],
        "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 384, "rows": 32},
        "ring_depth": 2
    });
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("grant");
    req.force = Some("n-tile-8x32".into());
    let ordinary =
        call_split("matmul.mm_kernel", &args, &req, &m).expect("ordinary layout conversion");
    req.force = Some("n-tile-8x32-layout-ring".into());
    let pipelined =
        call_split("matmul.mm_kernel", &args, &req, &m).expect("layout conversion ring");
    let mut shallower = args.clone();
    shallower["ring_depth"] = json!(1);
    let single_wave =
        call_split("matmul.mm_kernel", &shallower, &req, &m).expect("one-wave layout conversion");
    assert_ne!(single_wave.kernel.graph.name, pipelined.kernel.graph.name);
    let text = ktpu_graph::lower(&pipelined.kernel.graph)
        .expect("ring graph")
        .text();
    assert!(text.contains("pipe=2"));
    let (expected, _) = simulate_matmul(&ordinary.kernel);
    let (actual, writes) = simulate_matmul(&pipelined.kernel);
    assert_eq!(actual, expected);
    assert!(writes.iter().all(|&n| n == 1));
    req.force = None;
    let selected =
        call_split("matmul.mm_kernel", &args, &req, &m).expect("minimum complete product price");
    assert!(selected.split.cost.makespan <= ordinary.split.cost.makespan);
    assert!(selected.split.cost.makespan <= pipelined.split.cost.makespan);
}
