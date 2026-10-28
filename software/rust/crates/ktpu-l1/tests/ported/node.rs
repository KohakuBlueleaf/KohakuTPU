//! `compiler/tests/test_emit_node.py`, `test_flow.py` and
//! `test_l1_coverage.py`: node statements, cluster and mover words, and
//! their refusals.

use super::*;
use ktpu_isa::unit::mover::MoverOp;
use ktpu_isa::Isa;

pub const CHECKS: &[(&str, Check)] = &[
    (
        "test_flow::test_a_fused_drain_must_name_its_emits_sink",
        a_fused_drain_must_name_its_emits_sink,
    ),
    (
        "test_flow::test_a_count_is_not_marked",
        a_count_is_not_marked,
    ),
    (
        "test_l1_coverage::test_an_image_drain_names_only_this_mesh",
        an_image_drain_names_only_this_mesh,
    ),
    (
        "test_l1_coverage::test_a_mover_count_from_a_register_is_patched",
        a_mover_count_from_a_register_is_patched,
    ),
    (
        "test_emit_node::test_marks_waits_moves_fetch (plain mover forms)",
        plain_mover_forms_are_the_framework_moves,
    ),
    (
        "test_l1_coverage::test_fill_peers_and_gemm_anchor (two peers)",
        two_fill_peers_pack_one_byte_each,
    ),
    (
        "test_emit_node::test_cluster_forms (drain ack tuple)",
        a_drain_ack_is_a_tuple,
    ),
    (
        "test_flow::test_cluster_recv_fill_credit_drain_post (recv bank, credit)",
        recv_bank_and_credit,
    ),
    (
        "test_l1_coverage::test_explicit_config_lines_and_what_the_encoder_then_skips",
        explicit_config_lines_are_used,
    ),
    (
        "test_l1_coverage::test_images_and_descriptors_name_units_on_the_target (named ack)",
        a_peer_ack_names_a_unit,
    ),
    (
        "test_emit_node::test_errors / test_fused_drain (missing cases)",
        node_refusals,
    ),
    (
        "test_l1_coverage::test_register_count_in_a_fill",
        a_fill_count_from_a_register_is_patched,
    ),
];

fn e(err: Error) -> String {
    err.to_string()
}

/// A fused drain whose sink differs from its emit's is refused.
fn a_fused_drain_must_name_its_emits_sink() -> Result<(), String> {
    eq(
        steps_err("    send mg[0]\n        gemm 1x128x1 a=A0 b=B0 acc emit to=vc[0] l1=128 count credit\n        drain n=128 to=vc[0] l1=128 fused count\n")?,
        "L1.MG.021".into(),
    )
}

/// `vwait mark` is refused: a count is not marked (the vwait forms,
/// L1.IMG.017).
fn a_count_is_not_marked() -> Result<(), String> {
    eq(
        clause(img("image k()\n    vwait mark rx n=1\n    halt\n"))?,
        "L1.IMG.017".into(),
    )
}

/// An image drain names a unit of its own mesh only.
fn an_image_drain_names_only_this_mesh() -> Result<(), String> {
    let text = "target ktpu.v9\nimage k()\n    vdrain a3 <- l1[0] to=mesh1.mg[0]\n    halt\nfn f.l1(a: f16[64] @dram)\n    node 0\n        send vc[0]\n            run k()\n";
    eq(
        clause(progs_on(&machine(), text, &[("a", 4096)], Some(2)))?,
        "L1.IMG.043".into(),
    )
}

/// The patches of a step: `[word, segments, register, constant]`.
fn patches(step: &Value) -> Vec<Value> {
    step[2].as_array().cloned().unwrap_or_default()
}

/// A gather's `n=rK` patches register K into the index count field (bits
/// [55:40] of the R_INDEX write's value; a segment is value-relative).
fn a_mover_count_from_a_register_is_patched() -> Result<(), String> {
    let got = mv("        gather a -> w(8192, (20, 32)) index=8192 n=r2 pitch=128 words=4\n")
        .map_err(e)?;
    let p: Vec<Value> = patches(&got)
        .into_iter()
        .filter(|p| p[2] == json!(2))
        .collect();
    let segs: Vec<(u64, u64)> = p
        .iter()
        .flat_map(|p| p[1].as_array().cloned().unwrap_or_default())
        .map(|s| (s[0].as_u64().unwrap(), s[1].as_u64().unwrap()))
        .collect();
    eq(segs, vec![(40, 16)])
}

/// The framework's writes of `op`, as the spec runner prints them.
fn framework(op: MoverOp) -> Result<Value, String> {
    let isa = Isa::new(&machine()).map_err(|e| e.to_string())?;
    let w = isa.mover.writes(&op).map_err(|e| e.to_string())?;
    let pairs: Vec<(u64, u64)> = w.into_iter().map(|(r, v)| (r as u64, v)).collect();
    Ok(json!(mover_writes(&pairs)))
}

/// `copy a -> b bytes=`, `quant a -> b entries=` and `gt4 a -> b groups=`
/// are the framework's moves of that size.
fn plain_mover_forms_are_the_framework_moves() -> Result<(), String> {
    let cases = [
        (
            "copy a -> 8192 bytes=4K",
            MoverOp::Copy {
                src: 4096,
                dst: 8192,
                nbytes: 4096,
            },
        ),
        (
            "quant a -> 8192 entries=4",
            MoverOp::Quantise {
                src: 4096,
                dst: 8192,
                entries: 4,
                b: false,
            },
        ),
        (
            "gt4 a -> 8192 groups=2",
            MoverOp::Transpose4 {
                src: 4096,
                dst: 8192,
                groups: 2,
            },
        ),
    ];
    for (line, op) in cases {
        let got = mv(&format!("        {line}\n")).map_err(e)?;
        eq((line, got[1].clone()), (line, framework(op)?))?;
    }
    Ok(())
}

/// Two fill peers on a die of four clusters (`multimesh_v9`): `npeer=2`,
/// one coordinate byte (`y << 4 | x`) each, the first lowest.
fn two_fill_peers_pack_one_byte_each() -> Result<(), String> {
    let m = Machine::named("multimesh_v9").map_err(|e| e.to_string())?;
    let byte = |i: usize| {
        let c = m.units(UnitKind::Mg)[i];
        (c.1 as u64) << 4 | c.0 as u64
    };
    let text = format!("{BODY}    send mg[0]\n        fill A0 <- a n=8 peers=(mg[1], mg[2])\n");
    let got = progs_on(&m, &text, &[("a", 4096)], None).map_err(e)?;
    let s = prog_steps(&kind(&m), &got[0].program);
    let peers = byte(1) | byte(2) << 8;
    has(
        &s[0][2][0],
        &["npeer=2".into(), format!("peers={peers:#x}")],
    )
}

fn has(word: &Value, fields: &[String]) -> Result<(), String> {
    let w = word.as_str().unwrap_or("");
    match fields.iter().all(|f| w.split(' ').any(|t| t == f)) {
        true => Ok(()),
        false => Err(format!("{w:?} lacks {fields:?}")),
    }
}

/// A drain's `ack=(x, y)` is written as given; `l1=` counts words.
fn a_drain_ack_is_a_tuple() -> Result<(), String> {
    let got = mg("        drain n=16 to=vc[0] l1=256 ack=(0, 1)\n").map_err(e)?;
    let addr = 256 * machine().die.word_bytes as u64;
    has(&got[0], &[format!("addr={addr:#x}"), "dack_y=1".into()])
}

/// A fill into bank 1 by receive, and `credit` on an emitting gemm and its
/// fused drain.
fn recv_bank_and_credit() -> Result<(), String> {
    let fill = mg("        fill B1 recv n=24\n").map_err(e)?[0].clone();
    let mut got: Vec<&str> = fill.as_str().unwrap_or("").split(' ').collect();
    got.sort();
    eq(
        got,
        vec![
            "FILL",
            "anchor=40",
            "fbank=1",
            "n=24",
            "nk=1",
            "recv=1",
            "sel=1",
        ],
    )?;
    let got = mg("        gemm 1x128x1 a=A0 b=B0 acc emit to=vc[0] l1=128 count credit\n        drain n=128 to=vc[0] l1=128 fused count credit\n")
        .map_err(e)?;
    has(&got[0], &["credit=1".into()])?;
    has(&got[1], &["credit=1".into()])
}

/// Written `chkvl` strides and `xb` are used: the op after them writes no
/// configuration of its own.
fn explicit_config_lines_are_used() -> Result<(), String> {
    let got = img("image k()\n    chkvl vl=64 a=(0, 1) b=(2, 0)\n    xb mode=xor k=4\n    vmul v1, v2[0:], xor(v3[2], 4) vl=64\n    ustr 2 8\n    vunpk v4 <- l1[0] f16 walk=(2, 8)\n    halt\n")
        .map_err(e)?;
    let first = got.first().cloned().unwrap_or_default();
    for f in ["vl1=63", "a_stride=1", "b_base=2"] {
        if !first.split(' ').any(|t| t == f) {
            return Err(format!("{first:?} lacks {f}"));
        }
    }
    eq(got[1].as_str(), "XB xk=4 xm=1")?;
    if !got[2].starts_with("vmul") {
        return Err(format!("a config word before the vmul: {got:?}"));
    }
    eq(got[3].as_str(), "USTR s1=8 s0=2")?;
    match got[4].starts_with("VUNPK reg=4") {
        true => Ok(()),
        false => Err(format!("{got:?}")),
    }
}

/// `ack=` in `peer()` takes a unit name as it takes its coordinates.
fn a_peer_ack_names_a_unit() -> Result<(), String> {
    let c = machine().units(UnitKind::Mg)[0];
    let desc = |ack: String| -> Result<Value, String> {
        let s = steps(&format!(
            "    send vc[0]\n        desc a3 = peer(5, ack={ack}, mesh=1) walk=(32, 1)\n"
        ))
        .map_err(e)?;
        Ok(s[0][2][0].clone())
    };
    eq(desc("mg[0]".into())?, desc(format!("({}, {})", c.0, c.1))?)
}

/// The refusals the clause tests leave out: an unknown `wait moves` name, a
/// plain copy with no size, a fused drain after a gemm that does not emit.
fn node_refusals() -> Result<(), String> {
    eq(steps_err("    wait moves nope\n")?, "L1.NODE.036".into())?;
    eq(
        steps_err("    move\n        copy a -> a\n")?,
        "L1.MOV.009".into(),
    )?;
    eq(
        steps_err(
            "    send mg[0]\n        gemm 64x64x2 a=A0 b=B0\n        drain a n=4096 fused\n",
        )?,
        "L1.MG.021".into(),
    )
}

/// `n=rK` on a fill patches register K into the FILL count field
/// ([217:202]).
fn a_fill_count_from_a_register_is_patched() -> Result<(), String> {
    let got = steps("    send mg[0]\n        fill B1 <- a n=r3 at=5\n").map_err(e)?;
    let p = &got[0][3][0];
    eq(
        (p[1].clone(), p[2].clone()),
        (json!([[202, 16, 0]]), json!(3)),
    )
}
