//! Descriptors an image names: unit coordinates, descriptor words and a peer's base (L1.VC.003-.009).

use super::*;

/// A unit's `(x, y)`: written `(x, y)`, or a name resolved by `units`;
/// `local` refuses another mesh's.
pub fn coord(
    r: &Reader,
    st: &Stmt,
    t: &Term,
    units: Option<&dyn Units>,
    local: bool,
) -> Result<(u32, u32)> {
    if let Term::Tuple(items) = t {
        if let [Term::Int(x, _), Term::Int(y, _)] = items.as_slice() {
            return Ok((*x as u32, *y as u32));
        }
        return Err(r.fail(
            st,
            "L1.VC.009",
            format!("a coordinate is (x, y), got {}", fmt(t)),
        ));
    }
    let Some(units) = units else {
        return Err(r.fail(
            st,
            "L1.VC.009",
            format!("{}: unit names need the target; write (x, y)", fmt(t)),
        ));
    };
    let (mesh, xy) = units.resolve(st, t)?;
    if local && mesh != units.home() && !crate::mutant::on("l1.img/drain-any-mesh") {
        return Err(r.fail(
            st,
            "L1.IMG.043",
            format!(
                "{} is in mesh {mesh}: a drain names this mesh's memory port and carries the far unit in its descriptor (peer(OFF, mesh=, fin=))",
                fmt(t)
            ),
        ));
    }
    Ok(xy)
}

/// Descriptor fields keyed `(descriptor, field)`.
pub type Descs = BTreeMap<(u32, u32), u64>;

/// `desc aN = BASE walk=(STRIDE, BOUND) | walk=((S, B), ...)` as its fields;
/// all four dims are written, an unused one as (0, 0) (L1.VC.003).
pub fn desc(r: &Reader, st: &Stmt, facts: &CoreFacts, units: Option<&dyn Units>) -> Result<Descs> {
    let mut got = fields(r, st, facts, units)?;
    if on("isa.vector/desc_dims") {
        got.retain(|(_, fld), _| *fld != 4);
    }
    Ok(got)
}

fn fields(r: &Reader, st: &Stmt, facts: &CoreFacts, units: Option<&dyn Units>) -> Result<Descs> {
    if on("l1.img/desc-none") {
        return Ok(Descs::new());
    }
    let form = "desc aN = BASE walk=(STRIDE, BOUND) | walk=((S, B), ...)";
    let rest: Vec<&Term> = st
        .args
        .iter()
        .filter(|x| !matches!(x, Term::Assign(a, _) if a.as_name() == Some("walk")))
        .collect();
    let (lhs, base) = match rest.as_slice() {
        [Term::Assign(a, b)] => (a.as_ref(), b.as_ref()),
        _ => return Err(r.fail(st, "L1.VC.003", form)),
    };
    let ad = match lhs.as_name() {
        Some(n)
            if n.starts_with('a') && n.len() > 1 && n[1..].chars().all(|c| c.is_ascii_digit()) =>
        {
            n[1..].parse::<u64>().unwrap_or(u64::MAX)
        }
        _ => return Err(r.fail(st, "L1.VC.003", form)),
    };
    if ad >= facts.descriptors as u64 {
        return Err(r.fail(
            st,
            "L1.VC.003",
            format!("a{ad}: descriptors a0..a{}", facts.descriptors - 1),
        ));
    }
    let ad = ad as u32;
    let pairs: Vec<&Term> = match st.kw("walk") {
        None => Vec::new(),
        Some(Term::Tuple(items))
            if !items.is_empty() && items.iter().all(|i| matches!(i, Term::Tuple(_))) =>
        {
            items.iter().collect()
        }
        Some(w) => vec![w],
    };
    let mut dims = Vec::new();
    for p in pairs {
        match p {
            Term::Tuple(items) if items.len() == 2 => match (&items[0], &items[1]) {
                (Term::Int(s, _), Term::Int(b, _)) => dims.push((*s, *b)),
                _ => {
                    return Err(r.fail(
                        st,
                        "L1.VC.008",
                        format!("a dim is (STRIDE, BOUND) in constants, got {}", fmt(p)),
                    ))
                }
            },
            _ => {
                return Err(r.fail(
                    st,
                    "L1.VC.008",
                    format!("a dim is (STRIDE, BOUND) in constants, got {}", fmt(p)),
                ))
            }
        }
    }
    if dims.len() > facts.dims as usize {
        return Err(r.fail(
            st,
            "L1.VC.008",
            format!("{} dims: a descriptor holds {}", dims.len(), facts.dims),
        ));
    }
    let value = match base {
        Term::Call(n, _) if n == "peer" => peer_base(r, st, base, units)?,
        Term::Int(v, _) => *v as u64,
        _ => {
            return Err(r.fail(
                st,
                "L1.VC.008",
                format!(
                    "a descriptor's base is a constant address, got {}",
                    fmt(base)
                ),
            ))
        }
    };
    let mut out = Descs::new();
    out.insert((ad, 0), value);
    for i in 0..facts.dims as usize {
        let (s, b) = dims.get(i).copied().unwrap_or((0, 0));
        out.insert((ad, i as u32 + 1), dim(s as i64, b as u32)?);
    }
    if on("l1.vc/desc-given-dims") {
        out.retain(|k, v| k.1 == 0 || *v != 0);
    }
    Ok(out)
}

/// A far peer drain cut in two: its first `first` words go on the descriptor
/// itself, the last word on `spare` (L1.VC.010).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Split {
    pub spare: u32,
    pub first: u32,
}

/// Splits by the descriptor they cut.
pub type Splits = BTreeMap<u32, Split>;

/// The descriptors whose far peer drain would end in a one-flit interlink
/// packet: descriptor flit plus `count` words with count a multiple of
/// `il.packet_beats`, on an interlink with silicon flaw F-002: its sender
/// reads a one-flit packet's only flit before its FIFO shows it, so such a
/// burst loses its last word and leaves it behind as the next burst's first
/// flit (L1.VC.010).
pub fn far_splits(r: &Reader, stmts: &[Stmt], descs: &Descs, facts: &CoreFacts) -> Result<Splits> {
    if !facts.il_tail {
        return Ok(Splits::new());
    }
    let mut drains = BTreeMap::new();
    peer_drains(stmts, &mut drains);
    let mut named = BTreeSet::new();
    super::images::stmt_words(stmts, &mut named);
    let mut used: BTreeSet<u32> = descs.keys().map(|(ad, _)| *ad).collect();
    used.extend(
        named
            .iter()
            .filter_map(|w| w.strip_prefix('a').and_then(|n| n.parse::<u32>().ok())),
    );
    let mut out = Splits::new();
    for (ad, st) in drains {
        let base = descs.get(&(ad, 0)).copied().unwrap_or(0);
        let count: u64 = (1..=facts.dims)
            .map(|f| (descs.get(&(ad, f)).copied().unwrap_or(0) & DIM_BOUND_MASK).max(1))
            .product();
        let far = (base >> 26) & 0xFF != 0;
        if !far || !count.is_multiple_of(facts.packet_beats as u64) {
            continue;
        }
        let Some(spare) = (0..facts.descriptors).rev().find(|d| !used.contains(d)) else {
            return Err(r.fail(
                &st,
                "L1.VC.010",
                format!(
                    "a{ad}'s far drain of {count} words splits onto a spare descriptor; a0..a{} are all named",
                    facts.descriptors - 1
                ),
            ));
        };
        used.insert(spare);
        out.insert(
            ad,
            Split {
                spare,
                first: count as u32 - 1,
            },
        );
    }
    Ok(out)
}

/// `descs` with every split applied: the cut descriptor walks its first
/// words, the spare the last one, at the granule after them.
pub fn split_descs(descs: &Descs, splits: &Splits, facts: &CoreFacts) -> Descs {
    let mut out = descs.clone();
    for (ad, s) in splits {
        let base = descs.get(&(*ad, 0)).copied().unwrap_or(0);
        let stride = descs.get(&(*ad, 1)).copied().unwrap_or(0) & !DIM_BOUND_MASK;
        let granule = ((base & 0xFFFF) + s.first as u64) & 0xFFFF;
        out.insert((s.spare, 0), (base & !0xFFFF) | granule);
        out.insert((*ad, 1), stride | s.first as u64);
        out.insert((s.spare, 1), stride | 1);
        for f in 2..=facts.dims {
            out.insert((*ad, f), 0);
            out.insert((s.spare, f), 0);
        }
    }
    out
}

/// Each descriptor a `vdrain ... to=` names, with its first such statement.
fn peer_drains(stmts: &[Stmt], out: &mut BTreeMap<u32, Stmt>) {
    for st in stmts {
        if st.op == "vdrain" && st.kw("to").is_some() {
            let ad = st
                .positional()
                .first()
                .and_then(|t| t.as_name())
                .and_then(|n| n.strip_prefix('a'))
                .and_then(|n| n.parse::<u32>().ok());
            if let Some(ad) = ad {
                out.entry(ad).or_insert_with(|| st.clone());
            }
        }
        peer_drains(&st.body, out);
    }
}

/// Whether a peer burst's emitted descriptor is remote and longer than a
/// packet. An unknown destination can be remote; unknown bounds can be long.
pub fn long_remote(ad: u32, descs: &Descs, facts: &CoreFacts) -> bool {
    if descs
        .get(&(ad, 0))
        .is_some_and(|base| (base >> 26) & 0xFF == 0)
    {
        return false;
    }
    let mut count = 1u64;
    for field in 1..=facts.dims {
        let Some(value) = descs.get(&(ad, field)) else {
            return true;
        };
        count = count.saturating_mul((value & DIM_BOUND_MASK).max(1));
    }
    count > facts.packet_beats as u64
}

const DIM_BOUND_MASK: u64 = (1 << ktpu_isa::unit::vector::DIM_BOUND_BITS) - 1;

/// `peer(OFF, ack=UNIT, mesh=M, fin=UNIT, count=1)`: a peer drain's
/// descriptor base (L1.VC.005, .009).
pub fn peer_base(r: &Reader, st: &Stmt, call: &Term, units: Option<&dyn Units>) -> Result<u64> {
    let Term::Call(_, args) = call else {
        unreachable!("peer_base of a call")
    };
    let pos: Vec<&Term> = args.iter().filter(|a| !matches!(a, Term::Kw(..))).collect();
    let mut kw: BTreeMap<&str, &Term> = BTreeMap::new();
    for a in args {
        if let Term::Kw(n, v) = a {
            kw.insert(n.as_str(), v.as_ref());
        }
    }
    let form = "peer(OFF, ack=UNIT, mesh=M, fin=UNIT, count=1)";
    if pos.len() != 1
        || pos[0].as_int().is_none()
        || kw
            .keys()
            .any(|k| !["ack", "mesh", "fin", "count"].contains(k))
    {
        return Err(r.fail(st, "L1.VC.005", form));
    }
    let yx = |t: &Term| -> Result<u64> {
        let (x, y) = coord(r, st, t, units, false)?;
        Ok(((y as u64 & 0xF) << 4) | (x as u64 & 0xF))
    };
    let mut out = (pos[0].as_int().unwrap() as u64) & 0xFFFF;
    if let Some(a) = kw.get("ack") {
        out |= yx(a)? << 16;
    }
    let mut mesh: Option<u64> = None;
    if let Some(m) = kw.get("mesh") {
        match m.as_int() {
            Some(v) if (0..4).contains(&v) => mesh = Some(v as u64),
            _ => return Err(r.fail(st, "L1.VC.009", format!("mesh= 0..3, got {}", fmt(m)))),
        }
    }
    if let Some(t) = kw.get("fin") {
        out |= yx(t)? << 26;
        if !matches!(t, Term::Tuple(_)) {
            let far = units.expect("coord resolved a name").resolve(st, t)?.0 as u64;
            if mesh.is_some_and(|m| m != far) {
                return Err(r.fail(
                    st,
                    "L1.VC.009",
                    format!("mesh={} but fin= is in mesh {far}", mesh.unwrap()),
                ));
            }
            mesh = Some(far);
        }
    }
    if let Some(m) = mesh {
        out |= m << 24;
    }
    if let Some(c) = kw.get("count") {
        let v = match c {
            Term::Int(v @ (0 | 1), crate::text::IntStyle::Dec) => *v as u64,
            _ => return Err(r.fail(st, "L1.VC.005", format!("count= 0 or 1, got {}", fmt(c)))),
        };
        out |= v << 34;
    }
    if on("l1.vc/peer-count") {
        out &= !(1 << 34);
    }
    Ok(out)
}
