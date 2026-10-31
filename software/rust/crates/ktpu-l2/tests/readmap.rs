//! Composed reads (L2.READ): what each lowering rule emits, each rule's
//! mutant, and the machine fit.
#![cfg(feature = "mutants")]

use ktpu_l2::mutant::{scoped, State};
use ktpu_l2::{compile_with, Program, Search, Target, PASSES};
use ktpu_machine::Machine;
use serde_json::json;

const QUANT: &str = "target ktpu.v9
fn mapped.l2(x: f16[1,16] @dram, q: mx7a[8,64] @dram(gm=2,nk=1))
    quant q <- x read_map=((16,0,0,256,((8,1),(32,0)),((8,32),(32,1))), (256,0,0,512,((8,32),(2,0),(32,1)),((8,64),(2,32),(32,1)))) read_shape=(64,0,0,8,64) overlap=1
";

const DSL: &str = "target ktpu.v9
fn mapped_dsl.l2(x: f16[16,16] @dram, y: f16[16,128] @dram)
    par c in 0..2 on vc[c]
        for t in 0..4
            a = load x[:, :] read_map=((256,0,0,2048,((16,16),(8,0),(16,1)),((16,128),(8,16),(16,1))),) read_at=(c*8+t*2)*128 : f16[2,128] @l1
            v = unpack a : f32[2,128] @reg
            w = add v, 1.0 : f32[2,128] @reg
            b = pack w : f16[2,128] @l1
            store y[c*8+t*2:+2, :] <- b
";

const INVARIANT: &str = "target ktpu.v9
fn invariant.l2(x: f16[1,16] @dram, z: f16[8,128] @dram, y: f16[8,128] @dram)
    par c in 0..1 on vc[c]
        a = load x[:, :] read_map=((16,0,0,128,((8,0),(16,1)),((8,16),(16,1))),) read_at=0 : f16[1,128] @l1
        u = unpack a : f32[1,128] @reg
        for t in 0..8
            b = load z[t:+1, :] : f16[1,128] @l1
            v = unpack b : f32[1,128] @reg
            w = add v, u : f32[1,128] @reg
            q = pack w : f16[1,128] @l1
            store y[t:+1, :] <- q
";

const PHASED: &str = "target ktpu.v9
fn phased.l2(x: f16[25,16] @dram, y: f16[20,128] @dram)
    par c in 0..1 on vc[c]
        for t in 0..10
            a = load x[:, :] read_map=((400,0,0,2560,((20,20),(20,1)),((20,128),(20,1))),) read_at=t*256 : f16[2,128] @l1
            v = unpack a : f32[2,128] @reg
            q = pack v : f16[2,128] @l1
            store y[t*2:+2, :] <- q
";

const SPARSE: &str = "target ktpu.v9
fn sparse.l2(x: f16[128,32] @dram, q: mx7a[4,32] @dram(gm=1,nk=1))
    quant q <- x read_map=((4096,0,0,128,((128,32),),((128,1),)),) read_shape=(32,0,0,4,32) overlap=1
";

const STEADY: &str = "target ktpu.v9
fn steady.l2(x: f16[256,32] @dram, q: mx7a[256,32] @dram(gm=1,nk=1))
    quant q <- x read_map=((8192,0,0,8192,((256,32),(32,1)),((256,32),(32,1))),) read_shape=(32,0,0,256,32) overlap=1
";

const BATCHED: &str = "target ktpu.v9
fn batched.l2(x: f16[8,32] @dram, q: mx7a[16,32] @dram(gm=1,nk=1))
    quant q <- x read_map=((256,0,0,256,((8,32),(32,1)),((8,32),(32,1))),) read_shape=(32,0,0,4,32) read_batch=(2,8)
";

/// Entry rows 72 values apart: word runs of 2 and 3 no one walk reaches.
const IRREGULAR: &str = "target ktpu.v9
fn irregular.l2(x: f16[4,72] @dram, q: mx7a[4,32] @dram(gm=1,nk=1))
    quant q <- x read_map=((288,0,0,288,((4,72),(72,1)),((4,72),(72,1))),) read_shape=(72,0,0,4,32)
";

const IRREGULAR_LOAD: &str = "target ktpu.v9
fn spaced.l2(x: f16[4,1000] @dram, y: f16[4,128] @dram)
    par c in 0..1 on vc[c]
        for t in 0..1
            a = load x[:, :] read_map=((4000,0,0,4000,((4,1000),(1000,1)),((4,1000),(1000,1))),) read_pitch=1000 : f16[4,128] @l1
            v = unpack a : f32[4,128] @reg
            b = pack v : f16[4,128] @l1
            store y[0:+4, :] <- b
";

/// Two rows past a descriptor's step: the descriptor is rewritten between them.
const FAR: &str = "target ktpu.v9
fn far.l2(x: f16[2,65536] @dram, y: f16[2,128] @dram)
    par c in 0..1 on vc[c]
        for t in 0..1
            a = load x[:, :] read_map=((131072,0,0,131072,((2,65536),(65536,1)),((2,65536),(65536,1))),) read_pitch=65536 : f16[2,128] @l1
            v = unpack a : f32[2,128] @reg
            b = pack v : f16[2,128] @l1
            store y[0:+2, :] <- b
";

fn target() -> Target {
    let m = Machine::named("multimesh_v9t6")
        .unwrap()
        .with_fact("conversion.fp16_mx7.preferred_backend", json!("vector_mx7"))
        .unwrap();
    Target::of(&m).with_nodes(1)
}

fn program(text: &str, name: &str) -> Program {
    Program::parse(text, "<readmap>", Some(name)).unwrap()
}

fn l1_on(text: &str, name: &str, t: &Target) -> Result<String, String> {
    compile_with(&program(text, name), t, &PASSES, false, &Search::default())
        .map(|c| c.l1_text)
        .map_err(|e| format!("{}: {}", e.clause, e.message))
}

fn l1(text: &str, name: &str) -> String {
    l1_on(text, name, &target()).unwrap()
}

fn under<T>(mutant: &'static str, f: impl FnOnce() -> T) -> T {
    scoped(
        State {
            active: Some(mutant),
            ..Default::default()
        },
        f,
    )
}

/// The most fills an image takes with no `vsync fe on=fill` between them.
fn fills_unsynced(l1: &str) -> usize {
    let (mut most, mut held) = (0, 0);
    for line in l1.lines().map(str::trim) {
        if line.starts_with("vfill ") {
            held += 1;
            most = most.max(held);
        } else if line.starts_with("vsync fe on=fill") || line.starts_with("image ") {
            held = 0;
        }
    }
    most
}

#[test]
fn a_composed_pack_writes_no_fp16_intermediate() {
    let good = l1(QUANT, "mapped");
    assert!(
        good.contains("vpack v0") && good.contains("bcast("),
        "{good}"
    );
    assert!(
        !good.contains("copy ") && !good.contains("gather "),
        "{good}"
    );
}

#[test]
fn every_unpack_of_a_fill_waits_for_it() {
    let good = l1(QUANT, "mapped");
    assert!(good.contains("vsync fe on=fill"), "{good}");
    let bad = under("l2.readmap/no-fill-wait", || l1(QUANT, "mapped"));
    assert!(!bad.contains("vsync fe on=fill"), "{bad}");
}

#[test]
fn a_batch_reads_its_own_rows() {
    // Entry row 8 is the second batch's first row: logical row 4, 256 bytes in.
    let good = l1(BATCHED, "batched");
    assert!(good.contains("x + 256"), "{good}");
    let bad = under("l2.readmap/batch-base", || l1(BATCHED, "batched"));
    assert!(!bad.contains("x + 256"), "{bad}");
}

#[test]
fn an_invariant_load_takes_no_stream_phase() {
    let t = target();
    assert!(l1_on(INVARIANT, "invariant", &t).is_ok());
    let bad = std::panic::catch_unwind(|| {
        under("l2.readmap/invariant-phase", || {
            l1_on(INVARIANT, "invariant", &t)
        })
    });
    assert!(bad.is_err());
}

#[test]
fn a_retile_cost_counts_every_lifted_trip() {
    let (t, p) = (target(), program(PHASED, "phased"));
    let cost = || ktpu_l2::passes::retile_cost(&p, &t, &Search::default()).unwrap();
    let good = cost();
    let first = under("l2.retile/first-instance", cost);
    assert!(good > 5 * first, "{good} {first}");
    let mut knob = target();
    knob.read.retile_every_stage = false;
    let off = ktpu_l2::passes::retile_cost(&p, &knob, &Search::default()).unwrap();
    assert_eq!(off, first);
}

#[test]
fn a_regular_tile_walks_one_descriptor() {
    let good = l1(SPARSE, "sparse");
    assert!(good.contains("walk=(64, 64)"), "{good}");
    let runs = under("l2.readmap/no-lattice-transfers", || l1(SPARSE, "sparse"));
    assert!(!runs.contains("walk=(64, 64)"), "{runs}");
}

#[test]
fn an_irregular_tile_fetches_the_lattice_over_it() {
    let good = l1(IRREGULAR, "irregular");
    assert!(good.matches("vfill ").count() == 1, "{good}");
    let exact = under("l2.readmap/no-lattice", || l1(IRREGULAR, "irregular"));
    assert!(exact.matches("vfill ").count() > 1, "{exact}");
}

/// The machine's target with no lattice over a tile: every run its own fill.
fn exact_words() -> Target {
    let mut t = target();
    t.read.lattice_words = 0;
    t
}

#[test]
fn a_tile_past_the_dma_queue_syncs_on_its_fills() {
    let t = exact_words();
    let pack = l1_on(IRREGULAR, "irregular", &t).unwrap();
    assert!(fills_unsynced(&pack) <= t.dma_queue_depth, "{pack}");
    let bad = under("l2.readmap/no-pack-throttle", || {
        l1_on(IRREGULAR, "irregular", &t).unwrap()
    });
    assert!(fills_unsynced(&bad) > t.dma_queue_depth, "{bad}");
    let load = l1_on(IRREGULAR_LOAD, "spaced", &t).unwrap();
    assert!(fills_unsynced(&load) <= t.dma_queue_depth, "{load}");
    let bad = under("l2.readmap/no-load-throttle", || {
        l1_on(IRREGULAR_LOAD, "spaced", &t).unwrap()
    });
    assert!(fills_unsynced(&bad) > t.dma_queue_depth, "{bad}");
}

#[test]
fn a_far_read_rewrites_its_descriptor_in_a_continuation() {
    let good = l1(FAR, "far");
    assert!(good.contains("far_n0_vc0_0__part1"), "{good}");
    assert!(good.contains("+131072"), "{good}");
    let unrebased = under("l2.readmap/no-rebase", || l1(FAR, "far"));
    assert!(!unrebased.contains("+131072"), "{unrebased}");
    let unjoined = under("l2.readmap/split-no-join", || l1(FAR, "far"));
    assert!(!unjoined.contains("vsync fe on=fill"), "{unjoined}");
    let copied = under("l2.readmap/split-copy", || l1(FAR, "far"));
    assert!(copied.contains("vmov v0, k0"), "{copied}");
    assert!(!good.contains("vmov v0, k0"), "{good}");
}

#[test]
fn a_pack_over_a_drained_slot_waits_for_the_drain() {
    let good = l1(STEADY, "steady");
    assert!(good.contains("vsync pack on=drain"), "{good}");
    let bad = under("l2.readmap/no-output-reuse-wait", || l1(STEADY, "steady"));
    assert!(!bad.contains("vsync pack on=drain"), "{bad}");
}

#[test]
fn equal_tiles_pack_once_and_drain_to_every_destination() {
    let drains = |t: &str| t.matches("vdrain ").count();
    let good = l1(QUANT, "mapped");
    let bad = under("l2.readmap/drop-fanout", || l1(QUANT, "mapped"));
    assert!(drains(&bad) < drains(&good), "{good}\n{bad}");
}

#[test]
fn a_one_lane_segment_broadcasts_its_own_lane() {
    let good = l1(QUANT, "mapped");
    assert!(good.contains("sh=3"), "{good}");
    let bad = under("l2.readmap/broadcast-phase", || l1(QUANT, "mapped"));
    assert!(!bad.contains("sh=3"), "{bad}");
}

#[test]
fn a_mapped_load_assembles_its_rows_in_registers() {
    let good = l1(DSL, "mapped_dsl");
    assert!(good.contains("vsel ") && good.contains("vadd "), "{good}");
    assert!(!good.contains("copy "), "{good}");
    let chunk = under("l2.readmap/dsl-chunk", || l1(DSL, "mapped_dsl"));
    assert_ne!(chunk, good);
    let copy = under("l2.readmap/dsl-copy-reuse", || l1(DSL, "mapped_dsl"));
    assert_ne!(copy, good);
}

#[test]
fn a_composed_pack_defers_its_status_like_a_quant() {
    let cores = target().machine.quant_cores();
    assert!(cores > 1);
    let deferred = |values: i64| {
        let quant = QUANT.replace(" overlap=1", &format!(" status=s[0 : +{values}]"));
        let quant = quant.replace(
            "    quant q",
            &format!("    s = buffer : f16[{}] @dram\n    quant q", cores * 64),
        );
        l1_on(&format!("{quant}    quant_check s\n"), "mapped", &target())
    };
    let good = deferred(cores * 64).unwrap();
    assert_eq!(good.matches("assert_zero r14").count(), cores as usize);
    let at = good.rfind("run mapped.read_status_").unwrap();
    assert_eq!(
        good[at..].matches("assert_zero r14").count(),
        cores as usize
    );
    let short = deferred((cores - 1) * 64).unwrap_err();
    assert!(short.contains("records take"), "{short}");
}

/// A stream loop of `n` multiplies, no composed read in it.
fn chain(n: usize) -> String {
    let mut text = "target ktpu.v9
fn chain.l2(x: f16[16,128] @dram, y: f16[16,128] @dram)
    par c in 0..1 on vc[c]
        for t in 0..2
            a = load x[t*8:+8, :] : f16[8,128] @l1(gt4)
            v = unpack a : f32[8,128] @reg(gt4)
            e0 = add v, 1.0 : f32[8,128] @reg(gt4)
"
    .to_string();
    for i in 1..=n {
        let e = format!(
            "            e{i} = mul e{}, 1.5 : f32[8,128] @reg(gt4)\n",
            i - 1
        );
        text += &e;
    }
    text += &format!(
        "            p = pack e{n} : f16[8,128] @l1(gt4)
            store y[t*8:+8, :] <- p
"
    );
    text
}

#[test]
fn a_plain_image_past_the_room_splits_only_by_the_knob() {
    let text = chain(160);
    let p = program(&text, "chain");
    let plain = |t: &Target| {
        ktpu_l2::lower::lower(&p, t, &Search::default()).map_err(|e| e.message.to_string())
    };
    let refused = plain(&target()).unwrap_err();
    assert!(
        refused.contains("no image of the stream loop encodes"),
        "{refused}"
    );
    let mut t = target();
    t.read.split_every_image = true;
    assert!(plain(&t).unwrap().contains("__part1"));
}

#[test]
fn the_machine_fit_answers_what_lowering_refuses() {
    let mut narrow = target();
    assert!(ktpu_l2::lower::readmap::fits_layout(&narrow));
    narrow.l1_words = 384;
    assert!(!ktpu_l2::lower::readmap::fits_layout(&narrow));
    let refused = l1_on(QUANT, "mapped", &narrow).unwrap_err();
    assert!(refused.starts_with("L2.READ.002"), "{refused}");
}
