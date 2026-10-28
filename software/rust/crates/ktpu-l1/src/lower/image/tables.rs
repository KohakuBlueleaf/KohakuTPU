//! Name tables of the image text (waiters, kinds, transfer modes) and the mutants that change them.

use super::*;

pub(super) const WAITERS: [(&str, u32); 6] = [
    ("fe", W_FE),
    ("unpack", W_U),
    ("math", W_M),
    ("pack", W_P),
    ("fill", W_A),
    ("drain", W_D),
];
pub(super) const KINDS: [(&str, u32); 7] = [
    ("fill", K_FILL),
    ("unpack", K_UNPACK),
    ("math", K_MATH),
    ("pack", K_PACK),
    ("drain", K_DRAIN),
    ("unpack1", K_UNPACK1),
    ("pack1", K_PACK1),
];
pub(super) const XMODES: [(&str, u32); 4] = [
    ("xor", XM_XOR),
    ("rot", XM_ROT),
    ("bcast", XM_BCAST),
    ("merge", XM_MERGE),
];
/// Config words a program may write out itself.
pub const EXPLICIT: [&str; 5] = ["chk", "chkvl", "xb", "ustr", "pstr"];

pub(super) fn waiters() -> [(&'static str, u32); 6] {
    let mut t = WAITERS;
    if on("l1.img/waiters") {
        t[1].1 = W_P;
        t[5].1 = W_A;
    }
    if on("isa.vector/sync_codes") {
        t[3].1 = W_U;
        t[1].1 = W_P;
        t[5].1 = W_A;
        t[4].1 = W_D;
    }
    t
}

pub(super) fn kinds() -> [(&'static str, u32); 7] {
    let mut t = KINDS;
    if on("isa.vector/sync_codes") {
        t[4].1 = K_FILL;
        t[0].1 = K_DRAIN;
        t[2].1 = K_PACK;
        t[3].1 = K_MATH;
    }
    t
}

pub(super) fn xmodes() -> [(&'static str, u32); 4] {
    let mut t = XMODES;
    if on("l1.img/xmodes") {
        t[0].1 = XM_ROT;
        t[1].1 = XM_XOR;
        t[3].1 = XM_BCAST;
    }
    t
}

/// `w` as a mutant's encoder call would make it.
pub(super) fn mutated(w: VWord) -> VWord {
    match w {
        VWord::Fill { ad, l1off, .. } if on("l1.img/dma-rel") => VWord::Fill {
            ad,
            l1off,
            rel: false,
        },
        VWord::Drain {
            ad,
            l1off,
            node,
            buf_id,
            signal,
            ..
        } if on("l1.img/dma-rel") => VWord::Drain {
            ad,
            l1off,
            rel: false,
            node,
            buf_id,
            signal,
        },
        VWord::Drain {
            ad,
            l1off,
            rel,
            node: Some(Coord(x, y)),
            buf_id,
            signal,
        } if on("l1.img/peer-xy") => VWord::Drain {
            ad,
            l1off,
            rel,
            node: Some(Coord(y, x)),
            buf_id,
            signal,
        },
        VWord::Bar { .. } if on("l1.img/vbar-q") => VWord::Bar { q: 1 },
        VWord::Halt if on("l1.img/halt") => VWord::Bar { q: 0 },
        VWord::Ainc { ad, inc } if on("l1.img/ainc-abs") => VWord::Ainc { ad, inc: inc.abs() },
        VWord::Getstk { sreg } if on("l1.img/getstk") => VWord::Getstk { sreg: sreg ^ 1 },
        VWord::Loop { sreg, body } if on("l1.img/loop-len") => VWord::Loop {
            sreg,
            body: body + 1,
        },
        w => w,
    }
}

pub(super) fn lookup(table: &[(&str, u32)], name: &str) -> Option<u32> {
    table.iter().find(|(n, _)| *n == name).map(|(_, v)| *v)
}

pub(super) fn names(table: &[(&str, u32)]) -> String {
    table.iter().map(|(n, _)| *n).collect::<Vec<_>>().join(", ")
}

pub(super) fn name_of(t: &Term) -> Option<&str> {
    t.as_name()
}
