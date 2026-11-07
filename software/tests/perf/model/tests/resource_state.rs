//! Dispatch state and consumed notification generations constrain resource time.
use std::path::Path;

use ktpu_l1::text::read;
use ktpu_machine::Machine;
use ktpu_perf::model::{kernel, Params};
use serde_json::Value;

fn profile() -> (Machine, Params) {
    let perf = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("perf");
    let machine =
        Machine::load(perf.join("../../../machines/multimesh_v9t8.json")).expect("machine");
    let params: Value = serde_json::from_str(
        &std::fs::read_to_string(perf.join("params.json")).expect("parameters"),
    )
    .expect("parameter JSON");
    let mut params = Params::of(&params, "card", &machine);
    params.node_floor = 0.0;
    params.node_start = 0.0;
    (machine, params)
}

#[test]
fn restoring_a_descriptor_costs_an_update() {
    let (machine, mut params) = profile();
    params.node_send = 0.0;
    params.node_image = 0.0;
    let price = |third: &str| {
        let mut source = String::from(concat!(
            "target ktpu.v9\nimage idle()\n    halt\n",
            "fn descriptors.l1(x: f16[128] @dram, y: f16[128] @dram)\n",
        ));
        for input in ["x", "y", third] {
            source.push_str(&format!(
                "    send vc[0]\n        desc a0={input} walk=(32,8)\n        run idle()\n",
            ));
        }
        let module = read(&source, "descriptor updates").expect("L1");
        kernel(&module, "descriptors", &machine, &params).expect("price")["cycles"]
            .as_f64()
            .expect("cycles")
    };
    assert!(price("x") > price("y"));
}

#[test]
fn each_wait_consumes_its_own_notification_generation() {
    let (machine, params) = profile();
    let price = |second_slot: usize| {
        let source = format!(
            "target ktpu.v9\nimage work()\n    for i in 0..256\n        vadd v1, v2, v3\n    halt\n\nfn slots.l1(x: f16[16384] @dram, y: f16[16384] @dram)\n    node 0\n        wait slot 7 n=1 consume\n        wait slot {second_slot} n=1 consume\n        send vc[0]\n            run work()\n        barrier\n    node 1\n        ring mesh=0 slot=7 amount=1\n        move\n            copy w(x, (1024,32)) -> w(y, (1024,32))\n        ring mesh=0 slot={second_slot} amount=1\n",
        );
        let module = read(&source, "slot generations").expect("L1");
        kernel(&module, "slots", &machine, &params).expect("price")["cycles"]
            .as_f64()
            .expect("cycles")
    };
    assert_eq!(price(7), price(8));
}

#[test]
fn native_counted_receive_waits_for_the_payload() {
    let (machine, mut params) = profile();
    params.ring = 0.0;
    params.node_wait = 0.0;
    params.node_word = 0.0;
    params.node_send = 0.0;
    let price = |explicit: bool| {
        let wait = if explicit {
            "        wait slot 7 n=1 consume\n"
        } else {
            ""
        };
        let source = format!(
            "target ktpu.v9\nfn peer.l1(a: mx7a[256,32] @dram(gm=64,nk=1), b: mx7b[256,32] @dram(gn=64,nk=1), y: f16[256,256] @dram(tile=64x64))\n    node 0\n        send mg[0]\n            fill A0 <- a n=64\n            fill B0 <- b n=64\n            gemm 64x64x1 a=A0 b=B0 acc=0\n        wait mg[0]\n{wait}        send mg[0]\n            fill A0 recv n=1\n            drain y n=4096\n    node 1\n        send mg[0]\n            fill A0 <- a n=64\n            fill B0 <- b n=64\n            gemm 64x64x1 a=A0 b=B0 acc=0\n            drain n=4096 to=mesh0.mg[0] add\n            drain n=4 to=mesh0.mg[0] buf=0 l1=0 count\n        wait mg[0]\n        ring mesh=0 slot=7 amount=1\n",
        );
        let module = read(&source, "native peer completion").expect("L1");
        kernel(&module, "peer", &machine, &params).expect("price")["cycles"]
            .as_f64()
            .expect("cycles")
    };
    assert_eq!(price(false), price(true));
}

#[test]
fn slot_arrival_order_is_physical_and_invalid_counter_dependencies_are_refused() {
    let (machine, params) = profile();
    let scopes = [
        "    node 0\n        wait slot 7 n=1 consume\n        ring mesh=1 slot=8 amount=1\n",
        "    node 1\n        wait slot 8 n=1 consume\n        ring mesh=0 slot=7 amount=1\n",
        "    node 2\n        ring mesh=0 slot=7 amount=1\n",
    ];
    let price = |order: &[usize]| {
        let mut source = "target ktpu.v9\nfn notifications.l1()\n".to_string();
        for &index in order {
            source.push_str(scopes[index]);
        }
        let module = read(&source, "notification ordering").expect("L1");
        kernel(&module, "notifications", &machine, &params).expect("complete dependency graph")
            ["cycles"]
            .as_f64()
            .expect("cycles")
    };
    assert_eq!(price(&[0, 1, 2]), price(&[2, 1, 0]));
    let incomplete = read(
        "target ktpu.v9\nfn missing.l1()\n    node 0\n        wait slot 7 n=2 consume\n    node 1\n        ring mesh=0 slot=7 amount=1\n",
        "incomplete notification",
    )
    .expect("L1");
    assert!(kernel(&incomplete, "missing", &machine, &params).is_err());
}
