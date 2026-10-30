//! `compiler/tests/test_emit_image.py` and `test_vregs.py`: what the image
//! encoder writes for a text, against the words the Python encoder gave for
//! the same text.

use super::*;
use ktpu_l1::lower::image::{reset, CoreFacts};
use ktpu_l1::lower::imem::{rewrite, Resident};

pub const CHECKS: &[(&str, Check)] = &[
    (
        "test_emit_image::test_unknown_entry_sets_the_loop_state_once",
        unknown_entry_sets_the_loop_state_once,
    ),
    (
        "test_emit_image::test_operand_fields_follow_the_op",
        operand_fields_follow_the_op,
    ),
    (
        "test_emit_image::test_config_words_only_on_change",
        config_words_only_on_change,
    ),
    (
        "test_emit_image::test_free_fields_keep_the_state",
        free_fields_keep_the_state,
    ),
    (
        "test_emit_image::test_crossbar_on_c_reads_b",
        crossbar_on_c_reads_b,
    ),
    (
        "test_emit_image::test_walks_dma_and_sync",
        walks_dma_and_sync,
    ),
    (
        "test_emit_image::test_errors_name_the_line",
        errors_name_the_line,
    ),
    (
        "test_vregs::test_target_line_reads_and_writes_vregs",
        target_line_reads_and_writes_vregs,
    ),
    (
        "test_vregs::test_image_encodes_for_the_target_core",
        image_encodes_for_the_target_core,
    ),
    (
        "test_vregs::test_resident_imem_rebase_keeps_the_word_past_bit_32",
        resident_rebase_keeps_the_word_past_bit_32,
    ),
    (
        "skipz::a_long_skip_runs_as_consecutive_skips",
        a_long_skip_runs_as_consecutive_skips,
    ),
];

/// An add that sets the configuration, `getstk s1`, then `skipz s1` over `n`
/// adds of one word each (`inner` first in the block).
fn skip_text(n: usize, inner: &str) -> String {
    let body: String = (0..n).map(|_| "        vadd v1, v1, v2\n").collect();
    format!("image k()\n    vadd v1, v1, v2\n    getstk s1\n    skipz s1\n{inner}{body}    halt\n")
}

/// A block past VSKIPZ's 8-bit skip runs as consecutive skips on its register,
/// each within the field, the words between them the block's (L1.IMG.027):
/// 255 words take one VSKIPZ, 256 take two. A block that writes the register
/// stays refused.
fn a_long_skip_runs_as_consecutive_skips() -> Result<(), String> {
    for (n, parts) in [(255, 1), (256, 2), (300, 2), (511, 3)] {
        let got = words(&skip_text(n, ""))?;
        let heads: Vec<usize> = got
            .iter()
            .enumerate()
            .filter(|(_, w)| w.starts_with("VSKIPZ"))
            .map(|(i, _)| i)
            .collect();
        let skip = |i: usize| -> usize { got[i].rsplit("skip=").next().unwrap().parse().unwrap() };
        eq(heads.len(), parts)?;
        for (k, &h) in heads.iter().enumerate() {
            eq(got[h].contains("sreg=1"), true)?;
            let next = heads.get(k + 1).copied().unwrap_or(got.len() - 1);
            eq(h + 1 + skip(h), next)?;
        }
        eq(got[got.len() - 1].as_str(), "VHALT")?;
        eq(got.iter().filter(|w| w.starts_with("vadd")).count(), n + 1)?;
    }
    if words(&skip_text(300, "        getstk s1\n")).is_ok() {
        return Err("a block that writes its register was split".into());
    }
    Ok(())
}

const STREAM: &str = "image k(n)\n    seti s5 = n\n    loop s5\n        vunpk v0 <- l1[0] f16\n        vmul v1, v0, v0\n        vpack v1 -> l1[64] f16\n    halt\n";

/// The loop state an unknown entry needs is set once before VLOOP; the body
/// is the reset entry's.
fn unknown_entry_sets_the_loop_state_once() -> Result<(), String> {
    let m = machine();
    let module = read(&format!("target ktpu.v9\n{STREAM}"), "<t>").map_err(|e| e.to_string())?;
    let at = |entry| {
        img::image(&module, &m, "k", &[8], entry)
            .map(|(w, _)| core_words(&w))
            .map_err(|e| e.to_string())
    };
    let vlmax = CoreFacts::of(&m, module.vregs).vlmax;
    let (warm, cold) = (at(reset(vlmax))?, at(UNKNOWN)?);
    let want: Vec<&str> = vec![
        "VSETI sreg=5",
        "imm=0x8",
        "VLOOP sreg=5 body=3",
        "VUNPK n1=7",
        "vmul vd=1",
        "VPACK reg=1 n1=7 off=64",
        "VHALT",
    ];
    eq(
        warm.iter().map(String::as_str).collect::<Vec<_>>(),
        want.clone(),
    )?;
    let lp = 2;
    let mut set: Vec<&str> = cold[lp..lp + 3].iter().map(String::as_str).collect();
    set.sort();
    eq(
        set,
        vec![
            "CHKVL vl1=127 d_stride=1 c_stride=1 b_stride=1 a_stride=1",
            "PSTR s1=4 s0=1",
            "USTR s1=4 s0=1",
        ],
    )?;
    eq(&cold[..lp], &warm[..lp])?;
    eq(&cold[lp + 3..], &warm[lp..])
}

fn words(text: &str) -> Result<Vec<String>, String> {
    img(text).map_err(|e| e.to_string())
}

/// Each op's scalar / constant operand selects its own field: a constant on
/// `a` is `sa=3`.
fn operand_fields_follow_the_op() -> Result<(), String> {
    eq(
        words("image k()\n    vmul v16, v0, s0\n    vadd v16, v16, k1\n    vfma v1, v2, v3, v4\n    vexp2d v5, k0, v6\n    halt\n")?,
        [
            "CHKVL vl1=127 d_stride=1 c_stride=1 b_stride=1 a_stride=1",
            "vmul vd=16 sb=1",
            "vadd vd=16 va=16 vc=1 sc=3",
            "vfma vd=1 va=2 vb=3 vc=4",
            "vexp2d vd=5 vb=6 sa=3",
            "VHALT",
        ]
        .map(String::from)
        .to_vec(),
    )
}

/// CHKVL and XB are written only where the chunk state changes.
fn config_words_only_on_change() -> Result<(), String> {
    eq(
        words("image k()\n    vmax v1[4:], v2[0:], v2[4:] vl=64\n    vmax v1[4:], v2[0:], v2[4:] vl=64\n    vmax v1[0:], v3[0:], v3[4:] vl=64\n    vmul v9, v9, s0 vl=64\n    halt\n")?,
        [
            "CHKVL vl1=63 d_base=4 d_stride=1 c_stride=1 b_base=4 b_stride=1 a_stride=1",
            "XB",
            "vmax vd=1 va=2 vb=2 om=1",
            "vmax vd=1 va=2 vb=2 om=1",
            "CHKVL vl1=63 d_stride=1 c_stride=1 b_base=4 b_stride=1 a_stride=1",
            "vmax vd=1 va=3 vb=3 om=1",
            "vmul vd=9 va=9 sb=1",
            "VHALT",
        ]
        .map(String::from)
        .to_vec(),
    )
}

/// VMAX reads no `c`: its CHKVL keeps the `c` chunk the op before set.
fn free_fields_keep_the_state() -> Result<(), String> {
    eq(
        words("image k()\n    vadd v1[0:], v2[0:], v2[4:] vl=64\n    vmax v1[0:], v2[0:], v2[4:] vl=64\n    halt\n")?,
        [
            "CHKVL vl1=63 d_stride=1 c_base=4 c_stride=1 b_stride=1 a_stride=1",
            "XB",
            "vadd vd=1 va=2 vc=2 om=1",
            "CHKVL vl1=63 d_stride=1 c_base=4 c_stride=1 b_base=4 b_stride=1 a_stride=1",
            "vmax vd=1 va=2 vb=2 om=1",
            "VHALT",
        ]
        .map(String::from)
        .to_vec(),
    )
}

/// A crossbar on `c` reads it through `b`: the crossed operand's chunk is
/// `b_base`.
fn crossbar_on_c_reads_b() -> Result<(), String> {
    eq(
        words("image k()\n    vadd v21, v20, merge(v20[4:], 4) vl=64\n    vsub v3, v3, bcast(v22[2], lane=5, sh=3)\n    halt\n")?,
        [
            "CHKVL vl1=63 d_stride=1 c_stride=1 b_base=4 b_stride=1 a_stride=1",
            "XB xc=1 xk=4 xm=4",
            "vadd vd=21 va=20 vb=20 vc=20 om=1",
            "CHKVL vl1=127 d_stride=1 c_stride=1 b_base=2 a_stride=1",
            "XB xc=1 sh=3 xk=5 xm=3",
            "vsub vd=3 va=3 vb=22 vc=22 om=1",
            "VHALT",
        ]
        .map(String::from)
        .to_vec(),
    )
}

/// Walks, DMA and sync on a dual-queue core: the stale-AINC zeroing, USTR /
/// PSTR walks, gt4 and mx7b, fill / drain to a peer, and `vmark on=pack1`.
fn walks_dma_and_sync() -> Result<(), String> {
    let got = img_on(&dualq_machine(), "image k()\n    vunpk v4[2:] <- l1[96] f16 n=2 rel\n    vunpk v5 <- l1[8] gt4 walk=(1, 16) q=1\n    vpack v6 -> l1[256] mx7b\n    vfill a0 -> l1[0] rel\n    vdrain a2 <- l1[256] rel to=(1, 2) buf=1 signal\n    vsync drain mark=m5\n    vmark m5 on=pack1 slack=1\n    halt\n")
        .map_err(|e| e.to_string())?;
    eq(
        got,
        [
            "AINC",
            "AINC ad=2",
            "USTR s1=4 s0=1",
            "VUNPK reg=4 n1=1 cb=2 rel=1 off=96",
            "USTR s1=16 s0=1",
            "VUNPK reg=5 mode=1 n1=7 q=1 off=8",
            "PSTR s1=4 s0=1",
            "VPACK reg=6 mode=3 n1=7 cb=1 off=256",
            "VFILL rel=1",
            "VDRAIN rel=1 signal=1 node=1 ad=2 dst_x=1 dst_y=2 buf_id=1 off=256",
            "VSYNC waiter=6 marked=1 mark=5",
            "VMARK waiter=5 on=6 slack=1 mark=5",
            "VHALT",
        ]
        .map(String::from)
        .to_vec(),
    )
}

/// Each refusal names its clause and the statement's line (the text's line
/// 1 is `target`).
fn errors_name_the_line() -> Result<(), String> {
    let cases = [
        ("vmul v32, v0, v1", "L1.IMG.008", 3),
        ("vadd v1, v2", "L1.IMG.034", 3),
        ("vsync pack", "L1.IMG.044", 3),
        ("vexp2d v1, xor(v2, 1), v3", "L1.IMG.037", 3),
        ("vfoo v1", "L1.IMG.034", 3),
        (
            "loop s1\n        loop s2\n            halt",
            "L1.IMG.050",
            4,
        ),
    ];
    for (line, want, at) in cases {
        match img(&format!("image k()\n    {line}\n")) {
            Ok(w) => return Err(format!("{line}: encoded {w:?}")),
            Err(e) => eq((line, e.clause.as_str(), e.line), (line, want, at))?,
        }
    }
    Ok(())
}

/// `vregs=` is read and written back; the default line is bare.
fn target_line_reads_and_writes_vregs() -> Result<(), String> {
    let m = read("target ktpu.v9 vregs=128\n", "<t>").map_err(|e| e.to_string())?;
    eq(
        (m.vregs, m.target_line()),
        (128, "target ktpu.v9 vregs=128".to_string()),
    )
}

/// A register past the target core's file is refused; the wide target
/// encodes it with its high bits.
fn image_encodes_for_the_target_core() -> Result<(), String> {
    let text = "image k()\n    vmul v100, v65, v3\n    halt\n";
    let m = machine();
    let narrow = read(&format!("target ktpu.v9\n\n{text}"), "<t>").map_err(|e| e.to_string())?;
    eq(
        clause(img::image(&narrow, &m, "k", &[], UNKNOWN))?,
        "L1.IMG.008".into(),
    )?;
    let wide =
        read(&format!("target ktpu.v9 vregs=128\n\n{text}"), "<t>").map_err(|e| e.to_string())?;
    let (w, _) = img::image(&wide, &m, "k", &[], UNKNOWN).map_err(|e| e.to_string())?;
    let got = core_words(&w);
    eq(got[got.len() - 2].as_str(), "vmul vd=4 va=1 vb=3 hi=0xe0")
}

/// A resident IMEM rewrite keeps a word's bits past 32 (a 128-register
/// core's high register bits).
fn resident_rebase_keeps_the_word_past_bit_32() -> Result<(), String> {
    let m = machine();
    let module = read(
        "target ktpu.v9 vregs=128\n\nimage k()\n    vfma v100, v65, v3, v127\n    halt\n",
        "<t>",
    )
    .map_err(|e| e.to_string())?;
    let (words, _) = img::image(&module, &m, "k", &[], UNKNOWN).map_err(|e| e.to_string())?;
    if !words.iter().any(|w| w >> 32 != 0) {
        return Err("no word past bit 32".into());
    }
    let isa = img::isa_for(&m, 128).map_err(|e| e.to_string())?;
    let mut flits: Vec<ktpu_isa::W256> = words
        .iter()
        .enumerate()
        .map(|(i, &word)| {
            ktpu_isa::unit::vector::VecCu::Imem {
                addr: i as u32,
                word,
            }
            .encode(&isa)
        })
        .collect::<Result<_, _>>()
        .map_err(|e| e.to_string())?;
    flits.push(
        ktpu_isa::unit::vector::VecCu::Run { pc: 0 }
            .encode(&isa)
            .map_err(|e| e.to_string())?,
    );
    let mut r = Resident::new(m.vc.imem_words, m.vc.run_reach);
    let out = rewrite(&isa, &flits, &mut r).map_err(|e| e.to_string())?;
    // The CU op is the payload's top nibble (ISA.VC.030): 1 is IMEM.
    let op = isa.cfg.payload_bits - 4;
    let back: Vec<u64> = out
        .iter()
        .filter(|f| f.get(op, 4) == 1)
        .map(|f| isa.imem_word(*f))
        .collect();
    eq(back, words)
}
