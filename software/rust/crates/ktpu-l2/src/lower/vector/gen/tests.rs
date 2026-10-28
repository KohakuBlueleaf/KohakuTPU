//! Constructor fixtures against the generator's literal spelling and L1 parser.

use ktpu_l1::text::{parse, read, Stmt, Term};

use super::print::{self, Rule};
use super::typed::*;

fn n(s: &str) -> Term {
    Term::name(s)
}
fn i(v: i128) -> Term {
    Term::int(v)
}
fn r(s: &str, b: u64) -> Term {
    reg(s, Select::Stride(b))
}

fn fixture(stmts: Vec<Stmt>, literal: &str) {
    let mut stmts = stmts;
    assert_eq!(
        print::emit(&mut stmts).unwrap().as_bytes(),
        literal.as_bytes()
    );
    assert_eq!(stmts, parse(literal, "<fixture>").unwrap());
}

#[test]
fn register_selects() {
    // tree.rs tree4: "vmov {0}[4:], {0}[0:] vl=64"; math.rs: "vmov {0}, k0".
    fixture(
        vec![math(
            "vmov",
            r("v0", 4),
            vec![r("v0", 0)],
            vec![kw("vl", i(64))],
        )],
        "vmov v0[4:], v0[0:] vl=64\n",
    );
    fixture(
        vec![math(
            "vmov",
            reg("v0", Select::Whole),
            vec![n("k0")],
            vec![],
        )],
        "vmov v0, k0\n",
    );
    assert_eq!(
        reg("v1", Select::Chunk(0)),
        Term::View("v1".into(), vec![i(0)])
    );
}

#[test]
fn math_arities() {
    // ctx/math.rs emits "{code} {0}, ..."; tree.rs emits the fused vfma form.
    for (st, text) in [
        (
            math("vexp2", n("v0"), vec![n("v1")], vec![]),
            "vexp2 v0, v1\n",
        ),
        (
            math("vadd", n("v0"), vec![n("v1"), n("v2")], vec![]),
            "vadd v0, v1, v2\n",
        ),
        (
            math("vfma", n("v0"), vec![n("v1"), n("v2"), n("v0")], vec![]),
            "vfma v0, v1, v2, v0\n",
        ),
        (
            math("vsel", n("v0"), vec![n("v2"), n("v3"), n("v1")], vec![]),
            "vsel v0, v2, v3, v1\n",
        ),
    ] {
        fixture(vec![st], text);
    }
}

#[test]
fn compares_and_predicated_moves() {
    // ctx/math.rs: "{code} p1, ...", "vmov {0}, k0", "vmov {0}, k1 if=p1".
    for (op, text) in [
        ("vcmplt", "vcmplt p1, v1, v2\n"),
        ("vcmpgt", "vcmpgt p1, v1, v2\n"),
        ("vcmpeq", "vcmpeq p1, v1, v2\n"),
    ] {
        fixture(
            vec![math(op, n("p1"), vec![n("v1"), n("v2")], vec![])],
            text,
        );
    }
    fixture(
        vec![
            math("vmov", n("v0"), vec![n("k0")], vec![]),
            math("vmov", n("v0"), vec![n("k1")], vec![kw("if", n("p1"))]),
        ],
        "vmov v0, k0\nvmov v0, k1 if=p1\n",
    );
}

#[test]
fn crossbar_calls() {
    // ctx/math.rs xlane, tree.rs merge tree, ctx/unpack.rs unpack_stat.
    for (mode, literal) in [
        ("xor", "vmul v0, k1, xor(v1, 8)\n"),
        ("rot", "vmul v0, k1, rot(v1, 8)\n"),
    ] {
        fixture(
            vec![math(
                "vmul",
                n("v0"),
                vec![n("k1"), crossbar(mode, n("v1"), 8)],
                vec![],
            )],
            literal,
        );
    }
    fixture(
        vec![math(
            "vadd",
            n("v0"),
            vec![n("v1"), crossbar("merge", n("v1"), 8)],
            vec![],
        )],
        "vadd v0, v1, merge(v1, 8)\n",
    );
    fixture(
        vec![math(
            "vmul",
            n("v0"),
            vec![n("k1"), bcast(reg("v1", Select::Chunk(0)), 3, 3)],
            vec![],
        )],
        "vmul v0, k1, bcast(v1[0], lane=3, sh=3)\n",
    );
}

#[test]
fn pack_unpack_modes() {
    // ctx.rs pack_line/unpack_line; ctx/pack.rs MX7 and statistic packs.
    for (kind, mode, literal) in [
        (Memory::Unpack, "f16", "vunpk v0 <- l1[8] f16\n"),
        (Memory::Unpack, "gt4", "vunpk v0 <- l1[8] gt4\n"),
        (Memory::Unpack, "f32", "vunpk v0 <- l1[8] f32\n"),
        (Memory::Pack, "f16", "vpack v0 -> l1[8] f16\n"),
        (Memory::Pack, "f32", "vpack v0 -> l1[8] f32\n"),
        (Memory::Pack, "gt4", "vpack v0 -> l1[8] gt4\n"),
        (Memory::Pack, "mx7a", "vpack v0 -> l1[8] mx7a\n"),
        (Memory::Pack, "mx7b", "vpack v0 -> l1[8] mx7b\n"),
    ] {
        fixture(
            vec![memory(kind, n("v0"), 8, Some(mode), false, vec![]).unwrap()],
            literal,
        );
    }
    fixture(
        vec![memory(Memory::Unpack, n("v0"), 0, Some("f16"), true, vec![]).unwrap()],
        "vunpk v0 <- l1[0] f16 rel\n",
    );
    fixture(
        vec![memory(
            Memory::Pack,
            n("v0"),
            8,
            Some("f16"),
            false,
            vec![kw("walk", pair(16, 1))],
        )
        .unwrap()],
        "vpack v0 -> l1[8] f16 walk=(16, 1)\n",
    );
    fixture(
        vec![memory(
            Memory::Unpack,
            n("v0"),
            8,
            Some("f16"),
            false,
            vec![kw("n", i(1))],
        )
        .unwrap()],
        "vunpk v0 <- l1[8] f16 n=1\n",
    );
    fixture(
        vec![memory(
            Memory::Pack,
            n("v0"),
            8,
            Some("f16"),
            false,
            vec![kw("n", i(1))],
        )
        .unwrap()],
        "vpack v0 -> l1[8] f16 n=1\n",
    );
}

#[test]
fn scalar_constants() {
    // ctx/emit.rs constants/constant: "seti {reg} = {}" via py_float; band.rs loop count.
    fixture(vec![seti("s0", i(2))], "seti s0 = 2\n");
    for (v, literal) in [
        (1.5, "seti s0 = 1.5\n"),
        (-1.5, "seti s0 = -1.5\n"),
        (0.0, "seti s0 = 0.0\n"),
        (-0.0, "seti s0 = -0.0\n"),
        (1e-5, "seti s0 = 1e-05\n"),
    ] {
        fixture(vec![seti("s0", float(v).unwrap())], literal);
    }
    assert!(
        matches!(float(-0.0).unwrap(), Term::Neg(v) if matches!(*v, Term::Float(f) if f.to_bits() == 0.0f64.to_bits()))
    );
}

#[test]
fn nested_block_events() {
    // ctx/band.rs loop + braces; ctx/emit.rs sticky skipz + braces; mod.rs indent.
    let loop_head = instruction("loop", vec![n("s0")], vec![]);
    let skip_head = instruction("skipz", vec![n("s1")], vec![]);
    let events = vec![
        Event::Fixed(loop_head),
        Event::Open,
        Event::Fixed(skip_head),
        Event::Open,
        Event::Fixed(instruction("vbar", vec![], vec![])),
        Event::Close,
        Event::Close,
    ];
    assert!(events[1].is_open());
    assert!(events[5].is_block_edge());
    let body = nest(&events).unwrap();
    assert_eq!(
        body,
        vec![block(
            "loop",
            "s0",
            vec![block("skipz", "s1", vec![instruction("vbar", vec![], vec![])]).unwrap()]
        )
        .unwrap()]
    );
    fixture(body, "loop s0\n    skipz s1\n        vbar\n");
}

#[test]
fn control_instructions() {
    // ctx/band.rs and ctx/emit.rs raw control lines.
    fixture(
        vec![
            instruction("getstk", vec![n("s0")], vec![]),
            instruction("uptr", vec![i(8)], vec![]),
            instruction("uinc", vec![i(8)], vec![]),
            instruction("halt", vec![], vec![]),
            instruction("vbar", vec![], vec![]),
        ],
        "getstk s0\nuptr 8\nuinc 8\nhalt\nvbar\n",
    );
}

#[test]
fn dma_instructions() {
    // ctx/emit.rs prologue/iteration and modulo/emit.rs fill/drain.
    fixture(
        vec![memory(Memory::Fill, n("a0"), 8, None, false, vec![]).unwrap()],
        "vfill a0 -> l1[8]\n",
    );
    fixture(
        vec![memory(Memory::Fill, n("a0"), 8, None, true, vec![]).unwrap()],
        "vfill a0 -> l1[8] rel\n",
    );
    fixture(
        vec![memory(
            Memory::Drain,
            n("a0"),
            8,
            None,
            true,
            vec![kw("to", n("vc0")), kw("buf", i(1))],
        )
        .unwrap()],
        "vdrain a0 <- l1[8] rel to=vc0 buf=1\n",
    );
}

#[test]
fn synchronization() {
    // ctx/emit.rs sync templates; modulo/emit.rs put: "vsync {w} on={on} slack={s}".
    fixture(vec![sync("fe", "unpack", None)], "vsync fe on=unpack\n");
    fixture(
        vec![sync("pack", "drain", Some(i(2)))],
        "vsync pack on=drain slack=2\n",
    );
    fixture(
        vec![sync("pack", "drain", Some(n("None")))],
        "vsync pack on=drain slack=None\n",
    );
}

#[test]
fn wait_and_post() {
    // ctx/emit.rs: "vwait unpack rx n={}", "vwait drain cr n={}", "vpost {} n={}".
    fixture(
        vec![
            wait("unpack", "rx", 8),
            wait("drain", "cr", 8),
            post("vc0", 8),
        ],
        "vwait unpack rx n=8\nvwait drain cr n=8\nvpost vc0 n=8\n",
    );
}

#[test]
fn descriptor_walks() {
    // ctx/band.rs image_text: "desc a{} = {base} walk=({})"; peer count is optional.
    fixture(
        vec![desc("a0", n("p_a0"), &[(32, 8)]).unwrap()],
        "desc a0 = p_a0 walk=(32, 8)\n",
    );
    fixture(
        vec![desc("a0", peer(0, true), &[(32, 8), (256, 2)]).unwrap()],
        "desc a0 = peer(0, count=1) walk=((32, 8), (256, 2))\n",
    );
    fixture(
        vec![desc("a0", peer(0, false), &[(-32, 8)]).unwrap()],
        "desc a0 = peer(0) walk=(-32, 8)\n",
    );
}

#[test]
fn descriptor_increment() {
    // ctx/band.rs image_text and search.rs modulo: "ainc a{} += {}".
    fixture(
        vec![ainc("a0", 32), ainc("a1", -32)],
        "ainc a0 += 32\nainc a1 += -32\n",
    );
}

#[test]
fn image_module() {
    // ctx/band.rs image_text: image header, descriptors, then indented assigned lines.
    let body = vec![
        desc("a0", n("p_a0"), &[(32, 8)]).unwrap(),
        seti("s0", i(2)),
        block("loop", "s0", vec![instruction("vbar", vec![], vec![])]).unwrap(),
        instruction("halt", vec![], vec![]),
    ];
    let literal = "image k(p_a0)\n    desc a0 = p_a0 walk=(32, 8)\n    seti s0 = 2\n    loop s0\n        vbar\n    halt\n";
    let got = print::image("k", vec!["p_a0".into()], body, 64).unwrap();
    assert_eq!(got.source.as_bytes(), literal.as_bytes());
    let mut expected = read(literal, "<k>").unwrap();
    expected.vregs = 64;
    assert_eq!(got, expected);
    assert_eq!(
        (
            got.images["k"].stmts[2].body[0].line,
            got.images["k"].stmts[2].body[0].col
        ),
        (5, 9)
    );
    let empty_params =
        print::image("k", vec![], vec![instruction("halt", vec![], vec![])], 32).unwrap();
    assert_eq!(empty_params, read("image k()\n    halt\n", "<k>").unwrap());
}

#[test]
fn dotted_image_name() {
    // ctx/band.rs image_text interpolates the caller's name unchanged.
    let literal = "image foo.bar()\n    halt\n";
    let module = print::image(
        "foo.bar",
        vec![],
        vec![instruction("halt", vec![], vec![])],
        32,
    )
    .unwrap();
    assert_eq!(module.source.as_bytes(), literal.as_bytes());
    assert_eq!(module, read(literal, "<foo.bar>").unwrap());
    for bad in ["foo..bar", ".foo", "foo.", "foo.1", "%1"] {
        assert!(print::image(bad, vec![], vec![instruction("halt", vec![], vec![])], 32).is_err());
    }
}

#[test]
fn symbolic_metadata_is_explicit() {
    let symbols = vec![Symbol(1), Symbol(2), Symbol(2)];
    let stmt = math(
        "vcmplt",
        n("p1"),
        vec![
            Symbol(2).operand(Select::Whole),
            Symbol(2).operand(Select::Stride(0)),
        ],
        vec![],
    );
    let event = Event::Op {
        stmt: stmt.clone(),
        syms: symbols.clone(),
    };
    assert!(matches!(&event, Event::Op { syms, .. } if syms == &symbols));
    assert_eq!(nest(&[event]).unwrap(), vec![stmt.clone()]);
    assert!(print::emit(&mut [stmt]).is_err());
}

#[test]
fn unsupported_shapes_are_refused() {
    assert!(walk(&[]).is_err());
    assert!(float(f64::NAN).is_err());
    assert!(float(f64::INFINITY).is_err());
    assert!(memory(Memory::Fill, n("a0"), 0, Some("f16"), false, vec![]).is_err());
    assert!(memory(Memory::Unpack, n("v0"), 0, None, false, vec![]).is_err());
    assert!(nest(&[Event::Close]).is_err());
    assert!(nest(&[Event::Open]).is_err());
    assert!(nest(&[
        Event::Fixed(instruction("loop", vec![n("s0")], vec![])),
        Event::Open
    ])
    .is_err());
    assert!(block("halt", "s0", vec![instruction("vbar", vec![], vec![])]).is_err());
    assert!(print::image("k", vec![], vec![], 32).is_err());
    assert!(
        print::emit(&mut [instruction("vmov", vec![Term::Str("raw".into())], vec![])]).is_err()
    );
}

fn killed(st: Stmt, literal: &str, rule: Rule) {
    fixture(vec![st.clone()], literal);
    let changed = print::emit_rule(&mut [st], rule).unwrap();
    let failed = std::panic::catch_unwind(|| assert_eq!(changed.as_bytes(), literal.as_bytes()));
    assert!(failed.is_err(), "printer mutation survived");
}

#[test]
fn compact_slice_mutation_is_killed() {
    killed(
        math("vmov", r("v0", 4), vec![r("v0", 0)], vec![kw("vl", i(64))]),
        "vmov v0[4:], v0[0:] vl=64\n",
        Rule::SpacedSlice,
    );
}

#[test]
fn spaced_binding_mutation_is_killed() {
    killed(
        desc("a0", n("p_a0"), &[(32, 8)]).unwrap(),
        "desc a0 = p_a0 walk=(32, 8)\n",
        Rule::TightBinding,
    );
}

#[test]
fn keyword_order_mutation_is_killed() {
    killed(
        sync("pack", "drain", Some(i(2))),
        "vsync pack on=drain slack=2\n",
        Rule::ReverseKeywords,
    );
}
