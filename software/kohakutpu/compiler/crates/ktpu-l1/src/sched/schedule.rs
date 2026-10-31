//! The image scheduler (L1.SCHED.001-.006): each run of consecutive math,
//! unpack and pack statements of an image is list-scheduled under the issue
//! model (`sched::model`, the timing module's, RUL.063); the reordered image
//! replaces the written one only where it fits the image room and the core's
//! timing (`sched::timing`) of the whole image is faster.

use std::collections::{BTreeSet, HashMap};

use ktpu_machine::Machine;

use super::model::Ruled;
use super::timing;
use crate::error::Result;
use crate::lower::image::{words_of, IMEM_ROOM};
use crate::mutant;
use crate::text::{emit_at, fmt, read, Definition, Expander, Module, Stmt, Term};

/// A reordered run is kept only when the model times it this much faster.
pub const MIN_GAIN: f64 = 0.01;

/// The prices of a configuration word, in cycles, tried in turn until an
/// image's order fits (L1.SCHED.007); past the top the written order stands.
const PRICES: [i64; 10] = [0, 1, 2, 3, 4, 6, 8, 12, 16, 24];

const MOVABLE: [&str; 2] = ["vunpk", "vpack"];

/// Image statements that are not math and stay in place.
const FIXED: [&str; 10] = [
    "vfill", "vdrain", "vsync", "vwait", "vpost", "vmark", "vbar", "vsetvl", "vcfg", "vseti",
];

/// Whether `st` is in a run the scheduler reorders (L1.SCHED.001).
pub fn movable(st: &Stmt) -> bool {
    if st.target.is_some() || !st.body.is_empty() {
        return false;
    }
    let fixed = FIXED.contains(&st.op.as_str())
        && !(st.op == "vsync" && mutant::on("l1.sched/sync-movable"));
    MOVABLE.contains(&st.op.as_str()) || (st.op.starts_with('v') && !fixed)
}

fn is_reg_name(s: &str) -> bool {
    let mut c = s.chars();
    matches!(c.next(), Some('v' | 's' | 'k' | 'p'))
        && s.len() > 1
        && s[1..].chars().all(|d| d.is_ascii_digit())
}

/// Every register name a term reads or writes.
fn regs(t: &Term, out: &mut BTreeSet<String>) {
    match t {
        Term::Name(n) if is_reg_name(n) => {
            out.insert(n.clone());
        }
        Term::View(n, _) if is_reg_name(n) => {
            out.insert(n.clone());
        }
        Term::Call(_, args) => args.iter().for_each(|a| regs(a, out)),
        _ => {}
    }
}

/// The L1 words an unpack reads or a pack writes (`chunks`, `vc.chunks`,
/// without `n=`), or None.
fn l1_words(st: &Stmt, chunks: i128) -> Option<BTreeSet<i128>> {
    for a in st.positional() {
        if let Term::View(n, sl) = a {
            if n == "l1" && !sl.is_empty() {
                let off = match &sl[0] {
                    Term::Int(v, _) => Some(*v),
                    Term::Slice(Some(lo), _) => lo.as_int(),
                    _ => None,
                }?;
                let n = st.kw("n").and_then(Term::as_int);
                if n.is_none() && mutant::on("l1.sched/l1-words") {
                    return Some([off].into());
                }
                return Some((off..off + n.unwrap_or(chunks)).collect());
            }
        }
    }
    None
}

/// A statement's engine (`u`, `m`, `p`), the registers it defines and uses,
/// and the L1 words it touches (writing them or not).
struct Info {
    eng: char,
    defs: BTreeSet<String>,
    uses: BTreeSet<String>,
    l1: Option<BTreeSet<i128>>,
    write: bool,
}

fn info(st: &Stmt, chunks: i128) -> Info {
    let pos = st.positional();
    let mut defs = BTreeSet::new();
    let mut uses = BTreeSet::new();
    if st.op == "vunpk" || st.op == "vpack" {
        let unpack = st.op == "vunpk";
        if let Some(p) = pos.first() {
            regs(p, if unpack { &mut defs } else { &mut uses });
        }
        return Info {
            eng: if unpack { 'u' } else { 'p' },
            defs,
            uses,
            l1: l1_words(st, chunks),
            write: !unpack,
        };
    }
    if let Some(p) = pos.first() {
        regs(p, &mut defs);
    }
    for a in pos.iter().skip(1) {
        regs(a, &mut uses);
    }
    let partial = matches!(pos.first(), Some(Term::View(..)));
    if partial || st.op.starts_with("vcmp") {
        uses.extend(defs.iter().cloned());
    }
    for (k, v) in st.kwargs() {
        if k == "if" || k == "unless" {
            regs(v, &mut uses);
        }
    }
    Info {
        eng: 'm',
        defs,
        uses,
        l1: None,
        write: false,
    }
}

/// Each statement's predecessors in `run` (L1.SCHED.002): a register one
/// defines and the other uses or defines; L1 words both touch, one writing;
/// two unpack / pack statements either of whose words is unknown. `chunks`:
/// the core's `vc.chunks`, the words an unpack or pack without `n=` moves.
pub fn preds(run: &[Stmt], chunks: u32) -> Vec<BTreeSet<usize>> {
    let info: Vec<Info> = run.iter().map(|st| info(st, chunks as i128)).collect();
    let mut preds: Vec<BTreeSet<usize>> = vec![BTreeSet::new(); run.len()];
    for (j, b) in info.iter().enumerate() {
        for (i, a) in info.iter().enumerate().take(j) {
            let reg = !a.defs.is_disjoint(&b.uses)
                || !a.defs.is_disjoint(&b.defs)
                || !a.uses.is_disjoint(&b.defs);
            let l1 = match (&a.l1, &b.l1) {
                (Some(x), Some(y)) => !x.is_disjoint(y) && (a.write || b.write),
                _ => false,
            };
            let whole = (a.l1.is_none() || b.l1.is_none()) && a.eng != 'm' && b.eng != 'm';
            if reg || l1 || whole {
                preds[j].insert(i);
            }
        }
    }
    preds
}

/// Whether image `name` of `text` encodes within the image room.
fn fits(text: &str, name: &str, nparams: usize, vregs: u32, machine: &Machine) -> Result<bool> {
    let mut module = read(text, &format!("<{name}>"))?;
    module.vregs = vregs;
    let words = words_of(&module, name, nparams, machine);
    Ok(words.is_ok_and(|n| n as i64 <= IMEM_ROOM) || mutant::on("l1.sched/unfit"))
}

/// An image as the scheduler leaves it.
struct Scheduled {
    name: String,
    nparams: usize,
    text: String,
    /// The order of the first price decided it.
    first: bool,
    /// A reordering, known to fit; else the written order.
    tuned: bool,
}

/// Each image of `module` at `prices` (L1.SCHED.005, .007), expanded (macros,
/// `for`, `when`).
fn images(
    module: &Module,
    copies: u32,
    prices: &[i64],
    machine: &Machine,
) -> Result<Vec<Scheduled>> {
    let r = module.reader();
    let regs = module.vregs;
    let ruled = Ruled::new(&r, machine, regs, copies)?;
    let macros = module.macro_table();
    let fill = (
        machine.vc.fill_alone.0 as i64,
        machine.vc.fill_alone.1 as i64,
    );
    let image = |img: &Definition| -> Result<Scheduled> {
        let body = Expander::new(&macros, &r, true).stmts(&img.stmts, &HashMap::new())?;
        let head = format!("image {}({})\n", img.name, img.params.join(", "));
        let written = format!("{head}{}", emit_at(&body, 1, 48));
        let classed = body.iter().any(|s| s.kw("classed").is_some());
        let nparams = img.params.len();
        let left = |text, first, tuned| Scheduled {
            name: img.name.clone(),
            nparams,
            text,
            first,
            tuned,
        };
        for &price in prices {
            let tuned = format!("{head}{}", emit_at(&ruled.block_at(&body, price)?, 1, 48));
            let mut faster = tuned != written;
            if faster && !fits(&tuned, &img.name, nparams, regs, machine)? {
                continue;
            }
            if faster && !classed {
                let t = timing::cycles(&tuned, &img.name, regs, copies, fill, machine)?;
                let w = timing::cycles(&written, &img.name, regs, copies, fill, machine)?;
                faster = t < w;
            }
            return Ok(match faster {
                true => left(tuned, price == 0, true),
                false => left(written, price == 0, false),
            });
        }
        Ok(left(written, false, false))
    };
    module.images.values().map(image).collect()
}

/// Whether every image of `module` stands within the room at price 0, on a
/// core of `copies` register-file read copies.
pub fn whole(module: &Module, copies: u32, machine: &Machine) -> Result<bool> {
    for s in images(module, copies, &PRICES[..1], machine)? {
        let room = s.tuned || fits(&s.text, &s.name, s.nparams, module.vregs, machine)?;
        if !s.first || !room {
            return Ok(false);
        }
    }
    Ok(true)
}

/// `module` with every image scheduled ([`images`]); the result is read back
/// (L1.SCHED.005).
pub fn schedule_module(module: &Module, machine: &Machine) -> Result<Module> {
    let mut texts: Vec<String> = Vec::new();
    if module.target.is_some() {
        texts.push(format!("{}\n", module.target_line()));
    }
    let prices = match mutant::on("l1.sched/one-price") {
        true => &PRICES[..1],
        false => &PRICES[..],
    };
    let scheduled = images(module, module.vcopies, prices, machine)?;
    texts.extend(scheduled.into_iter().map(|s| s.text));
    for m in module.macros.values() {
        texts.push(format!(
            "macro {}({})\n{}",
            m.name,
            m.params.join(", "),
            emit_at(&m.stmts, 1, 48)
        ));
    }
    for ((name, level), b) in &module.fns {
        let params: Vec<String> = b
            .params
            .iter()
            .map(|(p, t)| format!("{p}: {}", fmt(t)))
            .collect();
        texts.push(format!(
            "fn {name}.{level}({})\n{}",
            params.join(", "),
            emit_at(&b.stmts, 1, 48)
        ));
    }
    read(&texts.join("\n"), &module.file)
}
