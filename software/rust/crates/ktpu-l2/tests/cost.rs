//! `ktpu_l2::cost` against cycles worked by hand from the machine's facts.

use ktpu_l1::text::read;
use ktpu_l2::cost::cycles;
use ktpu_l2::target::Target;
use ktpu_machine::Machine;
use serde_json::json;

fn machine() -> Machine {
    Machine::named("multimesh_v9t6").unwrap()
}

fn cost(body: &str, m: &Machine) -> i64 {
    let text = format!("target ktpu.v9t6 vregs=128 vcopies=2\n\n{body}\n");
    cycles(&read(&text, "<cost>").unwrap(), "k", &Target::of(m)).unwrap()
}

const PORTS: &str = "fn k.l1(a: f16[8192, 16] @dram, b: f16[8192, 16] @dram, i: f32[64] @dram)";

fn move_of(copy: &str, m: &Machine) -> i64 {
    cost(&format!("{PORTS}\n    move\n        {copy}"), m)
}

/// `run`s of cycles apiece and `words` of a cycle each after the run's front.
fn front_and(m: &Machine, runs: i64, words: i64) -> i64 {
    let (a_run, a_word) = m.mover.copy_cost;
    m.vc.launch_front as i64 + runs * a_run as i64 + words * a_word as i64
}

#[test]
fn a_copy_costs_its_runs_and_its_words() {
    let m = machine();
    let flat = move_of("copy w(a+0, (100, 32)) -> w(b+0, (100, 32))", &m);
    assert_eq!(flat, front_and(&m, 1, 100));
    let rows = move_of("copy w(a+0, (10, 1024), (10, 32)) -> w(b+0, (100, 32))", &m);
    assert_eq!(rows, front_and(&m, 10, 100));
    let strided = move_of("copy w(a+0, (100, 64)) -> w(b+0, (100, 32))", &m);
    assert_eq!(strided, front_and(&m, 100, 100));
    let back = move_of("copy w(a+640, (10, -32)) -> w(b+0, (10, 32))", &m);
    assert_eq!(back, front_and(&m, 10, 10));
    let written = move_of("copy w(a+0, (10, 32)) -> w(b+0, (10, 64))", &m);
    assert_eq!(written, front_and(&m, 10, 10));
}

#[test]
fn a_run_ends_at_the_burst_limit() {
    let m = machine();
    let burst = m.mover.burst_max as i64;
    let copy = format!("copy w(a+0, ({}, 32)) -> w(b+0, ({0}, 32))", 3 * burst);
    assert_eq!(move_of(&copy, &m), front_and(&m, 3, 3 * burst));
}

#[test]
fn a_gather_costs_rows_of_words() {
    let m = machine();
    let gather = "gather a+0 -> w(b+0, (24, 256), (2, 32)) fp index=i+0 n=24 pitch=256 words=2";
    assert_eq!(move_of(gather, &m), front_and(&m, 24, 48));
}

#[test]
fn a_posted_move_runs_beside_the_node() {
    let m = machine();
    let copy = "copy w(a+0, (100, 32)) -> w(b+0, (100, 32))";
    let posted = format!("{PORTS}\n    t = move posted\n        {copy}\n    wait moves t");
    assert_eq!(cost(&posted, &m), move_of(copy, &m));
    let twice = format!("{PORTS}\n    t = move posted\n        {copy}\n    r = move posted\n        {copy}\n    wait moves r");
    assert_eq!(cost(&twice, &m), front_and(&m, 2, 200));
}

fn fills(walk: &str) -> i64 {
    let m = machine();
    cost(
        &format!(
            "image k.i()\n    vfill a0 -> l1[0] rel\n    vbar\n    halt\n\n{PORTS}\n    send vc[0]\n        desc a0=a+0 + 0 walk={walk}\n        run k.i()\n    wait vc[0]"
        ),
        &m,
    )
}

#[test]
fn an_image_fills_the_walk_sent_with_it() {
    let m = machine();
    let (_, per_word) = Target::of(&m).mem(false);
    let (a_run, _) = m.mover.copy_cost;
    let contiguous = fills("((32, 16),)");
    let eight_words = fills("((32, 8),)");
    assert_eq!(contiguous - eight_words, 8 * per_word);
    let strided = fills("((32, 2), (4096, 8))");
    assert_eq!(strided - contiguous, 7 * a_run as i64);
}

#[test]
fn a_send_and_a_load_are_node_steps() {
    let m = machine();
    let send = format!(
        "image k.i()\n    halt\n\n{PORTS}\n    send vc[0]\n        run k.i()\n    wait vc[0]"
    );
    let alone = cost(&send, &m);
    let slower = m.with_fact("vc.step_cycles", json!(m.vc.step_cycles + 100));
    assert_eq!(cost(&send, &slower.unwrap()) - alone, 100);
    let loaded = format!("{PORTS}\n    r1 = load a+0\n    assert_zero r1 code=1");
    assert_eq!(
        cost(&loaded, &m),
        (m.vc.launch_front + 2 * m.vc.step_cycles) as i64
    );
}
