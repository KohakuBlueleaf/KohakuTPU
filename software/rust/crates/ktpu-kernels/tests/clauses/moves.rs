//! Moves: TG.RUN.005-.008, .013, DSL.MOV through the generators.

use super::{card, lines, text, value_ops};
use ktpu_graph as g;
use ktpu_kernels::gen::{convert, lanes, transpose};

#[test]
fn tg_run_005_move_is_one_walked_copy() {
    let k = convert::walk_kernel(
        4096,
        &[(4, 512), (3, 32)],
        64,
        2048,
        &[(12, 32)],
        0,
        &card(),
    )
    .unwrap();
    assert_eq!(
        lines(&k, "copy"),
        vec!["copy dst <- src walk=((4, 512), (3, 32)) at=64 dwalk=((12, 32),) dat=0"]
    );
    let c = convert::copy_kernel(100, &card()).unwrap();
    assert_eq!(lines(&c, "copy"), vec!["copy dst <- src"]);
    assert_eq!(c.graph.ports[0].spec.shape, vec![4, 16]);
}

#[test]
fn dsl_mov_005_fill_kernel() {
    let k = convert::fill_kernel(1024, &[(4, 128), (2, 32)], 32, 0x3c00, 16, &card()).unwrap();
    assert_eq!(
        lines(&k, "fill"),
        vec!["fill dst value=15360 ew=16 walk=((4, 128), (2, 32)) at=32"]
    );
}

#[test]
fn tg_run_006_transpose_lane_swaps() {
    let k = transpose::transpose_kernel(2, 32, 48, &card()).unwrap();
    assert_eq!(
        value_ops(&k)
            .into_iter()
            .filter(|o| matches!(*o, "xlane" | "sel"))
            .collect::<Vec<_>>(),
        vec!["xlane", "sel", "xlane", "sel"]
    );
    let t = text(&k);
    assert!(t.contains("xlane v3, 5.0") && t.contains(", 10.0"), "{t}");
    assert_eq!(lines(&k, "gt4").len(), 2);
    let err = transpose::transpose_kernel(1, 20, 32, &card()).unwrap_err();
    assert_eq!(err.clause, "TG.RUN.006");
    let m = transpose::masks(&card()).unwrap();
    assert_eq!(m.len(), 256);
    // Lane 1: bits 0 and 2 differ, bits 1 and 3 do not.
    assert_eq!((m[1], m[128 + 1]), (0x3c00, 0));
}

/// DSL.KERN.016's facts: the generated reverse and lanes kernels' name
/// parts (prefix, a 10-digit digest) and sorted ports.
pub fn kern_016_facts() -> Vec<(String, usize, Vec<String>)> {
    let rk = transpose::reverse_kernel(4096, &[(64, 64), (2, -32)], 32, 2048, &card()).unwrap();
    let pl = lanes::plan(32, &[(32, 1)], 0, &[(32, 1)], 0, 32, false, &card()).unwrap();
    let lk = lanes::relayout_kernel(&pl, &card()).unwrap();
    [rk, lk]
        .iter()
        .map(|k| {
            let parts: Vec<&str> = k.graph.name.split('_').collect();
            let mut ports: Vec<String> = k.graph.ports.iter().map(|p| p.name.clone()).collect();
            ports.sort();
            (parts[0].to_string(), parts.len(), ports)
        })
        .collect()
}

#[test]
fn dsl_kern_016_generated_kernels_named() {
    let s = |v: &[&str]| v.iter().map(|x| x.to_string()).collect::<Vec<_>>();
    assert_eq!(
        kern_016_facts(),
        vec![
            ("reverse".into(), 2, s(&["x", "y"])),
            ("lanes".into(), 2, s(&["dst", "index", "mask", "src"])),
        ]
    );
}

#[test]
fn tg_run_007_flip_reverses_lanes() {
    let k = transpose::reverse_kernel(4096, &[(8, 256), (8, -32)], 224, 1024, &card()).unwrap();
    assert!(text(&k).contains(", 15.0"), "{}", text(&k));
    assert_eq!(
        lines(&k, "copy"),
        vec!["copy h1 <- x walk=((8, 256), (8, -32)) at=224 dwalk=((64, 32),)"]
    );
}

#[test]
fn tg_run_008_gather_by_lane_relayout() {
    // A 5 x 7 transpose: every destination word reads taps of rotations.
    let pl = lanes::plan(
        35,
        &[(5, 1), (7, 5)],
        0,
        &[(5, 7), (7, 1)],
        0,
        35,
        false,
        &card(),
    )
    .unwrap();
    assert_eq!(pl.dst_words, 3);
    assert!(pl.fill);
    let k = lanes::relayout_kernel(&pl, &card()).unwrap();
    let gathers = lines(&k, "gather");
    assert!(!gathers.is_empty() && gathers.iter().all(|l| l.contains(" fp index=")));
    assert!(lines(&k, "scatter")
        .iter()
        .all(|l| l.contains(" fp index=")));
    assert!(value_ops(&k).contains(&"xrot"));
    let index = k.graph.ports.iter().find(|p| p.name == "index").unwrap();
    assert_eq!(index.spec.dtype, g::Dtype::F32);
    let err = lanes::plan(10, &[(11, 1)], 0, &[(11, 1)], 0, 11, false, &card()).unwrap_err();
    assert_eq!(err.clause, "L2.REL.005");
}

#[test]
fn tg_run_013_cast_is_a_flat_pass() {
    let k = convert::cast_kernel(300, g::Dtype::F32, g::Dtype::F16, &card()).unwrap();
    let dts: Vec<_> = k.graph.ports.iter().map(|p| p.spec.dtype).collect();
    assert_eq!(dts, vec![g::Dtype::F32, g::Dtype::F16]);
    assert_eq!(k.graph.ports[0].spec.shape, vec![38, 8]);
    assert_eq!(k.graph.ports[1].spec.shape, vec![19, 16]);
}
