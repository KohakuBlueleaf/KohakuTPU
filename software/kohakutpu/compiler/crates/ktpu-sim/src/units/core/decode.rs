//! Instruction memory as instructions: a word's fields by the ISA's formats.

use ktpu_isa::unit::vector::VecIsa;
use ktpu_isa::Decoded;

use super::inst::*;
use crate::units::link::{Place, Target};
use crate::R;

fn get(f: &Decoded, name: &str) -> i64 {
    f.iter().find(|(n, _)| *n == name).map_or(0, |(_, v)| *v)
}

fn chunks(f: &Decoded) -> [(u32, u32); 4] {
    [
        ("a_base", "a_stride"),
        ("b_base", "b_stride"),
        ("c_base", "c_stride"),
        ("d_base", "d_stride"),
    ]
    .map(|(base, stride)| (get(f, base) as u32, get(f, stride) as u32))
}

fn source(sel: i64, reg: i64) -> R<Src> {
    match sel {
        0 => Ok(Src::V(reg as u32)),
        1 => Ok(Src::S(reg as u32)),
        3 => Ok(Src::K(reg as u32)),
        _ => Err("a source selector of 2 faults F_SRC".into()),
    }
}

fn waiter(code: i64) -> R<Waiter> {
    match code {
        0 => Ok(Waiter::Fe),
        1 => Ok(Waiter::Unpack),
        2 => Ok(Waiter::Math),
        3 => Ok(Waiter::Pack),
        4 => Ok(Waiter::Fill),
        6 => Ok(Waiter::Drain),
        _ => Err(format!("waiter {code} faults F_SYNC")),
    }
}

/// One word, the word after it taken by a VSETI; relative counts in words.
fn word(isa: &VecIsa, w: u64, next: u64) -> R<Inst> {
    let (name, f) = isa
        .decode_word_reference(w)
        .ok_or_else(|| format!("{w:#x} is no core word"))?;
    let g = |n: &str| get(&f, n);
    Ok(match name {
        "CHK" => Inst::Config(Config::Chk(chunks(&f))),
        "CHKVL" => Inst::Config(Config::Chkvl(g("vl1") as u32 + 1, chunks(&f))),
        "XB" => Inst::Config(Config::Xb {
            xm: g("xm") as u32,
            xk: g("xk") as u32,
            sh: g("sh") as u32,
            xc: g("xc") != 0,
            pm: g("pm") as u32,
            pr: g("pr") as u32,
        }),
        "UPTR" | "UINC" | "PPTR" | "PINC" => Inst::Ptr {
            reg: match name {
                "UPTR" => Ptr::Uptr,
                "UINC" => Ptr::Uinc,
                "PPTR" => Ptr::Pptr,
                _ => Ptr::Pinc,
            },
            value: g("ptr") as u32,
        },
        "USTR" => Inst::Config(Config::Ustr(g("s0"), g("s1"))),
        "PSTR" => Inst::Config(Config::Pstr(g("s0"), g("s1"))),
        "GETSTK" => Inst::Getstk {
            sreg: g("sreg") as u32,
        },
        "AINC" => Inst::Ainc {
            ad: g("ad") as u32,
            inc: g("inc"),
        },
        "VUNPK" | "VPACK" => {
            let pack = name == "VPACK";
            let (n, cb) = (g("n1") as u32 + 1, g("cb") as u32);
            let (mode, cb) = match g("mode") {
                0 => (Mode::F16, cb),
                1 => (Mode::Gt4, cb),
                2 => (Mode::F32, cb),
                _ if pack => (Mode::Mx7(cb != 0), 0),
                _ => return Err("an MX7 unpack faults F_MODE".into()),
            };
            Inst::Walk {
                pack,
                reg: g("reg") as u32,
                off: g("off") as u32,
                n,
                mode,
                cb,
                rel: g("rel") != 0,
                stride: None,
            }
        }
        "VFILL" => Inst::Fill {
            ad: g("ad") as u32,
            off: g("off") as u32,
            rel: g("rel") != 0,
        },
        "VDRAIN" => Inst::Drain {
            ad: g("ad") as u32,
            off: g("off") as u32,
            rel: g("rel") != 0,
            peer: (g("node") != 0).then(|| Peer {
                to: Target {
                    mesh: None,
                    place: Place::At(g("dst_x") as u32, g("dst_y") as u32),
                },
                buf: g("buf_id") as u32,
            }),
        },
        "VSYNC" => Inst::Sync {
            waiter: waiter(g("waiter"))?,
            on: if g("marked") != 0 {
                On::Mark(g("mark") as u32)
            } else {
                On::Kind {
                    kind: g("on") as usize,
                    slack: g("slack"),
                }
            },
        },
        "VMARK" => Inst::Mark {
            mark: g("mark") as u32,
            kind: g("on") as usize,
            slack: g("slack"),
        },
        "VSETI" => Inst::Seti {
            sreg: g("sreg") as u32,
            to_k: g("to_k") != 0,
            value: (next & 0xFF_FFFF) as u32,
        },
        "VSETVL" => Inst::Setvl {
            sreg: g("sreg") as u32,
        },
        "VLOOP" => Inst::Loop {
            sreg: g("sreg") as u32,
            body: g("body") as u32,
        },
        "VSKIPZ" => Inst::Skipz {
            sreg: g("sreg") as u32,
            n: g("skip") as u32,
        },
        "VBAR" => Inst::Bar,
        "VHALT" => Inst::Halt,
        "VWAIT" if g("counter") == 0 => Inst::Landed {
            waiter: waiter(g("waiter"))?,
            n: g("n") as u32,
        },
        "VPOST" => Inst::Post {
            to: Target {
                mesh: None,
                place: Place::At(g("dst_x") as u32, g("dst_y") as u32),
            },
            n: g("n") as usize,
        },
        "VWAIT" | "VCFG" => return Err(format!("{name} in this simulator")),
        math => Inst::Math {
            op: MathOp::parse(&math.to_lowercase()).ok_or_else(|| format!("no word {math}"))?,
            vd: g("vd") as u32,
            a: source(g("sa"), g("va"))?,
            b: source(g("sb"), g("vb"))?,
            c: source(g("sc"), g("vc"))?,
            om: g("om") != 0,
            cfg: None,
        },
    })
}

/// The program from `pc`, past its last forward reach to a VHALT, with loop
/// and skip counts in instructions (a VSETI is two words, ISA.VC.165).
pub fn program(isa: &VecIsa, imem: &[u64], pc: usize) -> R<Vec<Inst>> {
    decode_program(imem, pc, |_, w, next| word(isa, w, next))
}

/// One decoded instruction per IMEM slot; VSETI also depends on its next word.
#[derive(Default)]
pub(crate) struct Cache {
    slots: Vec<Option<(u64, u64, Inst)>>,
}

impl Cache {
    pub(crate) fn program(&mut self, isa: &VecIsa, imem: &[u64], pc: usize) -> R<Vec<Inst>> {
        self.slots.resize_with(imem.len(), || None);
        decode_program(imem, pc, |at, w, next| {
            let slot = &mut self.slots[at];
            if let Some((old, following, inst)) = slot {
                if *old == w && *following == next {
                    return Ok(inst.clone());
                }
            }
            let inst = word(isa, w, next)?;
            *slot = Some((w, next, inst.clone()));
            Ok(inst)
        })
    }
}

fn decode_program(
    imem: &[u64],
    pc: usize,
    mut decode: impl FnMut(usize, u64, u64) -> R<Inst>,
) -> R<Vec<Inst>> {
    let mut out = Vec::new();
    let mut index = vec![usize::MAX; imem.len() + 1];
    let mut reach = pc;
    let mut at = pc;
    let mut relative = Vec::new();
    loop {
        let w = *imem
            .get(at)
            .ok_or_else(|| format!("pc {at} past instruction memory"))?;
        let next = imem.get(at + 1).copied().unwrap_or(0);
        let inst = decode(at, w, next)?;
        index[at] = out.len();
        let size = if matches!(inst, Inst::Seti { .. }) {
            2
        } else {
            1
        };
        if let Inst::Loop { body: n, .. } | Inst::Skipz { n, .. } = inst {
            let end = at + 1 + n as usize;
            reach = reach.max(end);
            relative.push((out.len(), end));
        }
        let halt = inst == Inst::Halt;
        out.push(inst);
        at += size;
        if halt && at > reach {
            break;
        }
    }
    index[at] = out.len();
    for (i, end) in relative {
        let n = (index[end] - i - 1) as u32;
        match &mut out[i] {
            Inst::Loop { body, .. } => *body = n,
            Inst::Skipz { n: s, .. } => *s = n,
            _ => unreachable!("a relative instruction"),
        }
    }
    Ok(out)
}
