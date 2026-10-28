use super::*;

#[test]
fn modulo_typed_header_pins_and_source_maps() {
    use crate::lower::vector::{Desc, Walk};
    let descs = vec![
        Desc {
            ad: 7,
            inc: -32,
            walk: Walk(vec![(32, 8), (512, 2)]),
            ..Default::default()
        },
        Desc {
            ad: 4,
            inc: 64,
            walk: Walk::flat(32, 4),
            ..Default::default()
        },
    ];
    let head = vec![
        Event::Fixed(ir::seti("s1", ir::float(-0.0).unwrap())),
        Event::Op {
            stmt: ir::math(
                "vmov",
                ir::Symbol(8).operand(ir::Select::Stride(0)),
                vec![Term::name("s1")],
                vec![],
            ),
            syms: vec![ir::Symbol(8)],
        },
    ];
    let tail = vec![
        Event::Fixed(ir::seti("s15", Term::int(3))),
        Event::Fixed(ir::instruction("loop", vec![Term::name("s15")], vec![])),
        Event::Open,
        Event::Fixed(ir::sync("pack", "drain", Some(Term::int(63)))),
        Event::Close,
    ];
    let got = image(
        &descs,
        32,
        "k.modulo",
        &head,
        &tail,
        &HashMap::from([("%8".into(), 2)]),
    );
    let text = "image k.modulo(p_a7, p_a4)\n    desc a7 = p_a7 walk=((32, 8), (512, 2))\n    desc a4 = p_a4 walk=(32, 4)\n    ainc a7 += -32\n    ainc a4 += 64\n    seti s1 = -0.0\n    vmov v2[0:], s1\n    seti s15 = 3\n    loop s15\n        vsync pack on=drain slack=63\n    halt\n";
    assert_eq!(got.source, text);
    assert_eq!(
        format!("{got:?}"),
        format!("{:?}", encode::read_image(text, "k.modulo", 32).unwrap())
    );
    assert_eq!(got.images["k.modulo"].stmts[7].body[0].line, 10);
    assert_eq!(got.images["k.modulo"].stmts[7].body[0].col, 9);
}

#[test]
fn modulo_typed_rebuilt_statement_domain() {
    let fixtures = [
        ("vbar", ir::instruction("vbar", vec![], vec![]), true),
        ("vsync pack on=drain", ir::sync("pack", "drain", None), true),
        (
            "vsync drain on=pack slack=3",
            ir::sync("drain", "pack", Some(Term::int(3))),
            true,
        ),
        (
            "vsync pack on=unpack",
            ir::sync("pack", "unpack", None),
            false,
        ),
        ("vsync fe on=unpack", ir::sync("fe", "unpack", None), false),
        (
            "vdrain a7 <- l1[32] rel",
            ir::memory(ir::Memory::Drain, Term::name("a7"), 32, None, true, vec![]).unwrap(),
            true,
        ),
        (
            "vfill a7 -> l1[32]",
            ir::memory(ir::Memory::Fill, Term::name("a7"), 32, None, false, vec![]).unwrap(),
            false,
        ),
        ("seti s1 = 2", ir::seti("s1", Term::int(2)), false),
        (
            "getstk s1",
            ir::instruction("getstk", vec![Term::name("s1")], vec![]),
            false,
        ),
        ("vwait fill rx n=1", ir::wait("fill", "rx", 1), false),
        ("vpost vc n=1", ir::post("vc", 1), false),
        (
            "uptr 32",
            ir::instruction("uptr", vec![Term::int(32)], vec![]),
            false,
        ),
        ("halt", ir::instruction("halt", vec![], vec![]), false),
    ];
    for (text, st, accept) in fixtures {
        assert_eq!(print::emit(&mut [st.clone()]).unwrap(), format!("{text}\n"));
        assert_eq!(REBUILT.iter().any(|r| text.starts_with(r)), accept);
        assert_eq!(rebuilt(&Event::Fixed(st)), accept, "{text}");
    }
    assert!(!rebuilt(&Event::Open));
    assert!(!rebuilt(&Event::Close));
    assert!(rebuilt(&Event::Op {
        stmt: ir::math(
            "vmov",
            ir::Symbol(0).operand(ir::Select::Whole),
            vec![Term::name("k0")],
            vec![]
        ),
        syms: vec![ir::Symbol(0)],
    }));
}
