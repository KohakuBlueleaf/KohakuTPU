//! A plan's shape and machine facts as Python `repr`s them (the kernel's
//! name: `Plan.shape()` and the `Hw` dataclass).

use super::*;
use crate::pyrepr::Py;

fn int(v: i64) -> Py {
    Py::Int(v)
}

fn tuple<T>(xs: &[T], f: impl Fn(&T) -> Py) -> Py {
    Py::Tuple(xs.iter().map(f).collect())
}

pub fn hw_repr(hw: &Hw) -> Py {
    Py::Obj(
        "Hw",
        vec![
            ("lanes", int(hw.lanes)),
            ("row", int(hw.row)),
            ("word", int(hw.word)),
            ("line", int(hw.line)),
            ("cores", int(hw.cores)),
            ("levels", int(hw.levels)),
            ("grain", int(hw.grain)),
            ("descriptors", int(hw.descriptors)),
        ],
    )
}

fn cls(c: &Cls) -> Py {
    Py::Obj(
        "Cls",
        vec![
            ("rot", int(c.rot)),
            ("rmask", int(c.rmask)),
            ("mask", int(c.mask)),
            ("row0", int(c.row0)),
            ("rows", int(c.rows)),
        ],
    )
}

fn layer(l: &Layer) -> Py {
    Py::Obj(
        "Layer",
        vec![
            ("k", int(l.k)),
            ("base", Py::str(&l.base)),
            ("row0", int(l.row0)),
            ("rows", int(l.rows)),
            ("at_d", int(l.at_d)),
            ("classes", tuple(&l.classes, cls)),
        ],
    )
}

fn group(g: &Group) -> Py {
    Py::Obj(
        "Group",
        vec![
            ("words", int(g.words)),
            ("row0", int(g.row0)),
            ("rows", int(g.rows)),
            ("at_s", int(g.at_s)),
            ("at_s2", g.at_s2.map_or(Py::None, int)),
            ("layers", tuple(&g.layers, layer)),
        ],
    )
}

fn slot(s: &Slot) -> Py {
    match s {
        Slot::Walk(levels, at) => Py::Tuple(vec![Py::str("walk"), Py::walk(levels), int(*at)]),
        Slot::List(at) => Py::Tuple(vec![Py::str("list"), int(*at)]),
    }
}

fn prog(p: &Prog) -> Py {
    Py::Tuple(vec![
        int(p.0),
        tuple(&p.1, |&(k, m)| Py::ints(&[k, m])),
        Py::ints(&p.2),
    ])
}

fn part(p: &Part) -> Py {
    match p {
        Part::Fill(f) => Py::Obj(
            "Fill",
            vec![
                ("dst", Py::str(&f.dst)),
                ("w0", int(f.w0)),
                ("words", int(f.words)),
            ],
        ),
        Part::Words(w) => Py::Obj(
            "Words",
            vec![
                ("src", Py::str(&w.src)),
                ("dst", Py::str(&w.dst)),
                ("levels", Py::walk(&w.levels)),
                ("at", int(w.at)),
                ("dlevels", Py::walk(&w.dlevels)),
                ("dat", int(w.dat)),
            ],
        ),
        Part::Runs(r) => Py::Obj(
            "Runs",
            vec![
                ("src", Py::str(&r.src)),
                ("src_words", int(r.src_words)),
                ("pitch", int(r.pitch)),
                ("margin", int(r.margin)),
                ("dst", Py::str(&r.dst)),
                ("groups", tuple(&r.groups, group)),
            ],
        ),
        Part::Splat(s) => Py::Obj(
            "Splat",
            vec![
                ("src", Py::str(&s.src)),
                ("w0", int(s.w0)),
                ("count", int(s.count)),
                ("dst", Py::str(&s.dst)),
                ("ones", int(s.ones)),
            ],
        ),
        Part::Pull(p) => Py::Obj(
            "Pull",
            vec![
                ("src", Py::str(&p.src)),
                ("dst", Py::str(&p.dst)),
                ("w0", int(p.w0)),
                ("words", int(p.words)),
                ("rows", int(p.rows)),
                ("out", int(p.out)),
                ("slots", tuple(&p.slots, slot)),
                ("codes", int(p.codes)),
                ("pairs", Py::ints(&p.pairs)),
                ("base", Py::str(&p.base)),
                ("zeros", Py::Bool(p.zeros)),
                (
                    "tree",
                    match &p.tree {
                        None => Py::Tuple(vec![]),
                        Some((sel, valid, zero)) => {
                            Py::Tuple(vec![Py::ints(sel), int(*valid), int(*zero)])
                        }
                    },
                ),
            ],
        ),
        Part::Exchange(x) => Py::Obj(
            "Exchange",
            vec![
                ("src", Py::str(&x.src)),
                ("dst", Py::str(&x.dst)),
                ("nb", int(x.nb)),
                ("A", int(x.a)),
                ("B", int(x.b)),
                ("masks", int(x.masks)),
            ],
        ),
        Part::Shuffle(s) => Py::Obj(
            "Shuffle",
            vec![
                ("src", Py::str(&s.src)),
                ("at", int(s.at)),
                ("nb", int(s.nb)),
                ("sb", int(s.sb)),
                ("A", int(s.a)),
                ("B", int(s.b)),
                ("Ap", int(s.ap)),
                ("dst", Py::str(&s.dst)),
                ("progs", tuple(&s.progs, prog)),
            ],
        ),
    }
}

/// `Plan.shape()`: all but the tables' contents.
pub fn shape_repr(pl: &Plan) -> Py {
    Py::Tuple(vec![
        tuple(&pl.parts, part),
        tuple(&pl.scratch, |(n, r, w)| {
            Py::Tuple(vec![Py::str(n), int(*r), int(*w)])
        }),
        int(pl.index.len() as i64),
        Py::ints(&[pl.masks.len() as i64, pl.masks[0].len() as i64]),
        int(pl.src_words),
        int(pl.dst_words),
    ])
}
