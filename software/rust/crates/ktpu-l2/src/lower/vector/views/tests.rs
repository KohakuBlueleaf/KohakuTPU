//! Descriptor walk bytes, extents and accesses against the text reference.

use super::*;
use crate::lower::sync::{mem, Access};
use ktpu_machine::Machine;

/// The migration base's mod.rs Desc::extent, operating only on reference text.
fn old_extent(text: &str) -> i64 {
    let mut pairs: Vec<(i64, i64)> = Vec::new();
    let b = text.as_bytes();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'(' {
            if let Some(end) = text[i..].find(')') {
                let inner = &text[i + 1..i + end];
                if let Some((s, n)) = inner.split_once(", ") {
                    if let (Ok(s), Ok(n)) = (s.parse::<i64>(), n.parse::<i64>()) {
                        if n >= 0 {
                            pairs.push((s, n));
                        }
                    }
                }
                i += end;
            }
        }
        i += 1;
    }
    if pairs.is_empty() {
        let (s, n) = text.split_once(", ").unwrap();
        pairs.push((s.parse().unwrap(), n.parse().unwrap()));
    }
    32 + pairs.iter().map(|(s, n)| (n - 1) * s).sum::<i64>()
}

fn cases() -> Vec<(Walk, String)> {
    // ctx.rs walk/rows_walk and views.rs tiled/mx7 formatting in the migration base.
    let (nbytes, row_words, pitch, rows, per, stride, bands) = (256, 2, 128, 4, 8, 256, 2);
    let dims = [(32, 8), (1024, 2), (256, 4)];
    vec![
        (Walk::flat(32, nbytes / 32), format!("32, {}", nbytes / 32)),
        (
            Walk(vec![(32, row_words), (pitch, rows)]),
            format!("(32, {row_words}), ({pitch}, {rows})"),
        ),
        (
            Walk(vec![(32, per), (stride, bands)]),
            format!("(32, {per}), ({stride}, {bands})"),
        ),
        (
            Walk(dims.to_vec()),
            dims.iter()
                .map(|(s, n)| format!("({s}, {n})"))
                .collect::<Vec<_>>()
                .join(", "),
        ),
        (Walk(vec![(32, 2), (-128, 4)]), "(32, 2), (-128, 4)".into()),
        (Walk::flat(-32, 8), "-32, 8".into()),
        (Walk::flat(0, 1), "0, 1".into()),
        (Walk::flat(32, 0), "32, 0".into()),
    ]
}

#[test]
fn bridge_and_extent_match_text_reference() {
    for (walk, text) in cases() {
        assert_eq!(walk.to_string().as_bytes(), text.as_bytes());
        let desc = Desc {
            walk,
            ..Desc::default()
        };
        assert_eq!(desc.extent(), old_extent(&text), "{text}");
    }
}

#[test]
fn signed_increments_keep_spans_accesses_and_image_bytes() {
    let body = Body {
        name: "k".into(),
        params: vec![],
        scratch: IndexMap::new(),
        stmts: vec![],
        reader: Default::default(),
        macros: IndexMap::new(),
        buffers: IndexMap::new(),
        vregs: 32,
        vcopies: 4,
        fill: (0, 1),
    };
    let t = Target::of(&Machine::named("multimesh_v9t6").unwrap());
    for (walk, text) in cases() {
        for inc in [-256, 0, 256] {
            for trips in [1, 2, 5] {
                for kind in ["load", "store"] {
                    let d = Desc {
                        walk: walk.clone(),
                        off: 4096,
                        inc,
                        buffer: Some("A".into()),
                        kind: kind.into(),
                        ..Desc::default()
                    };
                    let step = inc * (trips - 1);
                    let expected_span =
                        (4096 + step.min(0), 4096 + old_extent(&text) + step.max(0));
                    assert_eq!(d.span(trips), expected_span);
                    let mut c = ctx::Ctx::new(&body, &t, &Env::new(), None).unwrap();
                    c.unit = "vc[0]".into();
                    c.trips = (0..trips).collect();
                    c.descs.push(d);
                    let expected_accesses = vec![
                        mem(
                            "vc[0]",
                            kind == "store",
                            "A",
                            expected_span.0,
                            expected_span.1,
                        ),
                        Access::new("vc[0]", true, ("vc[0]", "l1"), 0, 1),
                    ];
                    assert_eq!(c.accesses(), expected_accesses);
                    let (image, params, accesses) = ctx::band::typed::image(
                        &c,
                        "k",
                        &[crate::lower::vector::gen::typed::Event::Fixed(
                            ktpu_l1::text::Stmt::new("halt", vec![]),
                        )],
                    )
                    .unwrap();
                    let image = image.source;
                    let increment = if inc == 0 {
                        String::new()
                    } else {
                        format!("    ainc a0 += {inc}\n")
                    };
                    assert_eq!(image, format!("image k(p_a0)\n    desc a0 = p_a0 walk=({text})\n{increment}    halt\n"));
                    assert_eq!(params, vec!["A+4096"]);
                    assert_eq!(accesses, expected_accesses);
                }
            }
        }
    }
}

fn term(text: &str) -> Term {
    ktpu_l1::text::parse(&format!("x {text}\n"), "<walk>")
        .unwrap()
        .remove(0)
        .args
        .remove(0)
}

#[test]
fn tiled_and_mx7_producers_keep_walks() {
    let t = Target::of(&Machine::named("multimesh_v9t6").unwrap());
    let tile = crate::ir::Buffer::of(&term("f16[16, 32] @dram(tile=4x8)")).unwrap();
    for (view, dims, text) in [
        ("A[0:4, :]", vec![(32, 8)], "32, 8"),
        ("A[0:8, :]", vec![(32, 8), (256, 2)], "(32, 8), (256, 2)"),
    ] {
        let ax = M::axes_of(&term(view), &tile.shape).unwrap();
        let (_, walk, _) = tiled(&tile, &ax, &t).unwrap();
        assert_eq!(walk.0, dims);
        assert_eq!(walk.to_string(), text);
    }
    let mx = crate::ir::Buffer::of(&term("mx7a[16, 128] @dram(gm=4, nk=2)")).unwrap();
    for (view, dims, text) in [
        ("A[0:4, 0:64]", vec![(32, 8)], "32, 8"),
        (
            "A[0:16, 0:128]",
            vec![(32, 8), (1024, 2), (256, 4)],
            "(32, 8), (1024, 2), (256, 4)",
        ),
    ] {
        let ax = M::axes_of(&term(view), &mx.shape).unwrap();
        let (_, walk, _) = mx7(&mx, &ax, &t).unwrap();
        assert_eq!(walk.0, dims);
        assert_eq!(walk.to_string(), text);
    }
}
