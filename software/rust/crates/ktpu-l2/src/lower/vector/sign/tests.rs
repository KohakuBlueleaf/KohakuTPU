use super::*;
#[cfg(feature = "witness")]
use crate::lower::vector::gen::print;
use crate::lower::vector::gen::typed::Symbol;
#[cfg(feature = "witness")]
use crate::lower::vector::gen::Code;

const LANES: Lanes = Lanes {
    lanes: 16,
    chunks: 8,
};

fn n(s: &str) -> Term {
    Term::name(s)
}

/// `s[c:]`: chunk c on, one a beat.
fn from(s: &str, c: u64) -> Term {
    ir::reg(s, ir::Select::Stride(c))
}

fn vl(n: i128) -> Term {
    ir::kw("vl", Term::int(n))
}

fn merge(x: Term, k: i64) -> Term {
    ir::crossbar("merge", x, k)
}

fn op(o: &str, dst: Term, srcs: Vec<Term>, opts: Vec<Term>) -> Event {
    Event::Op {
        stmt: ir::math(o, dst, srcs, opts),
        syms: vec![Symbol(1), Symbol(2)],
    }
}

/// `d = k0 + x`, the preserve copy.
fn add0(d: Term, x: Term, opts: Vec<Term>) -> Event {
    op("vadd", d, vec![n("k0"), x], opts)
}

/// The block header `op s3`.
fn header(o: &str) -> Event {
    Event::Fixed(Stmt::new(o, vec![n("s3")]))
}

/// `%N` as `vN`, the spelling allocation gives a symbol.
#[cfg(feature = "witness")]
fn physical(n: &str) -> String {
    format!("v{}", n.strip_prefix('%').unwrap())
}

#[cfg(feature = "witness")]
fn resolve(t: &mut Term) {
    match t {
        Term::Name(n) | Term::View(n, _) if vector_name(n) => *n = physical(n),
        _ => (),
    }
    match t {
        Term::View(_, args) | Term::Call(_, args) => args.iter_mut().for_each(resolve),
        Term::Assign(a, b) => {
            resolve(a);
            resolve(b);
        }
        Term::Kw(_, a) => resolve(a),
        _ => (),
    }
}

/// Each event printed with its symbols allocated as `vN`; a block edge as
/// the generator's `{` / `}`.
#[cfg(feature = "witness")]
fn text(events: &[Event]) -> Vec<String> {
    events
        .iter()
        .map(|e| match e {
            Event::Op { stmt, .. } | Event::Fixed(stmt) => {
                let mut stmt = stmt.clone();
                stmt.args.iter_mut().for_each(resolve);
                print::emit(&mut [stmt]).unwrap().trim().to_string()
            }
            Event::Open => "{".into(),
            Event::Close => "}".into(),
        })
        .collect()
}

fn preserved(events: Vec<Event>) -> Vec<Event> {
    ZeroSign::Preserve.events(events, LANES).unwrap()
}

fn refused(events: Vec<Event>) -> String {
    ZeroSign::Preserve.events(events, LANES).unwrap_err()
}

fn when(key: &str) -> Term {
    ir::kw(key, n("p1"))
}

fn test(o: &str, p: Term, a: Term, b: Term, opts: Vec<Term>) -> Event {
    op(o, p, vec![a, b], opts)
}

#[test]
fn names_parse_back() {
    for z in ZeroSign::ALL {
        assert_eq!(ZeroSign::parse(z.name()), Some(z));
    }
    assert_eq!(ZeroSign::parse("keep"), None);
}

/// A zero of x takes the constant and only a nonzero x moves, so the core's
/// select is exact (ISA.VC.058); an arm in a vector register can hold -0.
#[test]
fn a_select_of_x_or_a_constant_on_x_stays_the_core_select() {
    let lift = op("vsel", n("%1"), vec![n("%2"), n("s3"), n("%2")], vec![]);
    assert_eq!(preserved(vec![lift.clone()]), vec![lift]);
    assert_eq!(
        preserved(vec![op(
            "vsel",
            n("%1"),
            vec![n("%2"), n("%3"), n("%2")],
            vec![]
        )]),
        vec![
            test("vcmpeq", n("p1"), n("%2"), n("k0"), vec![]),
            add0(n("%1"), n("%2"), vec![]),
            add0(n("%1"), n("%3"), vec![when("if")]),
        ]
    );
    assert_eq!(
        preserved(vec![op(
            "vsel",
            n("%1"),
            vec![n("%2"), n("s3"), n("%4")],
            vec![]
        )]),
        vec![
            test("vcmpeq", n("p1"), n("%4"), n("k0"), vec![]),
            add0(n("%1"), n("%2"), vec![]),
            op("vmov", n("%1"), vec![n("s3")], vec![when("if")]),
        ]
    );
}

#[test]
fn native_keeps_every_event() {
    let events = vec![
        op("vmov", n("%1"), vec![n("%2")], vec![]),
        op(
            "vmax",
            n("%1"),
            vec![n("%2"), merge(from("%2", 4), 4)],
            vec![],
        ),
    ];
    assert_eq!(
        ZeroSign::Native.events(events.clone(), LANES).unwrap(),
        events
    );
    let (all, head) = ZeroSign::Native
        .trip(events.clone(), events.clone(), LANES)
        .unwrap();
    assert_eq!((all.len(), head), (4, 2));
}

#[test]
fn copies_become_an_add_to_k0() {
    let chunk = ir::reg("%2", ir::Select::Chunk(0));
    let xor = ir::crossbar("xor", n("%2"), 8);
    assert_eq!(
        preserved(vec![
            op("vmov", n("%1"), vec![n("%2")], vec![]),
            op("vmov", n("%1"), vec![n("%2")], vec![when("if")]),
            op("vmov", n("%1"), vec![chunk.clone()], vec![vl(64)]),
            op("vmul", n("%1"), vec![n("k1"), xor.clone()], vec![]),
            op("vmul", n("%1"), vec![chunk.clone(), n("k1")], vec![vl(64)]),
            op("vneg", n("%1"), vec![n("%2")], vec![]),
        ]),
        vec![
            add0(n("%1"), n("%2"), vec![]),
            add0(n("%1"), n("%2"), vec![when("if")]),
            add0(n("%1"), chunk.clone(), vec![vl(64)]),
            add0(n("%1"), xor, vec![]),
            add0(n("%1"), chunk, vec![vl(64)]),
            op("vsub", n("%1"), vec![n("k0"), n("%2")], vec![]),
        ]
    );
}

#[test]
fn constants_arithmetic_and_abs_are_kept() {
    let kept = vec![
        op("vmov", n("%1"), vec![n("k0")], vec![]),
        op("vmov", n("%1"), vec![n("k1")], vec![when("if")]),
        op("vmov", n("%1"), vec![n("s13")], vec![]),
        op("vmul", n("%1"), vec![n("%2"), n("%3")], vec![]),
        op("vmul", n("%1"), vec![n("k2"), n("%3")], vec![]),
        op("vadd", n("%1"), vec![n("%2"), n("%3")], vec![]),
        op(
            "vadd",
            n("%1"),
            vec![n("%2"), merge(from("%2", 4), 4)],
            vec![],
        ),
        op("vabs", n("%1"), vec![n("%2")], vec![]),
    ];
    assert_eq!(preserved(kept.clone()), kept);
}

#[test]
fn max_min_and_select_pick_by_p1() {
    let xor = ir::crossbar("xor", n("%2"), 8);
    let p1 = || n("p1");
    let cases = [
        (
            op("vmax", n("%1"), vec![n("%2"), n("%3")], vec![]),
            vec![
                test("vcmplt", p1(), n("%2"), n("%3"), vec![]),
                add0(n("%1"), n("%2"), vec![]),
                add0(n("%1"), n("%3"), vec![when("if")]),
            ],
        ),
        (
            op("vmin", n("%2"), vec![n("%2"), n("%3")], vec![]),
            vec![
                test("vcmpgt", p1(), n("%2"), n("%3"), vec![]),
                add0(n("%2"), n("%3"), vec![when("if")]),
            ],
        ),
        (
            op("vmax", n("%3"), vec![n("%2"), n("%3")], vec![]),
            vec![
                test("vcmplt", p1(), n("%2"), n("%3"), vec![]),
                add0(n("%3"), n("%2"), vec![when("unless")]),
            ],
        ),
        (
            op("vmax", n("%2"), vec![n("%2"), xor.clone()], vec![]),
            vec![
                test("vcmplt", p1(), n("%2"), xor.clone(), vec![]),
                add0(n("%2"), xor, vec![when("if")]),
            ],
        ),
        (
            op("vmax", n("%1"), vec![n("%2"), n("s3")], vec![]),
            vec![
                test("vcmplt", p1(), n("%2"), n("s3"), vec![]),
                add0(n("%1"), n("%2"), vec![]),
                op("vmov", n("%1"), vec![n("s3")], vec![when("if")]),
            ],
        ),
        (
            op("vsel", n("%1"), vec![n("%2"), n("%3"), n("%4")], vec![]),
            vec![
                test("vcmpeq", p1(), n("%4"), n("k0"), vec![]),
                add0(n("%1"), n("%2"), vec![]),
                add0(n("%1"), n("%3"), vec![when("if")]),
            ],
        ),
        (
            op("vsel", n("%4"), vec![n("%4"), n("%3"), n("%4")], vec![]),
            vec![
                test("vcmpeq", p1(), n("%4"), n("k0"), vec![]),
                add0(n("%4"), n("%3"), vec![when("if")]),
            ],
        ),
    ];
    for (event, want) in cases {
        assert_eq!(preserved(vec![event]), want);
    }
}

/// A compare writes P1 at its destination's chunks and a predicated copy
/// reads it at its own (ISA.VC.080, .081): the compare takes the pick's
/// destination select.
#[test]
fn a_pick_compares_on_its_destination_chunks() {
    let one = ir::reg("%1", ir::Select::Chunk(3));
    assert_eq!(
        preserved(vec![
            op(
                "vmax",
                from("%1", 4),
                vec![from("%2", 0), from("%2", 1)],
                vec![vl(16)]
            ),
            op(
                "vmin",
                one.clone(),
                vec![one.clone(), from("%3", 2)],
                vec![vl(32)]
            ),
        ]),
        vec![
            test(
                "vcmplt",
                from("p1", 4),
                from("%2", 0),
                from("%2", 1),
                vec![vl(16)]
            ),
            add0(from("%1", 4), from("%2", 0), vec![vl(16)]),
            add0(from("%1", 4), from("%2", 1), vec![vl(16), when("if")]),
            test(
                "vcmpgt",
                ir::reg("p1", ir::Select::Chunk(3)),
                one.clone(),
                from("%3", 2),
                vec![vl(32)]
            ),
            add0(one, from("%3", 2), vec![vl(32), when("if")]),
        ]
    );
}

#[test]
fn a_pick_is_refused_where_it_cannot_be_exact() {
    assert!(refused(vec![op(
        "vmax",
        n("%1"),
        vec![n("%2"), n("%3")],
        vec![when("if")]
    )])
    .contains("predicated"));
    assert!(refused(vec![op(
        "vmax",
        n("%2"),
        vec![n("%3"), ir::crossbar("xor", n("%2"), 8)],
        vec![]
    )])
    .contains("overlaps"));
    let live = vec![
        test("vcmplt", n("p1"), n("%5"), n("%6"), vec![]),
        op("vmax", n("%1"), vec![n("%2"), n("%3")], vec![]),
        op("vmov", n("%7"), vec![n("k1")], vec![when("if")]),
    ];
    assert!(refused(live).contains("p1 is live"));
    let dead = vec![
        test("vcmplt", n("p1"), n("%5"), n("%6"), vec![]),
        op("vmov", n("%7"), vec![n("k1")], vec![when("if")]),
        op("vmax", n("%1"), vec![n("%2"), n("%3")], vec![]),
    ];
    assert_eq!(preserved(dead).len(), 5);
}

/// P1 is live by the bit: a pick on other chunks than the pending read's
/// leaves it, a pick on the same chunk is refused.
#[test]
fn liveness_is_by_the_predicate_bit() {
    let program = |pick: Term| {
        vec![
            test(
                "vcmplt",
                n("p1"),
                from("%5", 0),
                from("%6", 0),
                vec![vl(16)],
            ),
            op(
                "vmax",
                pick,
                vec![from("%2", 0), from("%3", 0)],
                vec![vl(16)],
            ),
            op(
                "vmov",
                from("%7", 0),
                vec![n("k1")],
                vec![vl(16), when("if")],
            ),
        ]
    };
    assert_eq!(preserved(program(from("%1", 4))).len(), 5);
    assert!(refused(program(from("%1", 0))).contains("p1 is live"));
}

/// A loop's body runs again after its end: a pick after the compare a later
/// trip reads at the top would overwrite that P1. A skip runs once, a
/// modulo trip repeats, and a fixed line's read counts as an op's.
#[test]
fn liveness_follows_the_blocks() {
    let body = || {
        vec![
            op("vmov", n("%7"), vec![n("k1")], vec![when("if")]),
            test("vcmplt", n("p1"), n("%5"), n("%6"), vec![]),
            op("vmax", n("%1"), vec![n("%2"), n("%3")], vec![]),
        ]
    };
    let block = |h: &str| {
        let mut events = vec![header(h), Event::Open];
        events.extend(body());
        events.push(Event::Close);
        events
    };
    assert!(refused(block("loop")).contains("p1 is live"));
    assert_eq!(preserved(block("skipz")).len(), 8);
    assert!(ZeroSign::Preserve
        .trip(vec![], body(), LANES)
        .unwrap_err()
        .contains("p1 is live"));
    let (all, head) = ZeroSign::Preserve
        .trip(
            vec![op("vmov", n("%1"), vec![n("%2")], vec![])],
            body()[..2].to_vec(),
            LANES,
        )
        .unwrap();
    assert_eq!((all.len(), head), (3, 1));
    let mut nested = block("skipz");
    nested.splice(0..0, [header("loop"), Event::Open]);
    nested.push(Event::Close);
    assert!(refused(nested).contains("p1 is live"));
    let fixed = vec![
        op("vmax", n("%1"), vec![n("%2"), n("%3")], vec![]),
        Event::Fixed(ir::math("vmov", n("%7"), vec![n("k1")], vec![when("if")])),
    ];
    assert!(refused(fixed).contains("p1 is live"));
}

#[test]
fn a_rewrite_keeps_the_op_symbols_and_the_other_events() {
    let events = vec![
        Event::Fixed(ir::seti("s0", Term::Float(2.0))),
        op("vmax", n("%1"), vec![n("%2"), n("%3")], vec![]),
        header("skipz"),
        Event::Open,
        Event::Close,
    ];
    let got = preserved(events.clone());
    assert_eq!(got.len(), 7);
    assert_eq!(got[0], events[0]);
    assert_eq!(got[4..], events[2..]);
    for e in &got[1..4] {
        assert!(matches!(e, Event::Op { syms, .. } if syms == &vec![Symbol(1), Symbol(2)]));
    }
}

#[test]
fn negative_zero_literals_are_set_only_under_preserve() {
    assert!(ZeroSign::Preserve.negative_zero(-0.0));
    assert!(!ZeroSign::Preserve.negative_zero(0.0));
    assert!(!ZeroSign::Native.negative_zero(-0.0));
}

#[test]
fn only_preserve_picks_and_only_max_min_and_select() {
    for o in ["vmax", "vmin", "vsel"] {
        assert!(ZeroSign::Preserve.picks(o));
        assert!(!ZeroSign::Native.picks(o));
    }
    assert!(!ZeroSign::Preserve.picks("vadd"));
}

#[cfg(feature = "mutants")]
#[test]
fn each_mutant_restores_the_broken_form() {
    let viewed = op(
        "vmax",
        from("%1", 4),
        vec![from("%2", 0), from("%2", 1)],
        vec![vl(16)],
    );
    let events = vec![
        op("vmov", n("%1"), vec![n("%2")], vec![]),
        op("vmax", n("%1"), vec![n("%2"), n("%3")], vec![]),
        op("vsel", n("%1"), vec![n("%2"), n("%3"), n("%4")], vec![]),
        viewed,
    ];
    let under = |m: &'static str, f: &dyn Fn() -> bool| {
        crate::mutant::scoped(
            crate::mutant::State {
                active: Some(m),
                ..Default::default()
            },
            f,
        )
    };
    let events = &events;
    let has = move |e: Event| move || preserved(events.clone()).contains(&e);
    let unviewed = test(
        "vcmplt",
        n("p1"),
        from("%2", 0),
        from("%2", 1),
        vec![vl(16)],
    );
    assert!(has(add0(n("%1"), n("%2"), vec![]))());
    assert!(!has(unviewed.clone())());
    assert!(under("l2.zero_sign/native-copy", &has(events[0].clone())));
    assert!(under("l2.zero_sign/native-minmax", &has(events[1].clone())));
    assert!(under("l2.zero_sign/native-sel", &has(events[2].clone())));
    assert!(under("l2.zero_sign/compare-unviewed", &has(unviewed)));
    let lift = op("vsel", n("%1"), vec![n("%2"), n("s3"), n("%2")], vec![]);
    let picked = || preserved(vec![lift.clone()]) != vec![lift.clone()];
    assert!(!picked() && under("l2.zero_sign/lift-picked", &picked));
    assert!(under("l2.zero_sign/fold-negzero", &|| !ZeroSign::Preserve
        .negative_zero(-0.0)));
    let looped = || {
        vec![
            header("loop"),
            Event::Open,
            op("vmov", n("%7"), vec![n("k1")], vec![when("if")]),
            test("vcmplt", n("p1"), n("%5"), n("%6"), vec![]),
            op("vmax", n("%1"), vec![n("%2"), n("%3")], vec![]),
            Event::Close,
        ]
    };
    assert!(ZeroSign::Preserve.events(looped(), LANES).is_err());
    assert!(under("l2.zero_sign/flat-liveness", &|| ZeroSign::Preserve
        .events(looped(), LANES)
        .is_ok()));
    let folds = || crate::lower::vector::tree::lane_folds(ZeroSign::Preserve, "vmax");
    assert!(folds() && !under("l2.vector/merge-tree-picked", &folds));
}

/// The string mirror (the witness path) and the typed events lower the same
/// input to the same lines, blocks and views included; the copy is spelled
/// as the card reads it.
#[cfg(feature = "witness")]
#[test]
fn the_template_mirror_rewrites_as_the_typed_events() {
    let events = vec![
        op("vmov", n("%1"), vec![n("k0")], vec![when("if")]),
        op("vmov", n("%1"), vec![n("%2")], vec![]),
        op(
            "vmov",
            n("%1"),
            vec![ir::reg("%2", ir::Select::Chunk(0))],
            vec![vl(64)],
        ),
        op(
            "vmul",
            n("%1"),
            vec![n("k1"), ir::crossbar("xor", n("%2"), 8)],
            vec![],
        ),
        op("vneg", n("%1"), vec![n("%2")], vec![]),
        op("vmax", n("%1"), vec![n("%2"), n("%3")], vec![]),
        op("vmin", n("%2"), vec![n("%2"), n("%3")], vec![]),
        op("vmax", n("%3"), vec![n("%2"), n("%3")], vec![]),
        op("vsel", n("%1"), vec![n("%2"), n("%3"), n("%4")], vec![]),
        op(
            "vmax",
            from("%1", 4),
            vec![from("%2", 0), from("%2", 1)],
            vec![vl(16)],
        ),
        op(
            "vmin",
            n("%1"),
            vec![n("%2"), ir::crossbar("xor", n("%2"), 8)],
            vec![],
        ),
        Event::Fixed(ir::seti("s0", Term::Float(2.0))),
        header("loop"),
        Event::Open,
        test("vcmplt", n("p1"), n("%2"), n("%3"), vec![]),
        op("vmov", n("%4"), vec![n("k1")], vec![when("if")]),
        Event::Close,
    ];
    let syms: Vec<String> = ["%1", "%2", "%3", "%4"].map(String::from).to_vec();
    let allocated = |s: &[String]| s.iter().map(|s| physical(s)).collect::<Vec<_>>();
    // The printed `vN` back as `%N`, which `template` places.
    let symbolic = |line: &str| {
        let b = line.as_bytes();
        let mut out = String::new();
        for (i, c) in line.char_indices() {
            let word = i == 0 || !b[i - 1].is_ascii_alphanumeric();
            let digits = b[i + 1..].iter().take_while(|d| d.is_ascii_digit()).count();
            let after = !matches!(b.get(i + 1 + digits), Some(d) if d.is_ascii_alphanumeric());
            out.push(if c == 'v' && word && digits > 0 && after {
                '%'
            } else {
                c
            });
        }
        out
    };
    let code = |events: &[Event]| -> Vec<Code> {
        text(events)
            .iter()
            .zip(events)
            .map(|(line, e)| match e {
                Event::Op { .. } => Code::Op(template(&symbolic(line), &syms), syms.clone()),
                _ => Code::Raw(line.clone()),
            })
            .collect()
    };
    let printed = |code: Vec<Code>| -> Vec<String> {
        code.iter()
            .map(|c| match c {
                Code::Op(t, s) => crate::lower::vector::pat::format(t, &allocated(s)),
                Code::Raw(r) => r.clone(),
            })
            .collect()
    };
    let mut refusals = vec![events.clone(), events.clone()];
    refusals[0].push(op("vmov", n("%1"), vec![n("k0")], vec![when("if")]));
    refusals[1].insert(16, op("vmax", n("%1"), vec![n("%2"), n("%3")], vec![]));
    refusals[1].swap(14, 15);
    for live in refusals {
        assert_eq!(
            ZeroSign::Preserve.code(code(&live), LANES).unwrap_err(),
            refused(live)
        );
    }
    let mirrored = printed(ZeroSign::Preserve.code(code(&events), LANES).unwrap());
    let typed = text(&preserved(events.clone()));
    assert_eq!(typed, mirrored);
    assert_eq!(typed[1], "vadd v1, k0, v2");
    assert!(typed.iter().any(|l| l.starts_with("vcmplt p1[4")));
    let (head, trip) = events.split_at(9);
    let (mirror, at) = ZeroSign::Preserve
        .code_trip(code(head), code(trip), LANES)
        .unwrap();
    let (typed, typed_at) = ZeroSign::Preserve
        .trip(head.to_vec(), trip.to_vec(), LANES)
        .unwrap();
    assert_eq!((printed(mirror), at), (text(&typed), typed_at));
}
