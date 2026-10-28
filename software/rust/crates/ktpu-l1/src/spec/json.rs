//! The L1 runners' JSON forms (`software/tests/spec/runners/l1.py`): terms,
//! statements, and words decoded by the runner's own field tables (MSB
//! first), never by the stack's encoder, so a field the stack moves shows up
//! as a different value.

use ktpu_isa::W256;
use ktpu_l0::{Alu, Cond, Op, Package, Patch, Program, Stmt as PStmt};
use serde_json::{json, Value};

use crate::text::{py_float, IntStyle, Stmt, Term};

// ------------------------------------------------------------- decoders

const MATH_OPS: [&str; 19] = [
    "vmov", "vneg", "vabs", "vadd", "vsub", "vmul", "vfma", "vfnma", "vmax", "vmin", "vsel",
    "vcmplt", "vcmpgt", "vcmpeq", "vexp2", "vlog2", "vinv", "vrsqrt", "vexp2d",
];
type Table = &'static [(&'static str, i32)];
const MATH_F: Table = &[
    ("vd", 5),
    ("va", 5),
    ("vb", 5),
    ("vc", 5),
    ("sa", 2),
    ("sb", 2),
    ("sc", 2),
    ("om", 1),
];
const CHUNK_F: Table = &[
    ("d_base", 3),
    ("d_stride", 1),
    ("c_base", 3),
    ("c_stride", 1),
    ("b_base", 3),
    ("b_stride", 1),
    ("a_base", 3),
    ("a_stride", 1),
];
const WALK_F: Table = &[
    ("reg", 5),
    ("mode", 2),
    ("n1", 3),
    ("cb", 3),
    ("rel", 1),
    ("q", 1),
    ("off", 12),
];
const SYNC_F: Table = &[
    ("waiter", 3),
    ("on", 3),
    ("slack", 6),
    ("marked", 1),
    ("mark", 3),
    ("q", 1),
    ("z", 10),
];
const WAIT_F: Table = &[
    ("waiter", 3),
    ("on", 3),
    ("n", 10),
    ("q", 1),
    ("counter", 1),
    ("z", 9),
];

type Fields = Vec<(&'static str, i32)>;

fn cfg_f(sel: u64) -> Option<(&'static str, Fields)> {
    let mut chunk = CHUNK_F.to_vec();
    Some(match sel {
        0 => ("CHK", {
            let mut v = vec![("z", 7)];
            v.append(&mut chunk);
            v
        }),
        1 => (
            "XB",
            vec![
                ("z", 8),
                ("pr", 2),
                ("pm", 2),
                ("xc", 1),
                ("sh", 3),
                ("xk", 4),
                ("xm", 3),
            ],
        ),
        2 => ("UPTR", vec![("z", 10), ("ptr", 13)]),
        3 => ("UINC", vec![("z", 10), ("ptr", 13)]),
        4 => ("USTR", vec![("z", 5), ("s1", 9), ("s0", 9)]),
        5 => ("PPTR", vec![("z", 10), ("ptr", 13)]),
        6 => ("PINC", vec![("z", 10), ("ptr", 13)]),
        7 => ("PSTR", vec![("z", 5), ("s1", 9), ("s0", 9)]),
        8 => ("GETSTK", vec![("z", 19), ("sreg", 4)]),
        9 => ("AINC", vec![("ad", 3), ("z", 2), ("inc", -18)]),
        10 => ("CHKVL", {
            let mut v = vec![("vl1", 7)];
            v.append(&mut chunk);
            v
        }),
        _ => return None,
    })
}

fn core_f(op: u64) -> Option<(&'static str, Fields)> {
    Some(match op {
        0x14 => ("VUNPK", WALK_F.to_vec()),
        0x15 => ("VPACK", WALK_F.to_vec()),
        0x17 => (
            "VSKIPZ",
            vec![("z1", 1), ("sreg", 4), ("z", 14), ("skip", 8)],
        ),
        0x18 => ("VSETVL", vec![("z1", 6), ("sreg", 4), ("z", 17)]),
        0x19 => (
            "VPOST",
            vec![("dst_x", 4), ("dst_y", 4), ("z", 3), ("n", 16)],
        ),
        0x1A => (
            "VSETI",
            vec![("z1", 1), ("sreg", 4), ("z", 21), ("to_k", 1)],
        ),
        0x1B => (
            "VLOOP",
            vec![("z1", 6), ("sreg", 4), ("body", 10), ("z", 7)],
        ),
        0x1C => ("VBAR", vec![("z1", 16), ("q", 1), ("z", 10)]),
        0x1D => (
            "VFILL",
            vec![("rel", 1), ("z1", 2), ("ad", 3), ("z", 7), ("off", 14)],
        ),
        0x1E => (
            "VDRAIN",
            vec![
                ("rel", 1),
                ("signal", 1),
                ("node", 1),
                ("ad", 3),
                ("dst_x", 4),
                ("dst_y", 4),
                ("buf_id", 4),
                ("off", 9),
            ],
        ),
        0x1F => ("VHALT", vec![("z", 27)]),
        _ => return None,
    })
}

/// `table`'s fields of `word`, MSB first from bit `top`; a negative width is
/// a signed field.
pub fn fields_of(word: u128, top: u32, table: &[(&'static str, i32)]) -> Vec<(&'static str, i128)> {
    let mut out = Vec::new();
    let mut at = top + 1;
    for (name, width) in table {
        let w = width.unsigned_abs();
        at -= w;
        let mut v = ((word >> at) & ((1u128 << w) - 1)) as i128;
        if *width < 0 && v >> (w - 1) != 0 {
            v -= 1 << w;
        }
        out.push((*name, v));
    }
    out
}

/// `NAME k=v ...` of the nonzero fields; `hexes` print in hex.
pub fn show(name: &str, pairs: &[(&str, i128)], hexes: &[&str]) -> String {
    let mut parts = vec![name.to_string()];
    for (k, v) in pairs {
        if *v != 0 {
            if hexes.contains(k) {
                parts.push(format!("{k}={v:#x}"));
            } else {
                parts.push(format!("{k}={v}"));
            }
        }
    }
    parts.join(" ")
}

/// One vector-core instruction word (bits past 32 as `hi=`).
pub fn core_word(w: u64) -> String {
    let lo = (w & 0xFFFF_FFFF) as u128;
    let hi = w >> 32;
    let op = (lo >> 27) as u64;
    let hi_s = if hi != 0 {
        format!(" hi={hi:#x}")
    } else {
        String::new()
    };
    let (name, table): (String, Fields) = if (op as usize) < MATH_OPS.len() {
        (MATH_OPS[op as usize].to_string(), MATH_F.to_vec())
    } else if op == 0x13 {
        let sel = ((lo >> 23) & 0xF) as u64;
        let (name, table) = match cfg_f(sel) {
            Some((n, t)) => (n.to_string(), t),
            None => (format!("VCFG{sel}"), vec![("payload", 23)]),
        };
        let mut t = vec![("sel", 4)];
        t.extend(table);
        let got: Vec<(&str, i128)> = fields_of(lo, 26, &t)
            .into_iter()
            .filter(|(k, _)| *k != "sel")
            .collect();
        return show(&name, &got, &[]) + &hi_s;
    } else if op == 0x16 {
        let waiter = (lo >> 24) & 7;
        let on = (lo >> 21) & 7;
        if waiter == 5 {
            ("VMARK".into(), SYNC_F.to_vec())
        } else if on == 7 {
            ("VWAIT".into(), WAIT_F.to_vec())
        } else {
            ("VSYNC".into(), SYNC_F.to_vec())
        }
    } else {
        match core_f(op) {
            Some((n, t)) => (n.to_string(), t),
            None => ("?".into(), Vec::new()),
        }
    };
    show(&name, &fields_of(lo, 26, &table), &[]) + &hi_s
}

/// Words of an image, a VSETI's immediate shown as `imm=`.
pub fn core_words(words: &[u64]) -> Vec<String> {
    let mut out = Vec::new();
    let mut k = 0;
    while k < words.len() {
        let w = words[k];
        out.push(core_word(w));
        if (w & 0xFFFF_FFFF) >> 27 == 0x1A && k + 1 < words.len() {
            out.push(format!("imm={:#x}", words[k + 1]));
            k += 1;
        }
        k += 1;
    }
    out
}

/// `v` as a signed `bits`-bit field.
pub fn signed(v: i128, bits: u32) -> i128 {
    if v >> (bits - 1) != 0 {
        v - (1 << bits)
    } else {
        v
    }
}

// ------------------------------------------------------- program and package

/// A 256-bit value as Python's `hex()`.
pub fn hex256(w: &W256) -> String {
    let mut s = String::new();
    for limb in w.0.iter().rev() {
        s.push_str(&format!("{limb:016x}"));
    }
    let t = s.trim_start_matches('0');
    format!("0x{}", if t.is_empty() { "0" } else { t })
}

fn bits(w: &W256, lo: u32, width: u32) -> u128 {
    if width <= 64 {
        return w.get(lo, width) as u128;
    }
    w.get(lo, 64) as u128 | (w.get(lo + 64, width - 64) as u128) << 64
}

fn fields_w(w: &W256, top: u32, table: &[(&'static str, i32)]) -> Vec<(&'static str, i128)> {
    let mut out = Vec::new();
    let mut at = top + 1;
    for (name, width) in table {
        let wd = width.unsigned_abs();
        at -= wd;
        out.push((*name, bits(w, at, wd) as i128));
    }
    out
}

/// A vector core's CU payload: IMEM, DESC or RUN.
pub fn vc_payload(p: &W256) -> Value {
    let op = p.get(252, 4);
    match op {
        1 => {
            let addr = p.get(243, 9) | p.get(242, 1) << 9;
            json!(["IMEM", addr, core_word(p.get(0, 40))])
        }
        2 => {
            let ad = p.get(249, 3);
            let fld = p.get(246, 3);
            let value = p.get(212, 34) | p.get(63, 6) << 34;
            if fld == 0 {
                json!(["DESC", ad, 0, format!("{value:#x}")])
            } else {
                let stride = signed(((value >> 16) & 0x3FFFF) as i128, 18) as i64;
                json!(["DESC", ad, fld, stride, value & 0xFFFF])
            }
        }
        3 => json!(["RUN", p.get(243, 9)]),
        _ => json!(["?", hex256(p)]),
    }
}

const MG_F: &[(&str, i32)] = &[
    ("addr", 34),
    ("n", 16),
    ("sel", 1),
    ("acc", 1),
    ("gm", 8),
    ("gn", 8),
    ("nk", 8),
    ("anchor", 8),
    ("peers", 24),
    ("npeer", 2),
    ("recv", 1),
    ("eoff", 8),
    ("aoff", 8),
    ("boff", 8),
    ("emit", 1),
    ("fuse", 1),
    ("abank", 1),
    ("bbank", 1),
    ("fbank", 1),
    ("dnode", 1),
    ("dst_x", 4),
    ("dst_y", 4),
    ("dbuf", 8),
    ("dflags", 8),
    ("dack_y", 4),
    ("dack_x", 4),
    ("dmesh", 2),
    ("dfin", 8),
    ("addr_hi", 6),
    ("credit", 1),
];

/// A cluster's CU payload.
pub fn mg_payload(p: &W256) -> String {
    let mut got = fields_w(p, 251, MG_F);
    let hi = got.iter().find(|(k, _)| *k == "addr_hi").map_or(0, |f| f.1);
    got.retain(|(k, _)| *k != "addr_hi");
    got[0].1 |= hi << 34;
    let name = match p.get(252, 4) {
        1 => "FILL",
        2 => "GEMM",
        3 => "DRAIN",
        4 => "POST",
        _ => "?",
    };
    show(name, &got, &["addr", "peers"])
}

/// One mover register write.
pub fn mover_write(reg: u64, v: u64) -> String {
    let s = |x: u64, b: u32| signed(x as i128, b);
    let i = |x: u64| x as i128;
    match reg {
        0x00 => show(
            "CTRL",
            &[
                ("mode", i(v & 7)),
                ("ew", i((v >> 3) & 3)),
                ("flags", i((v >> 8) & 0xFF)),
                ("go", i((v >> 16) & 1)),
                ("fp", i((v >> 17) & 1)),
            ],
            &["flags"],
        ),
        0x10 => show(
            "HDR",
            &[
                ("sel", i(v & 1)),
                ("base", i((v >> 4) & ((1 << 40) - 1))),
                ("ndim", i((v >> 44) & 7)),
                ("xid", i((v >> 47) & 0xF)),
                ("xmode", i((v >> 55) & 0xF)),
            ],
            &["base"],
        ),
        0x18 => show(
            "DIM",
            &[
                ("sel", i(v & 1)),
                ("level", i((v >> 1) & 7)),
                ("count", i((v >> 4) & 0xFFFF)),
                ("stride", s(v >> 20, 32)),
            ],
            &[],
        ),
        0x20 => show("AXIS", &[("axis", i(v & 3)), ("astep", s(v >> 2, 16))], &[]),
        0x28 => show(
            "WIN",
            &[
                ("sel", i(v & 1)),
                ("axis", i((v >> 1) & 1)),
                ("base", s((v >> 2) & 0xFFFF, 16)),
                ("extent", i(v >> 18)),
            ],
            &[],
        ),
        0x30 => show(
            "INDEX",
            &[("base", i(v & ((1 << 40) - 1))), ("count", i(v >> 40))],
            &["base"],
        ),
        0x50 => show(
            "ROW",
            &[("pitch", i(v & 0xFFFF_FFFF)), ("words", i(v >> 32))],
            &[],
        ),
        0x38 => format!("SEED {v:#x}"),
        0x40 => format!("IMM {v:#x}"),
        r => format!("R{r:#x} {v:#x}"),
    }
}

/// Mover writes, the padding pair dropped.
pub fn mover_writes(writes: &[(u64, u64)]) -> Vec<String> {
    writes
        .iter()
        .filter(|(r, _)| *r != u64::MAX)
        .map(|(r, v)| mover_write(*r, *v))
        .collect()
}

/// A unit's words decoded by its type: VC, MG, else hex.
pub fn words_of(kind: &str, words: &[W256]) -> Value {
    match kind {
        "VC" => {
            let mut out: Vec<Value> = Vec::new();
            for w in words {
                let mut got = vc_payload(w);
                let seti = out.last().is_some_and(|l| {
                    l[0] == "IMEM" && l[2].as_str().is_some_and(|s| s.starts_with("VSETI"))
                });
                if got[0] == "IMEM" && seti {
                    got = json!(["IMEM", got[1], format!("imm={:#x}", w.get(0, 40))]);
                }
                out.push(got);
            }
            Value::Array(out)
        }
        "MG" => json!(words.iter().map(mg_payload).collect::<Vec<_>>()),
        _ => json!(words.iter().map(hex256).collect::<Vec<_>>()),
    }
}

fn alu_name(a: Alu) -> &'static str {
    [
        "MOV", "ADD", "SUB", "MUL", "AND", "OR", "XOR", "SHL", "SHR", "MIN", "MAX", "DIVU", "REMU",
    ][a as usize]
}

fn cond_name(c: Cond) -> &'static str {
    ["ALWAYS", "EQ", "NE", "LTU", "GEU", "LT", "GE"][c as usize]
}

fn patches(ps: &[Patch]) -> Value {
    json!(ps
        .iter()
        .map(|p| json!([
            p.word,
            p.segments
                .iter()
                .map(|s| json!([s.0, s.1, s.2]))
                .collect::<Vec<_>>(),
            p.reg,
            p.konst as i64
        ]))
        .collect::<Vec<_>>())
}

fn with_patches(mut v: Vec<Value>, ps: &[Patch]) -> Value {
    if !ps.is_empty() {
        v.push(patches(ps));
    }
    Value::Array(v)
}

/// One program step as the runner writes it; `kind` names a coordinate's
/// unit type.
pub fn prog_step(kind: &dyn Fn((u32, u32)) -> String, s: &PStmt) -> Value {
    let xy = |c: &(u32, u32)| json!([c.0, c.1]);
    match s {
        PStmt::Send {
            coord,
            words,
            patches,
        } => with_patches(
            vec![json!("send"), xy(coord), words_of(&kind(*coord), words)],
            patches,
        ),
        PStmt::SendAdvance {
            coord,
            words,
            patches,
        } => with_patches(
            vec![
                json!("send_advance"),
                xy(coord),
                words_of(&kind(*coord), words),
            ],
            patches,
        ),
        PStmt::Repeat {
            coord,
            words,
            repeats,
            increments,
        } => json!([
            "repeat",
            xy(coord),
            words_of(&kind(*coord), words),
            repeats,
            increments
        ]),
        PStmt::MoveAdvance { writes, patches } => with_patches(
            vec![json!("move_advance"), json!(mover_writes(writes))],
            patches,
        ),
        PStmt::Move { writes, patches } => {
            with_patches(vec![json!("move"), json!(mover_writes(writes))], patches)
        }
        PStmt::Post {
            writes,
            token,
            patches,
        } => with_patches(
            vec![json!("post"), json!(mover_writes(writes)), json!(token)],
            patches,
        ),
        PStmt::Mark { coord, token } => json!(["mark", xy(coord), token]),
        PStmt::Wait { coord, token } => json!(["wait", xy(coord), token]),
        PStmt::Alu { rd, op, ra, b, imm } => json!(["alu", rd, alu_name(*op), ra, b, imm]),
        PStmt::Jump {
            name,
            cond,
            ra,
            b,
            imm,
        } => json!(["jump", name, cond_name(*cond), ra, b, imm]),
        PStmt::Store { pairs, from_regs } => json!([
            "store",
            pairs.iter().map(|p| json!([p.0, p.1])).collect::<Vec<_>>(),
            from_regs
        ]),
        PStmt::Ctrl { pairs, from_regs } => json!([
            "ctrl",
            pairs.iter().map(|p| json!([p.0, p.1])).collect::<Vec<_>>(),
            from_regs
        ]),
        PStmt::Push {
            coord,
            descriptor,
            address,
            words,
            ack,
        } => {
            json!(["push", xy(coord), hex256(descriptor), address, words, ack])
        }
        PStmt::SendRaw {
            coord,
            flit_type,
            words,
            ack,
        } => json!([
            "send_raw",
            xy(coord),
            flit_type,
            words.iter().map(hex256).collect::<Vec<_>>(),
            ack
        ]),
        PStmt::Recv {
            address,
            count,
            coord,
        } => {
            json!(["recv", address, count, coord.map(|c| json!([c.0, c.1]))])
        }
        PStmt::Fetch {
            coord,
            addr,
            count,
            patches,
        } => with_patches(
            vec![json!("fetch"), xy(coord), json!(addr), json!(count)],
            patches,
        ),
        PStmt::Barrier => json!(["barrier"]),
        PStmt::Ring {
            mesh,
            slot,
            amount,
            fence,
        } => json!(["ring", mesh, slot, amount, fence]),
        PStmt::WaitBell { mesh, count } => json!(["wait_bell", mesh, count]),
        PStmt::WaitSlot {
            slot,
            count,
            consume,
        } => json!(["wait_slot", slot, count, consume]),
        PStmt::Load {
            reg,
            address,
            index,
            ctrl,
        } => json!(["load", reg, address, index, ctrl]),
        PStmt::Label(n) => json!(["label", n]),
        PStmt::AssertZero { reg, code } => json!(["assert_zero", reg, code]),
        PStmt::Signal { value32, value64 } => json!(["signal", value32, value64]),
        PStmt::Settle(n) => json!(["settle", n]),
        PStmt::WaitMoves(t) => json!(["wait_moves", t]),
    }
}

/// Every step of a program.
pub fn prog_steps(kind: &dyn Fn((u32, u32)) -> String, p: &Program) -> Value {
    Value::Array(p.steps.iter().map(|s| prog_step(kind, s)).collect())
}

fn type_name(code: u64) -> String {
    [(code >> 8) & 0xFF, code & 0xFF]
        .iter()
        .map(|b| {
            if *b < 0x80 {
                *b as u8 as char
            } else {
                '\u{FFFD}'
            }
        })
        .collect()
}

/// One L0 step of a package, its payloads decoded.
pub fn pkg_step(pkg: &Package, s: &ktpu_l0::Step) -> Value {
    let op = s.op.name();
    let unit = pkg.units.get(s.unit as usize);
    let coord = || unit.map_or(Value::Null, |u| json!([u.x, u.y]));
    let kind = || {
        unit.map(|u| type_name(u.type_code))
            .unwrap_or_else(|| "?".into())
    };
    let payloads = |a: u64, n: u64| {
        let (a, n) = (a as usize, n as usize);
        &pkg.payloads[a.min(pkg.payloads.len())..(a + n).min(pkg.payloads.len())]
    };
    let name = |code: Option<&'static str>| code.map_or(Value::Null, |n| json!(n));
    match s.op {
        Op::Dispatch => json!([op, coord(), words_of(&kind(), payloads(s.arg, s.count))]),
        Op::Await if s.flags == 2 => json!([op, coord(), "sent"]),
        Op::Await => json!([op, coord(), s.count]),
        Op::Barrier => json!([op]),
        Op::Mover => {
            let mut pairs = Vec::new();
            for w in payloads(s.arg, s.count) {
                for half in [0u32, 128] {
                    pairs.push((w.get(half, 64), w.get(half + 64, 64)));
                }
            }
            json!([op, mover_writes(&pairs), s.flags & 1])
        }
        Op::Mwait => json!([op, s.count]),
        Op::Fetch => json!([op, coord(), s.count, format!("{:#x}", s.arg)]),
        Op::Alu => json!([
            op,
            s.unit,
            name(Alu::from_code(s.count & 0xFF).map(alu_name)),
            (s.count >> 8) & 0xF
        ]),
        Op::Jump => json!([
            op,
            s.unit,
            name(Cond::from_code(s.count & 0xFF).map(cond_name)),
            (s.count >> 8) & 0xF
        ]),
        Op::Patch => json!([
            op,
            s.unit,
            s.count,
            s.arg & 0xFF,
            (s.arg >> 8) & 0xFF,
            (s.arg >> 16) & 0xFF
        ]),
        Op::Ring => json!([op, s.unit, s.count, s.arg & 0xFFFF, (s.arg >> 16) & 1]),
        _ => json!([op, s.unit, s.count, s.arg, s.flags]),
    }
}

/// Every step of a package.
pub fn package_json(pkg: &Package) -> Value {
    Value::Array(pkg.steps.iter().map(|s| pkg_step(pkg, s)).collect())
}

fn opt(t: &Option<Box<Term>>) -> Value {
    t.as_ref().map(|x| term(x)).unwrap_or(Value::Null)
}

fn list(ts: &[Term]) -> Value {
    Value::Array(ts.iter().map(term).collect())
}

/// A term as `[Kind, ...]`.
pub fn term(t: &Term) -> Value {
    match t {
        Term::Name(s) => json!(["Name", s]),
        Term::Int(v, s) => {
            let style = match s {
                IntStyle::Dec => "dec",
                IntStyle::Hex => "hex",
                IntStyle::Size => "size",
            };
            json!(["Int", *v as i64, style])
        }
        Term::Float(v) => json!(["Float", py_float(*v)]),
        Term::Dims(vs) => json!(["Dims", vs.iter().map(|v| *v as i64).collect::<Vec<_>>()]),
        Term::Str(s) => json!(["Str", s]),
        Term::Neg(x) => json!(["Neg", term(x)]),
        Term::Not(x) => json!(["Not", term(x)]),
        Term::Offset(a, b) => json!(["Offset", term(a), term(b)]),
        Term::BinOp(op, a, b) => json!(["BinOp", op.to_string(), term(a), term(b)]),
        Term::Pos(x) => json!(["Pos", term(x)]),
        Term::Placed(x, s) => json!(["Placed", term(x), term(s)]),
        Term::Compare(op, a, b) => json!(["Compare", op, term(a), term(b)]),
        Term::Computed(p, i, s) => json!([
            "Computed",
            p,
            term(i),
            s.as_ref().map(|x| list(x)).unwrap_or(Value::Null)
        ]),
        Term::AddAssign(a, b) => json!(["AddAssign", term(a), term(b)]),
        Term::Span(a, b) => json!(["Span", term(a), term(b)]),
        Term::Typed(n, t) => json!(["Typed", n, term(t)]),
        Term::Kw(n, v) => json!(["Kw", n, term(v)]),
        Term::Call(n, a) => json!(["Call", n, list(a)]),
        Term::Slice(lo, hi) => json!(["Slice", opt(lo), opt(hi)]),
        Term::NewAxis => json!(["NewAxis"]),
        Term::View(n, s) => json!(["View", n, list(s)]),
        Term::Tuple(items) => json!(["Tuple", list(items)]),
        Term::Assign(a, b) => json!(["Assign", term(a), term(b)]),
        Term::Member(a, b) => json!(["Member", term(a), term(b)]),
        Term::Arrow(s) => json!(["Arrow", s]),
    }
}

/// A statement as `{op, args, target?, annot?, commas?, body?}`.
pub fn stmt(st: &Stmt) -> Value {
    let mut out = serde_json::Map::new();
    out.insert("op".into(), json!(st.op));
    out.insert("args".into(), list(&st.args));
    if let Some(t) = &st.target {
        let v = match t {
            Term::Name(n) => json!(n),
            other => term(other),
        };
        out.insert("target".into(), v);
    }
    if let Some(a) = &st.annot {
        out.insert("annot".into(), term(a));
    }
    if st.commas {
        out.insert("commas".into(), json!(true));
    }
    if !st.body.is_empty() {
        out.insert(
            "body".into(),
            Value::Array(st.body.iter().map(stmt).collect()),
        );
    }
    Value::Object(out)
}
