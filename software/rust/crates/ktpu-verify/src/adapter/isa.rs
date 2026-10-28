//! Decoded unit words as the checker reads them (`ktpu_isa` decodes them),
//! and the vector core facts the model names.

/// A field name's lookup key: its length and first 7 bytes (names longer
/// than 7 bytes are compared in full on a key match).
#[inline]
fn key(name: &str) -> u64 {
    let b = name.as_bytes();
    let mut k = b.len() as u64 & 0xFF;
    for (i, &c) in b.iter().take(7).enumerate() {
        k |= u64::from(c) << (8 * (i + 1));
    }
    k
}

/// Decoded fields by name.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Fields(Vec<(u64, &'static str, i64)>, Vec<&'static str>);

impl From<ktpu_isa::Decoded> for Fields {
    fn from(d: ktpu_isa::Decoded) -> Fields {
        Fields(
            d.into_iter().map(|(n, v)| (key(n), n, v)).collect(),
            Vec::new(),
        )
    }
}

impl Fields {
    /// Remove data-only values from the checker representation. Access is a bug,
    /// not an implicit zero: this enforces the confinement contract structurally.
    pub fn without_data(mut self, names: &[&'static str]) -> Self {
        self.0.retain(|(_, n, _)| !names.contains(n));
        self.1.extend_from_slice(names);
        self
    }

    pub fn get(&self, name: &str) -> Option<i64> {
        assert!(
            !self.1.contains(&name),
            "checker read of data-only field {name}"
        );
        let k = key(name);
        self.0
            .iter()
            .find(|(fk, n, _)| *fk == k && (name.len() <= 7 || *n == name))
            .map(|&(_, _, v)| v)
    }

    /// A field the format has (0 when it has none).
    pub fn g(&self, name: &str) -> i64 {
        self.get(name).unwrap_or(0)
    }
}

/// VWAIT counters: granules landed, credits.
pub const RX: i64 = 0;
pub const CR: i64 = 1;

/// The core's VHALT word (op 0x1F), what a fetch past IMEM reads as.
pub const HALT_WORD: u64 = 0x1F << 27;

/// A vector core word's format (`isa/vector.md`), by its ISA name; the 19
/// math formats (`isa/core.py` OPS) first.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum VOp {
    Vmov,
    Vneg,
    Vabs,
    Vadd,
    Vsub,
    Vmul,
    Vfma,
    Vfnma,
    Vmax,
    Vmin,
    Vsel,
    Vcmplt,
    Vcmpgt,
    Vcmpeq,
    Vexp2,
    Vlog2,
    Vinv,
    Vrsqrt,
    Vexp2d,
    Chk,
    Chkvl,
    Xb,
    Uptr,
    Uinc,
    Pptr,
    Pinc,
    Ustr,
    Pstr,
    Getstk,
    Ainc,
    Vcfg,
    Vunpk,
    Vpack,
    Vmark,
    Vwait,
    Vsync,
    Vpost,
    Vskipz,
    Vsetvl,
    Vseti,
    Vloop,
    Vbar,
    Vhalt,
    Vfill,
    Vdrain,
}

/// Every format and its ISA name, in `VOp` order.
const VOPS: [(VOp, &str); 45] = [
    (VOp::Vmov, "VMOV"),
    (VOp::Vneg, "VNEG"),
    (VOp::Vabs, "VABS"),
    (VOp::Vadd, "VADD"),
    (VOp::Vsub, "VSUB"),
    (VOp::Vmul, "VMUL"),
    (VOp::Vfma, "VFMA"),
    (VOp::Vfnma, "VFNMA"),
    (VOp::Vmax, "VMAX"),
    (VOp::Vmin, "VMIN"),
    (VOp::Vsel, "VSEL"),
    (VOp::Vcmplt, "VCMPLT"),
    (VOp::Vcmpgt, "VCMPGT"),
    (VOp::Vcmpeq, "VCMPEQ"),
    (VOp::Vexp2, "VEXP2"),
    (VOp::Vlog2, "VLOG2"),
    (VOp::Vinv, "VINV"),
    (VOp::Vrsqrt, "VRSQRT"),
    (VOp::Vexp2d, "VEXP2D"),
    (VOp::Chk, "CHK"),
    (VOp::Chkvl, "CHKVL"),
    (VOp::Xb, "XB"),
    (VOp::Uptr, "UPTR"),
    (VOp::Uinc, "UINC"),
    (VOp::Pptr, "PPTR"),
    (VOp::Pinc, "PINC"),
    (VOp::Ustr, "USTR"),
    (VOp::Pstr, "PSTR"),
    (VOp::Getstk, "GETSTK"),
    (VOp::Ainc, "AINC"),
    (VOp::Vcfg, "VCFG"),
    (VOp::Vunpk, "VUNPK"),
    (VOp::Vpack, "VPACK"),
    (VOp::Vmark, "VMARK"),
    (VOp::Vwait, "VWAIT"),
    (VOp::Vsync, "VSYNC"),
    (VOp::Vpost, "VPOST"),
    (VOp::Vskipz, "VSKIPZ"),
    (VOp::Vsetvl, "VSETVL"),
    (VOp::Vseti, "VSETI"),
    (VOp::Vloop, "VLOOP"),
    (VOp::Vbar, "VBAR"),
    (VOp::Vhalt, "VHALT"),
    (VOp::Vfill, "VFILL"),
    (VOp::Vdrain, "VDRAIN"),
];

impl VOp {
    /// Every format, in order.
    pub fn all() -> impl Iterator<Item = VOp> {
        VOPS.iter().map(|&(op, _)| op)
    }

    /// Its ISA format name.
    pub fn name(self) -> &'static str {
        VOPS[self as usize].1
    }

    /// The format named `name` (None for a name no core format has).
    pub fn from_name(name: &str) -> Option<VOp> {
        VOPS.iter().find(|&&(_, n)| n == name).map(|&(op, _)| op)
    }

    /// A math format (`isa/core.py` OPS).
    pub fn is_math(self) -> bool {
        (self as usize) <= VOp::Vexp2d as usize
    }

    /// A compare (writes no register).
    pub fn is_cmp(self) -> bool {
        matches!(self, VOp::Vcmplt | VOp::Vcmpgt | VOp::Vcmpeq)
    }
}

impl std::fmt::Display for VOp {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.name())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Every vector core format the ISA builds is a `VOp`, and back; the
    /// math formats are exactly `is_math`; an unknown name is none.
    #[test]
    fn vop_names_are_the_isa_formats() {
        let m = ktpu_machine::Machine::named("multimesh_v9t6").unwrap();
        let isa = ktpu_isa::Isa::new(&m).unwrap();
        let formats: Vec<&str> = isa.vector.core.set.formats.iter().map(|f| f.name).collect();
        let maths: Vec<&str> = isa.vector.core.maths.iter().map(|(_, f)| f.name).collect();
        for name in &formats {
            let op = VOp::from_name(name).unwrap_or_else(|| panic!("no VOp for {name}"));
            assert_eq!(op.name(), *name);
            assert_eq!(op.is_math(), maths.contains(name), "{name}");
        }
        let mut all: Vec<&str> = VOp::all().map(VOp::name).collect();
        all.sort_unstable();
        let mut want = formats.clone();
        want.sort_unstable();
        want.dedup();
        assert_eq!(all, want, "VOp names are the ISA's core formats");
        for (i, op) in VOp::all().enumerate() {
            assert_eq!(op as usize, i);
            assert_eq!(VOp::from_name(op.name()), Some(op));
        }
        assert_eq!(VOp::from_name("VNOPE"), None);
        assert_eq!(VOp::from_name("vmov"), None);
    }

    /// A field is found by name whatever its length; a missing one is 0.
    #[test]
    fn fields_by_name() {
        let f = Fields::from(vec![
            ("q", 1),
            ("counter", 2),
            ("payload", 3),
            ("payloadx", 4),
            ("payloady", 5),
        ]);
        assert_eq!(f.g("q"), 1);
        assert_eq!(f.g("counter"), 2);
        assert_eq!(f.g("payload"), 3);
        assert_eq!(f.g("payloadx"), 4);
        assert_eq!(f.g("payloady"), 5);
        assert_eq!(f.get("payloadz"), None);
        assert_eq!(f.g("p"), 0);
    }
}
