//! The Python L2 tests (`compiler/tests/test_l2.py`, `l2/test_stage.py`,
//! `test_banks.py`) ported to the Rust stack: each port reads the same L2
//! text on the machine the Python `T.machine()` names (multimesh_v9, the 4+2
//! die) and checks the same property of the L2 or L1 text. With `mutants`,
//! each port's killing mutant makes it fail (`each_port_has_its_killing_mutant`).

use std::cmp::Reverse;
use std::collections::{BTreeSet, BinaryHeap, HashMap};
use std::path::Path;

use ktpu_l1::text::emit;
use ktpu_l1::text::Term;
use ktpu_l2::lower::mover::cut;
use ktpu_l2::lower::vector::banks::typed as banks;
use ktpu_l2::lower::vector::gen::typed::{self as ir, Event, Select, Symbol};
use ktpu_l2::lower::vector::modulo::internals::{self, Life};
use ktpu_l2::lower::vector::modulo::{derive, lead, plan, Core, MOp};
use ktpu_l2::target::MOVE_WORDS;
use ktpu_l2::{compile_with, lower, passes, Program, Search, Strategy, Target, PASSES};
use ktpu_machine::Machine;
use regex::Regex;
use serde_json::json;

type Res = Result<(), String>;

// Literal test fixtures enter through the L1 parser; production derivation is typed.
fn parse(template: &str, names: &[String], copies: u32, core: &Core) -> MOp {
    let syms: Vec<_> = names
        .iter()
        .map(|n| Symbol(n[1..].parse().unwrap()))
        .collect();
    let source = Regex::new(r"\{([0-9]+)\}")
        .unwrap()
        .replace_all(template, |c: &regex::Captures| format!("v{}", &c[1]))
        .into_owned();
    let mut st = ktpu_l1::text::parse(&(source + "\n"), "<fixture>")
        .unwrap()
        .remove(0);
    fn resolve(t: &mut Term, syms: &[Symbol]) {
        match t {
            Term::Name(n) | Term::View(n, _) if n.starts_with('v') => {
                if let Ok(i) = n[1..].parse::<usize>() {
                    *n = syms[i].name();
                }
            }
            Term::Call(_, args) => {
                for a in args {
                    resolve(a, syms);
                }
            }
            _ => {}
        }
    }
    for a in &mut st.args {
        resolve(a, &syms);
    }
    derive(&st, &syms, copies, core)
}

macro_rules! ensure {
    ($c:expr) => {
        if !$c {
            return Err(format!("line {}: {}", line!(), stringify!($c)));
        }
    };
    ($c:expr, $($m:tt)+) => {
        if !$c {
            return Err(format!("line {}: {}", line!(), format!($($m)+)));
        }
    };
}

// ------------------------------------------------------------------ helpers

/// The modulo scheduler's placement attempts an op may take beyond its
/// first (`modulo.rs` `PLACE_BUDGET`, private).
const PLACE_BUDGET: i64 = 6;

/// A xorshift stream for the randomised ports.
struct Rng(u64);

impl Rng {
    fn below(&mut self, m: u64) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0 % m
    }

    /// An integer in lo..=hi.
    fn range(&mut self, lo: i64, hi: i64) -> i64 {
        lo + self.below((hi - lo + 1) as u64) as i64
    }
}

/// The Python `T.machine()`: the 4+2 die, four nodes.
fn machine() -> Machine {
    Machine::named("multimesh_v9").unwrap()
}

fn target() -> Target {
    Target::of(&machine()).with_nodes(4)
}

fn nodes(n: i64) -> Target {
    target().with_nodes(n)
}

/// `t` with vector cores of `n` registers.
fn regs(mut t: Target, n: u32) -> Target {
    t.vregs = n;
    t
}

fn read(text: &str) -> Result<Program, String> {
    Program::parse(text, "<ported>", None).map_err(|e| format!("read: {e}"))
}

/// `p` for vector cores of `n` registers.
fn vregs(p: &Program, n: u32) -> Program {
    p.with_vregs(n, p.module.vcopies)
}

fn example_text(name: &str) -> String {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../language/kohakutpu/language/examples/l2");
    for sub in ["matmul", "vector", "multinode", "mover", "fused"] {
        let path = root.join(sub).join(format!("{name}.ktpu"));
        if path.exists() {
            return std::fs::read_to_string(path).unwrap();
        }
    }
    panic!("no L2 example {name}")
}

fn example(name: &str) -> Result<Program, String> {
    Program::parse(&example_text(name), "<example>", Some(name)).map_err(|e| format!("read: {e}"))
}

/// The L1 text of `p` (no passes), or the refusal's text.
fn lw(p: &Program, t: &Target, s: Strategy) -> Result<String, String> {
    lower::lower(p, t, &Search::new(s)).map_err(|e| format!("{e}"))
}

/// [`lw`] with every candidate's constants held (`hold`) or its rows made
/// before the loop pinned (`pin`) as forced.
fn forced(
    p: &Program,
    t: &Target,
    s: Strategy,
    hold: Option<bool>,
    pin: Option<bool>,
) -> Result<String, String> {
    lower::lower(p, t, &Search::new(s).forced(hold, pin)).map_err(|e| format!("{e}"))
}

fn run(p: &Program, names: &[&str], t: &Target, s: Strategy) -> Result<Program, String> {
    passes::run(p, names, t, &Search::new(s)).map_err(|e| format!("{e}"))
}

/// The pipeline's L1 (scheduled when `sched`): its module's text.
fn compile(p: &Program, t: &Target, names: &[&str], sched: bool) -> Result<String, String> {
    compile_with(p, t, names, sched, &Search::default())
        .map(|c| c.l1.source)
        .map_err(|e| format!("{e}"))
}

fn refused(got: Result<impl Sized, String>, said: &str) -> Res {
    match got {
        Ok(_) => Err(format!("not refused ({said})")),
        Err(e) if e.contains(said) => Ok(()),
        Err(e) => Err(format!("refused, not with {said}: {e}")),
    }
}

/// The first `t * N : +N` of a text: its N.
fn rows_of(text: &str) -> Option<i64> {
    let re = Regex::new(r"t \* (\d+) : \+(\d+)").unwrap();
    let found = re
        .captures_iter(text)
        .find(|c| c[1] == c[2])
        .map(|c| c[1].parse().unwrap());
    found
}

/// The value of an integer expression of `+ - * //`, parentheses and names.
fn eval(text: &str, env: &[(&str, i64)]) -> Result<i64, String> {
    fn atom(s: &[char], i: &mut usize, env: &[(&str, i64)]) -> Result<i64, String> {
        skip(s, i);
        if *i < s.len() && s[*i] == '(' {
            *i += 1;
            let v = sum(s, i, env)?;
            skip(s, i);
            *i += 1;
            return Ok(v);
        }
        if *i < s.len() && s[*i] == '-' {
            *i += 1;
            return Ok(-atom(s, i, env)?);
        }
        let start = *i;
        while *i < s.len() && (s[*i].is_alphanumeric() || s[*i] == '_') {
            *i += 1;
        }
        let w: String = s[start..*i].iter().collect();
        if let Ok(v) = w.parse() {
            return Ok(v);
        }
        env.iter()
            .find(|(n, _)| *n == w)
            .map(|(_, v)| *v)
            .ok_or(format!("no value of {w:?}"))
    }
    fn skip(s: &[char], i: &mut usize) {
        while *i < s.len() && s[*i] == ' ' {
            *i += 1;
        }
    }
    fn prod(s: &[char], i: &mut usize, env: &[(&str, i64)]) -> Result<i64, String> {
        let mut v = atom(s, i, env)?;
        loop {
            skip(s, i);
            if *i < s.len() && s[*i] == '*' {
                *i += 1;
                v *= atom(s, i, env)?;
            } else if *i + 1 < s.len() && s[*i] == '/' && s[*i + 1] == '/' {
                *i += 2;
                v = v.div_euclid(atom(s, i, env)?);
            } else {
                return Ok(v);
            }
        }
    }
    fn sum(s: &[char], i: &mut usize, env: &[(&str, i64)]) -> Result<i64, String> {
        let mut v = prod(s, i, env)?;
        loop {
            skip(s, i);
            if *i < s.len() && s[*i] == '+' {
                *i += 1;
                v += prod(s, i, env)?;
            } else if *i < s.len() && s[*i] == '-' {
                *i += 1;
                v -= prod(s, i, env)?;
            } else {
                return Ok(v);
            }
        }
    }
    let s: Vec<char> = text.chars().collect();
    sum(&s, &mut 0, env)
}

/// The text after the first `image `, up to the next one.
fn first_image(text: &str) -> Result<&str, String> {
    text.split("image ").nth(1).ok_or("no image".to_string())
}

/// The text from the first `image ` on.
fn images(text: &str) -> Result<&str, String> {
    text.find("image ")
        .map(|i| &text[i..])
        .ok_or("no image".to_string())
}

fn first_word_at(pattern: &str, text: &str) -> Result<i64, String> {
    Regex::new(pattern)
        .unwrap()
        .captures(text)
        .map(|c| c[1].parse().unwrap())
        .ok_or(format!("no {pattern}"))
}

// ------------------------------------------------------------ the image values

/// A value an image computes: a number, an unpacked row (tagged), or an op.
#[derive(Clone, Debug, PartialEq)]
enum Val {
    N(f64),
    X(Option<i64>),
    Op(String, Vec<Val>),
}

fn op(name: &str, args: Vec<Val>) -> Val {
    Val::Op(name.to_string(), args)
}

/// What [`packed`] tags each unpacked row with.
#[derive(Clone, Copy, PartialEq)]
enum Tag {
    None,
    Count,
    Word,
}

type Drains = Vec<(String, Vec<Option<Val>>)>;

/// Each `vpack`'s value, its rows' numbers read through the registers the
/// image sets (scalars, `seti`, pinned `vmov`s): a `loop sN` body runs sN
/// times, a `skipz sN` block once unless sN is 0 (`getstk` reads `sticky`).
/// An unpack is tagged by `tag`; with `drains`, each 8-row drain is
/// appended as (descriptor, the values its L1 words hold).
fn packed(
    image: &str,
    sticky: f64,
    tag: Tag,
    mut drains: Option<&mut Drains>,
) -> Result<Vec<Val>, String> {
    struct St {
        regs: HashMap<String, Val>,
        out: Vec<Val>,
        l1: HashMap<i64, Val>,
        unpacks: i64,
    }
    let word = Regex::new(r"l1\[(\d+)\]").unwrap();
    let at = |s: &str| -> Result<i64, String> {
        word.captures(s)
            .map(|c| c[1].parse().unwrap())
            .ok_or(format!("no l1 word: {s}"))
    };
    fn val(st: &St, a: &str) -> Result<Val, String> {
        st.regs.get(a).cloned().ok_or(format!("no value in {a:?}"))
    }
    fn first(a: &str) -> &str {
        a.split_whitespace().next().unwrap_or("")
    }
    #[allow(clippy::too_many_arguments)]
    fn go(
        body: &[(usize, &str)],
        st: &mut St,
        sticky: f64,
        tag: Tag,
        drains: &mut Option<&mut Drains>,
        at: &dyn Fn(&str) -> Result<i64, String>,
    ) -> Res {
        let mut i = 0;
        while i < body.len() {
            let (ind, s) = body[i];
            i += 1;
            let (op_, rest) = s.split_once(' ').unwrap_or((s, ""));
            let args: Vec<&str> = rest.split(',').map(str::trim).collect();
            match op_ {
                "loop" | "skipz" => {
                    let mut j = i;
                    while j < body.len() && body[j].0 > ind {
                        j += 1;
                    }
                    let n = match val(st, args[0])? {
                        Val::N(x) => x,
                        v => return Err(format!("a count of {v:?}")),
                    };
                    let times = if op_ == "loop" {
                        n as i64
                    } else {
                        (n != 0.0) as i64
                    };
                    for _ in 0..times {
                        go(&body[i..j], st, sticky, tag, drains, at)?;
                    }
                    i = j;
                }
                "getstk" => {
                    st.regs.insert(args[0].to_string(), Val::N(sticky));
                }
                "vfma" => {
                    let v = op(
                        "vfma",
                        args[1..]
                            .iter()
                            .map(|a| val(st, a))
                            .collect::<Result<_, _>>()?,
                    );
                    st.regs.insert(args[0].to_string(), v);
                }
                "seti" => {
                    let (reg, v) = rest.split_once('=').ok_or("seti R = V")?;
                    let v: f64 = v.trim().parse().map_err(|_| format!("seti {v}"))?;
                    st.regs.insert(reg.trim().to_string(), Val::N(v));
                }
                "vunpk" => {
                    st.unpacks += 1;
                    let t = match tag {
                        Tag::Word => Some(at(s)?),
                        Tag::Count => Some(st.unpacks),
                        Tag::None => None,
                    };
                    st.regs.insert(first(args[0]).to_string(), Val::X(t));
                }
                "vpack" => {
                    let v = val(st, first(args[0]))?;
                    st.out.push(v.clone());
                    st.l1.insert(at(s)?, v);
                }
                "vdrain" => {
                    if let Some(d) = drains.as_deref_mut() {
                        let w = at(s)?;
                        let held = (0..8).map(|r| st.l1.get(&(w + 8 * r)).cloned()).collect();
                        d.push((first(args[0]).to_string(), held));
                    }
                }
                "vmov" => {
                    let v = val(st, args[1])?;
                    st.regs.insert(args[0].to_string(), v);
                }
                "vadd" | "vmul" => {
                    let v = op(op_, vec![val(st, args[1])?, val(st, args[2])?]);
                    st.regs.insert(args[0].to_string(), v);
                }
                _ => {}
            }
        }
        Ok(())
    }
    let lines: Vec<(usize, &str)> = image
        .lines()
        .map(|s| (s.len() - s.trim_start().len(), s.trim()))
        .filter(|x| x.0 >= 4)
        .collect();
    let mut st = St {
        regs: [("k0", 0.0), ("k1", 1.0), ("k2", -1.0)]
            .into_iter()
            .map(|(k, v)| (k.to_string(), Val::N(v)))
            .collect(),
        out: Vec::new(),
        l1: HashMap::new(),
        unpacks: 0,
    };
    go(&lines, &mut st, sticky, tag, &mut drains, &at)?;
    Ok(st.out)
}

// ------------------------------------------------------- the mover's walks

type Levels = Vec<(i64, i64)>;
type Walk = (String, i64, Levels);

/// Byte addresses a walk visits, in its order.
fn addresses(w: &Walk) -> Vec<(String, i64)> {
    let mut out = vec![w.1];
    for &(n, s) in &w.2 {
        out = out
            .iter()
            .flat_map(|a| (0..n).map(move |i| a + i * s))
            .collect();
    }
    out.into_iter().map(|a| (w.0.clone(), a)).collect()
}

/// The `w(NAME + OFF, (N, S), ...)` walks of an L1 line.
fn walks(line: &str) -> Vec<Walk> {
    let re = Regex::new(r"w\((\w+) \+ (-?\d+), ((?:\(\d+, -?\d+\)(?:, )?)+)\)").unwrap();
    let lv = Regex::new(r"\((\d+), (-?\d+)\)").unwrap();
    re.captures_iter(line)
        .map(|m| {
            let levels = lv
                .captures_iter(&m[3])
                .map(|c| (c[1].parse().unwrap(), c[2].parse().unwrap()))
                .collect();
            (m[1].to_string(), m[2].parse().unwrap(), levels)
        })
        .collect()
}

fn mover_lines(text: &str, op: &str) -> Vec<String> {
    text.lines()
        .map(str::trim)
        .filter(|l| l.starts_with(&format!("{op} w(")))
        .map(str::to_string)
        .collect()
}

/// Word for word, the pairs of the walks of `walks` side by side.
fn zipped(ws: &[Walk]) -> Vec<Vec<(String, i64)>> {
    let a: Vec<Vec<(String, i64)>> = ws.iter().map(addresses).collect();
    (0..a[0].len())
        .map(|k| a.iter().map(|x| x[k].clone()).collect())
        .collect()
}

fn mover_program(decl: &str, stmt: &str) -> String {
    format!("target ktpu.v9\nfn k.l2({decl})\n    {stmt}\n")
}

/// The pieces of `op` cover `want` step for step, each within the limits.
fn pieces_cover(decl: &str, stmt: &str, op: &str, want: &[Walk]) -> Result<Vec<String>, String> {
    let t = target();
    let text = lw(&read(&mover_program(decl, stmt))?, &t, Strategy::Auto)?;
    let lines = mover_lines(&text, op);
    let mut got = Vec::new();
    for ln in &lines {
        let ws = walks(ln);
        ensure!(
            ws.iter().all(|w| addresses(w).len() as i64 <= MOVE_WORDS),
            "{ln}"
        );
        ensure!(ws.iter().all(|w| w.2.len() <= t.levels), "{ln}");
        got.extend(zipped(&ws));
    }
    ensure!(got == zipped(want));
    Ok(lines)
}

// ------------------------------------------- the one-word-run check (test side)

const WORD: i64 = 32;

fn ref_merge(levels: &[(i64, i64)]) -> Levels {
    let mut out: Levels = Vec::new();
    for &(n, s) in levels.iter().rev() {
        match out.last() {
            Some(&(m, t)) if t * m == s => {
                out.pop();
                out.push((n * m, t));
            }
            _ => out.push((n, s)),
        }
    }
    out.reverse();
    out
}

fn ref_squeeze(levels: &[(i64, i64)]) -> Levels {
    let out: Levels = levels.iter().copied().filter(|l| l.0 != 1).collect();
    if out.is_empty() {
        vec![*levels.last().unwrap()]
    } else {
        out
    }
}

/// Words of a level list's innermost run.
fn ref_run(levels: &[(i64, i64)]) -> i64 {
    let (n, s) = *ref_merge(&ref_squeeze(levels)).last().unwrap();
    if s == WORD {
        n
    } else {
        1
    }
}

/// Top-level pieces of `s` split at `sep` outside parentheses.
fn split_top(s: &str, sep: char) -> Vec<String> {
    let (mut out, mut cur, mut depth) = (Vec::new(), String::new(), 0);
    for c in s.chars() {
        match c {
            '(' => depth += 1,
            ')' => depth -= 1,
            _ => {}
        }
        if c == sep && depth == 0 {
            if !cur.trim().is_empty() {
                out.push(cur.trim().to_string());
            }
            cur.clear();
        } else {
            cur.push(c);
        }
    }
    if !cur.trim().is_empty() {
        out.push(cur.trim().to_string());
    }
    out
}

/// `ref_run` of an L1 walk `w(BASE, (COUNT, STRIDE), ...)`; 1 for anything
/// else, or a level not two integers.
fn l1_run(t: &str) -> i64 {
    let Some(inner) = t.strip_prefix("w(").and_then(|x| x.strip_suffix(')')) else {
        return 1;
    };
    let mut levels = Vec::new();
    for lv in split_top(inner, ',').iter().skip(1) {
        let pair = lv.strip_prefix('(').and_then(|x| x.strip_suffix(')'));
        let Some(pair) = pair.filter(|_| !lv.contains('=')) else {
            return 1;
        };
        let xs: Vec<Option<i64>> = pair.split(',').map(|x| x.trim().parse().ok()).collect();
        match xs.as_slice() {
            [Some(n), Some(s)] => levels.push((*n, *s)),
            _ => return 1,
        }
    }
    if levels.is_empty() {
        1
    } else {
        ref_run(&levels)
    }
}

/// Whether L1 mover line `line` reads and writes in runs of one word on both
/// sides (a copy's two walks; a gather reads `words=` a row, a scatter writes
/// it; `burst=1` caps both sides, `nocoal` the write side): the moves the
/// `stage` pass removes on a mover without `mover.one_word_runs` (F-001,
/// L2.PASS.001). A test-side port of Python's `l2/mover.py` `one_word`; Rust
/// has no product check.
fn one_word(line: &str) -> bool {
    let norm = line.trim().replace(" + ", "+").replace(" - ", "-");
    let toks = split_top(&norm, ' ');
    let Some(op) = toks.first() else {
        return false;
    };
    if !["copy", "gather", "scatter"].contains(&op.as_str()) {
        return false;
    }
    let mut pos = Vec::new();
    let mut kw = HashMap::new();
    for t in &toks[1..] {
        match t.split_once('=') {
            Some((k, v)) if !t.starts_with("w(") => {
                kw.insert(k.to_string(), v.to_string());
            }
            _ => pos.push(t.as_str()),
        }
    }
    if pos.len() < 3 || pos[1] != "->" || !pos.iter().any(|p| p.starts_with("w(")) {
        return false;
    }
    let int = |k: &str| kw.get(k).and_then(|v| v.parse::<i64>().ok());
    let table = int("words").filter(|&n| n != 0).unwrap_or(1);
    let (mut rd, mut wr) = (l1_run(pos[0]), l1_run(pos[2]));
    if op == "gather" {
        rd = table;
    } else if op == "scatter" {
        wr = table;
    }
    if pos.contains(&"nocoal") {
        wr = 1;
    }
    if int("burst") == Some(1) {
        rd = 1;
        wr = 1;
    }
    rd == 1 && wr == 1
}

/// The ops of the L1 text's mover lines that step one-word runs.
fn one_word_moves(l1: &str) -> Vec<String> {
    l1.lines()
        .filter(|l| one_word(l))
        .map(|l| l.split_whitespace().next().unwrap().to_string())
        .collect()
}

// ---------------------------------------------------------------- the texts

const OPEN: &str = r"target ktpu.v9

fn mm.l2(x: mx7a[512, 512] @dram(gm=64, nk=2), w: mx7b[1024, 512] @dram(gn=64, nk=2), y: f16[512, 1024] @dram(tile=64x64))
    t = buffer : f16[512, 128] @mem
    par n in 0..2 on node[n]
        par u in 0..2 on mg[any]
            for i in 0..2
                a = fill x[i*256 : +256, 0 : +512]          : mx7a[256, 512] @A
                b = fill w[(2*n + u)*256 : +256, 0 : +512]  : mx7b[256, 512] @B
                acc = gemm a, b split                       : f32[256, 256] @acc
                drain acc -> y[i*256 : +256, (2*n + u)*256 : +256]
";

const FAR: &str = r"target ktpu.v9

fn far.l2(x: f16[64, 70000] @dram, out: f16[64, 128] @dram)
    par c in 0..2 on vc[c]
        for t in 0..8
            xl = load x[c*32 + t*4 : +4, 0 : +128] : f16[4, 128] @l1(gt4)
            v = unpack xl : f32[4, 128] @reg(gt4)
            e = add v, 1.0 : f32[4, 128] @reg(gt4)
            p = pack e : f16[4, 128] @l1(gt4)
            store out[c*32 + t*4 : +4, 0 : +128] <- p
";

const WIDE: &str = r"target ktpu.v9

fn wide.l2(x: f16[64, 256] @dram, out: f16[64, 256] @dram)
    par c in 0..2 on vc[c]
        for t in 0..2
            xl = load x[c*32 + t*16 : +16, 0 : +256] : f16[16, 256] @l1(gt4)
            v = unpack xl : f32[16, 256] @reg(gt4)
            e = add v, 1.0 : f32[16, 256] @reg(gt4)
            p = pack e : f16[16, 256] @l1(gt4)
            store out[c*32 + t*16 : +16, 0 : +256] <- p
";

const BAND: &str = r"target ktpu.v9

fn band.l2(x: f16[64, 128] @dram, g: f16[128] @dram, out: f16[64, 128] @dram)
    par c in 0..2 on vc[c]
        gl = load g[0 : +128] : f16[4, 128] @l1(gt4)
        gv = unpack gl : f32[4, 128] @reg(gt4)
        for t in 0..2
            xl = load x[c*32 + t*16 : +16, 0 : +128] : f16[16, 128] @l1(gt4)
            v = unpack xl : f32[16, 128] @reg(gt4)
            b = add gv, 2.0 : f32[4, 128] @reg(gt4)
            b2 = mul b, 0.5 : f32[4, 128] @reg(gt4)
            e = mul v, b2 : f32[16, 128] @reg(gt4)
            p = pack e : f16[16, 128] @l1(gt4)
            store out[c*32 + t*16 : +16, 0 : +128] <- p
";

const SPLIT: &str = r"target ktpu.v9

fn split.l2(x: f16[8, 512] @dram, g: f16[512] @dram, h: f16[512] @dram, out: f16[8, 512] @dram)
    par c in 0..1 on vc[c]
        gl = load g[0 : +512] : f16[4, 512] @l1(gt4)
        gv = unpack gl : f32[4, 512] @reg(gt4)
        hl = load h[0 : +512] : f16[4, 512] @l1(gt4)
        hv = unpack hl : f32[4, 512] @reg(gt4)
        for t in 0..2
            xl = load x[t*4 : +4, 0 : +512] : f16[4, 512] @l1(gt4)
            v = unpack xl : f32[4, 512] @reg(gt4)
            a = mul v, gv : f32[4, 512] @reg(gt4)
            e = add a, hv : f32[4, 512] @reg(gt4)
            p = pack e : f16[4, 512] @l1(gt4)
            store out[t*4 : +4, 0 : +512] <- p
";

fn whole() -> String {
    SPLIT
        .replace("g[0 : +512]", "g")
        .replace("x[t*4 : +4, 0 : +512]", "x[t*4 : +4]")
        .replace("out[t*4 : +4, 0 : +512]", "out[t*4 : +4]")
}

fn xlane() -> String {
    SPLIT.replace(
        "a = mul v, gv",
        "u = xlane v, 1 : f32[4, 512] @reg(gt4)\n            a = mul u, gv",
    )
}

const PLAIN: &str = r"target ktpu.v9

fn chain.l2(x: f16[64, 128] @dram, out: f16[64, 128] @dram)
    par c in 0..2 on vc[c]
        for t in 0..2
            xl = load x[c*32 + t*16 : +16, 0 : +128] : f16[16, 128] @l1
            v = unpack xl : f32[16, 128] @reg
{chain}            p = pack e{n} : f16[16, 128] @l1
            store out[c*32 + t*16 : +16, 0 : +128] <- p
";

const CONSTS: &str = r"target ktpu.v9

fn chain.l2(x: f16[{rows}, 128] @dram, out: f16[{rows}, 128] @dram)
    par c in 0..1 on vc[c]
        for t in 0..{trips}
            xl = load x[t*8 : +8, 0 : +128] : f16[8, 128] @l1
            v = unpack xl : f32[8, 128] @reg
{chain}            p = pack e{n} : f16[8, 128] @l1
            store out[t*8 : +8, 0 : +128] <- p
";

const PIN: &str = r"target ktpu.v9

fn pin.l2(x: f16[32, 128] @dram, g: f16[1, 128] @dram, out: f16[32, 128] @dram)
    par c in 0..1 on vc[c]
        gl = load g[0 : +1, 0 : +128] : f16[1, 128] @l1
        gv = unpack gl : f32[1, 128] @reg
        for t in 0..4
            xl = load x[t*8 : +8, 0 : +128] : f16[8, 128] @l1
            v = unpack xl : f32[8, 128] @reg
            b = add gv, 2.0 : f32[1, 128] @reg
            e = mul v, b : f32[8, 128] @reg
            p = pack e : f16[8, 128] @l1
            store out[t*8 : +8, 0 : +128] <- p
";

const BLOCKS: &str =
    "target ktpu.v9\n\nfn blk.l2(x: f16[16, 512] @dram, out: f16[16, 512] @dram)\n";

const BLOCK: &str = r"    par c{j} in 0..2 on vc[c{j}]
        for t{j} in 0..2
            xl{j} = load x[c{j}*8 + t{j}*4 : +4, {at} : +{w}] : f16[4, {w}] @l1(gt4)
            v{j} = unpack xl{j} : f32[4, {w}] @reg(gt4)
            e{j} = mul v{j}, {k} : f32[4, {w}] @reg(gt4)
            p{j} = pack e{j} : f16[4, {w}] @l1(gt4)
            store out[c{j}*8 + t{j}*4 : +4, {at} : +{w}] <- p{j}
";

const PACKED: &str = r"target ktpu.v9

fn pk.l2(x: f16[32, 128] @dram, g: f16[8, 128] @dram, out: f16[32, 128] @dram, o2: f16[32, 128] @dram)
    par c in 0..1 on vc[c]
        gl = load g[0 : +8, 0 : +128] : f16[8, 128] @l1
        gv = unpack gl : f32[8, 128] @reg
        for t in 0..4
            xl = load x[t*8 : +8, 0 : +128] : f16[8, 128] @l1
            v = unpack xl : f32[8, 128] @reg
            e = add v, 1.0 : f32[8, 128] @reg
            p = pack e : f16[8, 128] @l1
            store out[t*8 : +8, 0 : +128] <- p
            q = pack gv : f16[8, 128] @l1
            store o2[t*8 : +8, 0 : +128] <- q
";

const FOLDED: &str = r"target ktpu.v9

fn fd.l2(x: f16[64, 128] @dram, g: f16[128] @dram, out: f16[64] @dram)
    par c in 0..1 on vc[c]
        gl = load g[0 : +128] : f16[4, 128] @l1(gt4)
        gv = unpack gl : f32[4, 128] @reg(gt4)
        for t in 0..4
            xl = load x[t*16 : +16, 0 : +128] : f16[16, 128] @l1(gt4)
            v = unpack xl : f32[16, 128] @reg(gt4)
            e = mul v, gv : f32[16, 128] @reg(gt4)
            s = rsum e : f32[16] @reg
            p = pack s : f16[16] @l1
            store out[t*16 : +16] <- p
";

const KEEP: &str = r"target ktpu.v9

fn keep.l2(x: f16[64, 128] @dram, g: f16[8, 128] @dram, out: f16[64, 128] @dram)
    par c in 0..1 on vc[c]
        gl = load g[0 : +8, 0 : +128] : f16[8, 128] @l1
        gv = unpack gl : f32[8, 128] @reg
        for t in 0..4
            xl = load x[t*16 : +16, 0 : +128] : f16[16, 128] @l1
            v = unpack xl : f32[16, 128] @reg
            e = mul v, 2.0 : f32[16, 128] @reg
            p = pack e : f16[16, 128] @l1
            store out[t*16 : +16, 0 : +128] <- p
";

const TWIN: &str = r"target ktpu.v9

fn bc.l2(x: f16[{rows}, 128] @dram, out: f16[{rows2}, 128] @dram)
    par c in 0..1 on vc[c]
        for t in 0..{trips}
            xl = load x[t*8 : +8, 0 : +128] : f16[8, 128] @l1
            v = unpack xl : f32[8, 128] @reg
            e = mul v, 2.0 : f32[8, 128] @reg
            p = pack e : f16[8, 128] @l1
            store out[t*8 : +8, 0 : +128] <- p
            store out[{rows} + t*8 : +8, 0 : +128] <- p
";

const LAYOUTS: &str = r"target ktpu.v9

fn gt.l2(x: f16[16, 32] @dram, o1: f16[16, 32] @dram, o2: f16[16, 32] @dram(tile=4x32))
    par c in 0..1 on vc[c]
        for t in 0..4
            xl = load x[t*4 : +4, 0 : +32] : f16[4, 32] @l1(gt4)
            v = unpack xl : f32[4, 32] @reg(gt4)
            e = mul v, 2.0 : f32[4, 32] @reg(gt4)
            p = pack e : f16[4, 32] @l1(gt4)
            store o1[t*4 : +4, 0 : +32] <- p
            store o2[t*4 : +4, 0 : +32] <- p
";

const STICKY: &str = r"target ktpu.v9

fn chain.l2(x: f16[16, 128] @dram, out: f16[16, 128] @dram)
    par c in 0..1 on vc[c]
        for t in 0..2
            xl = load x[t*8 : +8, 0 : +128] : f16[8, 128] @l1
            v = unpack xl : f32[8, 128] @reg
{chain}            h = fma g16, 3.25, 3.5 : f32[8, 128] @reg
            if sticky
                h = mul h, 3.75 : f32[8, 128] @reg
            k = mul h, 3.75 : f32[8, 128] @reg
            p = pack k : f16[8, 128] @l1
            store out[t*8 : +8, 0 : +128] <- p
";

fn chain_of(n: usize) -> String {
    let mut c = "            e0 = add v, 1.0 : f32[16, 128] @reg\n".to_string();
    for i in 1..=n {
        c += &format!(
            "            e{i} = mul e{}, 1.5 : f32[16, 128] @reg\n",
            i - 1
        );
    }
    PLAIN.replace("{chain}", &c).replace("{n}", &n.to_string())
}

/// A chain reading n + 1 distinct numbers: its lines and each row's value.
fn consts(n: usize) -> (String, Val) {
    let mut chain = "            e0 = add v, 0.5 : f32[8, 128] @reg\n".to_string();
    let mut want = op("vadd", vec![Val::X(None), Val::N(0.5)]);
    for i in 1..=n {
        let c = 1.0 + i as f64 / 64.0;
        chain += &format!(
            "            e{i} = mul e{}, {c} : f32[8, 128] @reg\n",
            i - 1
        );
        want = op("vmul", vec![want, Val::N(c)]);
    }
    (chain, want)
}

/// A GT4 layernorm over rows of `w` columns, 4-row trips, gamma and beta one
/// row broadcast to the band.
fn norm(w: i64) -> String {
    let r = 1.0 / w as f64;
    [
        "target ktpu.v9".to_string(),
        format!("fn ln.l2(x: f16[64, {w}] @dram, g: f16[{w}] @dram, b: f16[{w}] @dram, y: f16[64, {w}] @dram)"),
        "    par c in 0..2 on vc[c]".to_string(),
        format!("        gl = load g : f16[4, {w}] @l1(gt4)"),
        format!("        gv = unpack gl : f32[4, {w}] @reg(gt4)"),
        format!("        bl = load b : f16[4, {w}] @l1(gt4)"),
        format!("        bv = unpack bl : f32[4, {w}] @reg(gt4)"),
        "        for t in 0..8".to_string(),
        format!("            xl = load x[c*32 + t*4 : +4] : f16[4, {w}] @l1(gt4)"),
        format!("            v = unpack xl : f32[4, {w}] @reg(gt4)"),
        "            s1 = rsum v : f32[4] @reg".to_string(),
        format!("            mu = mul s1, {r} : f32[4] @reg"),
        format!("            d = sub v, mu[:, *] : f32[4, {w}] @reg(gt4)"),
        format!("            q = mul d, d : f32[4, {w}] @reg(gt4)"),
        "            s2 = rsum q : f32[4] @reg".to_string(),
        format!("            var = mul s2, {r} : f32[4] @reg"),
        "            var = add var, 1e-05 : f32[4] @reg".to_string(),
        "            rs = rsqrt var : f32[4] @reg".to_string(),
        format!("            n = mul d, rs[:, *] : f32[4, {w}] @reg(gt4)"),
        format!("            n = mul n, gv : f32[4, {w}] @reg(gt4)"),
        format!("            o = add n, bv : f32[4, {w}] @reg(gt4)"),
        format!("            ol = pack o : f16[4, {w}] @l1(gt4)"),
        "            store y[c*32 + t*4 : +4] <- ol".to_string(),
        String::new(),
    ]
    .join("\n")
}

/// `fn k.l2(params)` of `body` lines.
fn kernel(params: &str, body: &[&str]) -> String {
    let lines: String = body.iter().map(|l| format!("    {l}\n")).collect();
    format!("target ktpu.v9\n\nfn k.l2({params})\n{lines}")
}

/// `fn k.l2`'s statements, one a line, spaces collapsed.
fn statements(p: &Program) -> Vec<String> {
    let spaces = Regex::new(r" +").unwrap();
    p.text()
        .split_once("fn k.l2(")
        .map(|x| x.1)
        .unwrap_or("")
        .lines()
        .skip(1)
        .filter(|l| !l.trim().is_empty())
        .map(|l| spaces.replace_all(l.trim(), " ").to_string())
        .collect()
}

const TAB: &str = "t: f16[64, 16] @dram, idx: u32[24] @dram, idxf: f32[24] @dram";

/// The stage tests' moves: (params, statement, the staged statements).
fn rewrites() -> Vec<(String, &'static str, Vec<&'static str>)> {
    vec![
        (
            format!("{TAB}, y: f16[21, 128] @dram"),
            "gather y[:, 16 : +16] <- t index=idx",
            vec![
                "h0 = buffer : f16[21, 16] @dram",
                "gather h0 <- t index=idx",
                "copy y[:, 16 : +16] <- h0",
            ],
        ),
        (
            format!("{TAB}, y: f16[21, 128] @dram"),
            "gather y[:, 16 : +16] <- t index=idxf fp",
            vec![
                "h0 = buffer : f16[21, 16] @dram",
                "gather h0 <- t fp index=idxf",
                "copy y[:, 16 : +16] <- h0",
            ],
        ),
        (
            format!("{TAB}, s: f16[21, 128] @dram"),
            "scatter t <- s[:, 16 : +16] index=idx",
            vec![
                "h0 = buffer : f16[21, 16] @dram",
                "copy h0 <- s[:, 16 : +16]",
                "scatter t <- h0 index=idx",
            ],
        ),
        (
            "x: f16[4, 64] @dram, y: f16[16, 64] @dram".to_string(),
            "copy y[:, 0 : +16] <- x perm=(1, 0)",
            vec![
                "h0 = buffer : f16[256] @dram",
                "copy h0 <- x perm=(1, 0)",
                "copy y[:, 0 : +16] <- h0",
            ],
        ),
        (
            "x: f16[8, 64] @dram, y: f16[8, 128] @dram".to_string(),
            "copy y[:, 32 : +16] <- x[:, 16 : +16]",
            vec![
                "h0 = buffer : f16[128] @dram",
                "copy h0 <- x[:, 16 : +16]",
                "copy y[:, 32 : +16] <- h0",
            ],
        ),
        (
            "x: f16[4, 64] @dram, y: f16[16, 64] @dram".to_string(),
            "copy y[:, 0 : +16] <- x walk=((4, 32), (4, 128))",
            vec![
                "h0 = buffer : f16[256] @dram",
                "copy h0 <- x walk=((4, 32), (4, 128))",
                "copy y[:, 0 : +16] <- h0",
            ],
        ),
        (
            "x: f16[16, 64] @dram, y: f16[16, 64] @dram".to_string(),
            "copy y <- x[:, 0 : +16] dwalk=((4, 128), (4, 512)) dat=32",
            vec![
                "h0 = buffer : f16[256] @dram",
                "copy h0 <- x[:, 0 : +16]",
                "copy y <- h0 dwalk=((4, 128), (4, 512)) dat=32",
            ],
        ),
    ]
}

/// A machine whose mover takes one-word runs.
fn safe() -> Target {
    let mut t = nodes(1);
    t.one_word_runs = true;
    t
}

fn staged(p: &Program, t: &Target) -> Result<Program, String> {
    run(p, &["stage"], t, Strategy::Auto)
}

fn l1_moves(p: &Program, names: &[&str]) -> Result<Vec<String>, String> {
    Ok(one_word_moves(&compile(p, &nodes(1), names, false)?))
}

fn unstaged() -> Vec<&'static str> {
    PASSES.iter().copied().filter(|p| *p != "stage").collect()
}

// ---------------------------------------------------------------- the ports

mod port {
    use super::*;

    // ---------------------------------------------------------- test_l2.py

    pub fn shared_fills_lower_to_the_node_rendezvous() -> Res {
        let text = lw(&example("share2")?, &nodes(2), Strategy::Auto)?;
        let node0 = text.split("    node 1").next().unwrap();
        ensure!(node0.contains("peers=(mg[2])") && node0.contains("peers=(mg[0])"));
        let at = |s: &str| node0.find(s).ok_or(format!("no {s}"));
        let first = at("t2 = mark mg[2]")?;
        let own = at("fill B0 <- w + 262144 n=128")?;
        let shared = at("fill A0 <- x + 0 n=128 peers=(mg[0])")?;
        let wait = at("wait mg[2] upto=t2")?;
        let lead = at("fill A0 <- x + 0 n=128 peers=(mg[2])")?;
        ensure!(own < first && first < shared && shared < wait && wait < lead);
        Ok(())
    }

    pub fn each_pass_writes_its_choice_in() -> Res {
        let t = nodes(2);
        let mut m = read(OPEN)?;
        m = run(&m, &["place"], &t, Strategy::Auto)?;
        ensure!(m.text().contains("on mg[u]") && !m.text().contains("any"));
        m = run(&m, &["space"], &t, Strategy::Auto)?;
        ensure!(m.text().contains("@stage") && !m.text().contains("@mem"));
        m = run(&m, &["share"], &t, Strategy::Auto)?;
        ensure!(m.text().matches("share=cross").count() == 1);
        m = run(&m, &["split"], &t, Strategy::Auto)?;
        ensure!(m.text().contains("for kk in 0..8") && m.text().contains("acc = zero"));
        let l1 = lw(&m, &t, Strategy::Auto)?;
        ensure!(l1.contains("node 0") && l1.contains("node 1"));
        Ok(())
    }

    pub fn retile_takes_the_row_tile_the_issue_model_times_fastest() -> Res {
        let t = target();
        let band = Strategy::Band;
        let l2 = example("softmax")?;
        let eight = read(
            &example_text("softmax")
                .replace("t*16 : +16", "t*8 : +8")
                .replace("[16, 128]", "[8, 128]")
                .replace("f32[16]", "f32[8]")
                .replace("0..32", "0..64"),
        )?;
        let got = run(&eight, &["retile"], &t, band)?;
        ensure!(got.text().contains("for t in 0..32") && got.text().contains("t * 16 : +16"));
        ensure!(got.text().contains("f32[16, 128] @reg"));
        let same = run(&l2, &["retile"], &t, band)?;
        ensure!(lw(&same, &t, band)? == lw(&l2, &t, band)?);
        for name in ["silu", "add"] {
            let k = example(name)?;
            let got = run(&k, &["retile"], &t, band)?;
            ensure!(lw(&got, &t, band)? == lw(&k, &t, band)?, "{name}");
        }
        Ok(())
    }

    pub fn step_runs_trips_too_far_apart_for_ainc_as_a_node_loop() -> Res {
        let t = nodes(1);
        let m = read(FAR)?;
        refused(
            compile(&m, &t, &unstaged_step(), true),
            "ainc takes 18 signed bits",
        )?;
        let got = run(&m, &["step"], &t, Strategy::Auto)?;
        let src = got.text();
        ensure!(src.contains("for t_trip in 0..8"));
        ensure!(src.contains("for t in t_trip..t_trip + 1"));
        ensure!(src.find("for t_trip") < src.find("par c"));
        compile(&m, &t, &PASSES, true)?;
        let near = read(&FAR.replace("70000", "128"))?;
        let kept = run(&near, &["step"], &t, Strategy::Auto)?;
        ensure!(!kept.text().contains("t_trip"));
        ensure!(lw(&kept, &t, Strategy::Auto)? == lw(&near, &t, Strategy::Auto)?);
        Ok(())
    }

    fn unstaged_step() -> Vec<&'static str> {
        PASSES.iter().copied().filter(|p| *p != "step").collect()
    }

    pub fn step_keeps_each_instance_to_its_own_trips() -> Res {
        type Bound = fn(i64) -> i64;
        let cases: [(&str, &str, Bound, Bound); 2] = [
            (
                "0..8 - 3*c",
                "when t_trip < 8 - 3 * c",
                |_| 0,
                |c| 8 - 3 * c,
            ),
            ("2*c..8", "when t_trip >= 2 * c", |c| 2 * c, |_| 8),
        ];
        let t = nodes(1);
        let re = Regex::new(r"send vc\[(\d)\]\n\s+run \w+\(x\+(\d+),").unwrap();
        for (span, when, lo, hi) in cases {
            let m = read(&FAR.replace("for t in 0..8", &format!("for t in {span}")))?;
            let got = run(&m, &["step"], &t, Strategy::Auto)?;
            ensure!(
                got.text().contains("for t_trip in 0..8") && got.text().contains(when),
                "{span}"
            );
            let l1 = compile(&m, &t, &PASSES, true)?;
            let runs: Vec<(i64, i64)> = re
                .captures_iter(&l1)
                .map(|c| (c[1].parse().unwrap(), c[2].parse().unwrap()))
                .collect();
            let want: Vec<(i64, i64)> = (0..8)
                .flat_map(|tr| [0, 1].map(|c| (tr, c)))
                .filter(|&(tr, c)| lo(c) <= tr && tr < hi(c))
                .map(|(tr, c)| (c, (c * 32 + 4 * tr) * 140000))
                .collect();
            ensure!(runs == want, "{span}: {runs:?}");
            ensure!(
                l1.matches("send vc[1]").count() as i64 == hi(1) - lo(1),
                "{span}"
            );
        }
        Ok(())
    }

    pub fn step_finds_a_value_a_trip_carries_from_the_last() -> Res {
        let t = nodes(1);
        let made = FAR.replace("e = add v, 1.0", "e = add v, 1.0\n            f = add e, e");
        let got = run(&read(&made)?, &["step"], &t, Strategy::Auto)?;
        ensure!(got.text().contains("for t_trip in 0..8"));
        let carried = FAR.replace(
            "            e = add v, 1.0 : f32[4, 128] @reg(gt4)\n",
            "            f = add e, e : f32[4, 128] @reg(gt4)\n            e = add v, 1.0 : f32[4, 128] @reg(gt4)\n",
        );
        refused(
            run(&read(&carried)?, &["step"], &t, Strategy::Auto),
            "carries values from trip to trip",
        )
    }

    pub fn retile_fits_a_tile_the_core_does_not_hold() -> Res {
        let t = nodes(1);
        let m = read(&WIDE.replace("256", "512"))?;
        refused(lw(&m, &t, Strategy::Auto), "L1")?;
        let got = run(&m, &["retile"], &t, Strategy::Auto)?;
        let rows = rows_of(got.text()).ok_or("no tile")?;
        ensure!(rows < 16 && 16 % rows == 0, "{rows}");
        lw(&got, &t, Strategy::Auto)?;
        ensure!(got
            .text()
            .contains(&format!("for t in 0..{}", 2 * 16 / rows)));
        Ok(())
    }

    pub fn retile_scales_rows_not_a_column_run_of_the_same_size() -> Res {
        let text = WIDE
            .replace("t*16 : +16, 0 : +256", "t*32 : +32, 0 : +32")
            .replace("[16, 256]", "[32, 32]");
        let (_, stmts) = passes::body_of(&read(&text)?).map_err(|e| e.to_string())?;
        let par = stmts.iter().find(|s| s.op == "par").ok_or("no par")?;
        let got = emit(&[passes::retile::scaled(&par.body[0], "t", 32, 8)]);
        ensure!(
            got.contains("t * 8 : +8, 0 : +32") && got.contains("[8, 32]"),
            "{got}"
        );
        Ok(())
    }

    pub fn retile_scales_a_trip_count_that_depends_on_the_instance() -> Res {
        let text = WIDE
            .replace("256", "512")
            .replace("for t in 0..2", "for t in 0..2 - c");
        let got = run(&read(&text)?, &["retile"], &nodes(1), Strategy::Auto)?;
        let rows = rows_of(got.text()).ok_or("no tile")?;
        let hi = Regex::new(r"for t in 0\.\.(.+)")
            .unwrap()
            .captures(got.text())
            .map(|c| c[1].to_string())
            .ok_or("no loop")?;
        for c in [0, 1] {
            ensure!(
                eval(&hi, &[("c", c)])? * rows == (2 - c) * 16,
                "{hi}, c = {c}"
            );
        }
        Ok(())
    }

    pub fn retile_covers_the_rows_the_trips_did() -> Res {
        let t = nodes(1);
        let wide = WIDE.replace("256", "512");
        let looped = wide
            .replace(
                "    par c in 0..2 on vc[c]\n",
                "    for t_trip in 0..2\n    par c in 0..2 on vc[c]\n",
            )
            .replace("for t in 0..2", "for t in t_trip..t_trip + 1");
        let indented: String = {
            let (head, body) = looped.split_once("    par c").unwrap();
            let body: String = format!("    par c{body}")
                .lines()
                .map(|l| format!("    {l}\n"))
                .collect();
            format!("{head}{body}")
        };
        let cases = [
            (wide.replace("for t in 0..2", "for t in 1..2"), "1"),
            (indented, "t_trip"),
        ];
        let span = Regex::new(r"for t in (.+)\.\.(.+)").unwrap();
        for (text, lo_rows) in cases {
            let got = run(&read(&text)?, &["retile"], &t, Strategy::Auto)?;
            let rows = rows_of(got.text()).ok_or("no tile")?;
            ensure!(rows < 16, "{rows}");
            let (lo, hi) = span
                .captures(got.text())
                .map(|c| (c[1].to_string(), c[2].to_string()))
                .ok_or("no loop")?;
            for trip in [0, 3] {
                let env = [("t_trip", trip)];
                let want = 16 * eval(lo_rows, &env)?;
                ensure!(
                    (eval(&lo, &env)? * rows, eval(&hi, &env)? * rows) == (want, want + 16),
                    "{lo}..{hi} at {rows} rows"
                );
            }
        }
        Ok(())
    }

    pub fn retile_keeps_a_band_made_before_the_loop() -> Res {
        let t = nodes(1);
        let made = Regex::new(r"b = add gv, 2\.0 +: f32\[4, 128\]").unwrap();
        let unpack = Regex::new(r"v = unpack xl +: f32\[(\d+), 128\]").unwrap();
        // The property needs a retile that leaves the band's 4 rows: which
        // input tiles do so follows the machine's timing, so try every one.
        let mut moved = Vec::new();
        for rows in [32, 16, 8, 4, 2, 1] {
            let src = BAND
                .replace("t*16 : +16", &format!("t*{rows} : +{rows}"))
                .replace("[16,", &format!("[{rows},"))
                .replace("for t in 0..2", &format!("for t in 0..{}", 32 / rows));
            let m = read(&src)?;
            let out = run(&m, &["retile"], &t, Strategy::Auto)?;
            ensure!(made.is_match(out.text()), "{rows}");
            let new = rows_of(out.text()).ok_or("no tile")?;
            let got = unpack
                .captures(out.text())
                .map(|c| c[1].parse::<i64>().unwrap());
            ensure!(got == Some(new), "{rows} -> {new}: {got:?}");
            lw(&out, &t, Strategy::Auto)?;
            if new != 4 {
                moved.push((rows, new));
            }
        }
        ensure!(
            !moved.is_empty(),
            "no input tile retiles away from the band's 4 rows"
        );
        Ok(())
    }

    pub fn retile_splits_columns_no_row_tile_holds() -> Res {
        let t = nodes(1);
        let ty = Regex::new(r": \w+\[([^\]]*)\]").unwrap();
        for src in [SPLIT.to_string(), whole(), xlane()] {
            let m = read(&src)?;
            ensure!(lw(&m, &t, Strategy::Auto).is_err());
            let got = run(&m, &["retile"], &t, Strategy::Auto)?;
            let body = got.text().split_once("fn split.l2(").unwrap().1;
            let mut pars: Vec<Vec<&str>> = Vec::new();
            for ln in body.lines().skip(1) {
                if ln.starts_with("    par ") {
                    pars.push(Vec::new());
                } else if let Some(p) = pars.last_mut() {
                    p.push(ln.trim());
                }
            }
            ensure!(pars.len() == 2, "{} pars", pars.len());
            let w = 512 / pars.len() as i64;
            let mut names = Vec::new();
            let mut runs: HashMap<String, Vec<(i64, i64)>> = HashMap::new();
            for p in &pars {
                for ln in p {
                    let (target, rest) = match ln.split_once(" = ") {
                        Some((a, b)) if !a.contains(' ') => (Some(a), b),
                        _ => (None, *ln),
                    };
                    names.extend(target);
                    let mut words = rest.split_whitespace();
                    let opn = words.next().unwrap_or("");
                    if opn == "load" || opn == "store" {
                        let view = rest[opn.len() + 1..].trim();
                        let (buf, sl) =
                            view.split_once('[').ok_or(format!("a whole view: {ln}"))?;
                        let sl = &sl[..sl.find(']').unwrap()];
                        let last = sl.rsplit(',').next().unwrap();
                        let (lo, n) = last.split_once(" : +").ok_or(format!("a run: {ln}"))?;
                        runs.entry(buf.to_string())
                            .or_default()
                            .push((lo.trim().parse().unwrap(), n.trim().parse().unwrap()));
                    }
                    if opn != "store" {
                        if let Some(c) = ty.captures_iter(ln).last() {
                            let dim = c[1].rsplit(',').next().unwrap().trim().to_string();
                            ensure!(dim == w.to_string(), "{ln}");
                        }
                    }
                }
            }
            let distinct: BTreeSet<&&str> = names.iter().collect();
            ensure!(distinct.len() == names.len(), "{names:?}");
            for buf in ["x", "g", "h", "out"] {
                let mut got = runs.get(buf).cloned().unwrap_or_default();
                got.sort();
                let want: Vec<(i64, i64)> = (0..2).map(|j| (j * w, w)).collect();
                ensure!(got == want, "{buf}: {got:?}");
            }
            lw(&got, &t, Strategy::Auto)?;
        }
        Ok(())
    }

    pub fn retile_refuses_when_no_tile_lowers_naming_why() -> Res {
        let text = WIDE
            .replace("256", "8192")
            .replace("t*16 : +16", "t*32 : +32")
            .replace("[16, ", "[32, ")
            .replace("for t in 0..2", "for t in 0..1")
            .replace(
                "            e = add v, 1.0 : f32[32, 8192] @reg(gt4)\n            p = pack e : f16[32, 8192] @l1(gt4)\n            store out[c*32 + t*32 : +32, 0 : +8192] <- p\n",
                "            s = rmax v : f32[32] @reg\n            p = pack s : f16[32, 32] @l1(gt4)\n            store out[c*32 + t*32 : +32, 0 : +32] <- p\n",
            );
        ensure!(text.contains("rmax"));
        refused(
            run(&read(&text)?, &["retile"], &nodes(1), Strategy::Auto),
            "no row tile of the stream lowers",
        )
    }

    pub fn space_turns_an_any_tile_into_a_transfer() -> Res {
        let t = nodes(1);
        let any = compile(&example("anysilu")?, &t, &PASSES, true)?;
        let direct = compile(&example("tilesilu")?, &t, &PASSES, true)?;
        ensure!(any.replace("anysilu", "tilesilu") == direct);
        Ok(())
    }

    pub fn space_keeps_an_any_tile_too_big_to_land_in_memory() -> Res {
        let src = example_text("mmsilu");
        let (params, rest) = src.split_once("    par u").unwrap();
        let text = format!(
            "{}    h = buffer : f16[256, 256] @any(tile=64x64)\n    par u{rest}",
            params.replace("h: f16[256, 256] @dram(tile=64x64), ", "")
        );
        let got = run(&read(&text)?, &["space"], &target(), Strategy::Auto)?;
        ensure!(got.text().contains(": f16[256, 256] @stage(tile=64x64)"));
        ensure!(got.text().contains("drain acc -> h") && !got.text().contains("recv"));
        Ok(())
    }

    pub fn pipeline_lets_a_node_loop_of_two_unit_kinds_run_ahead() -> Res {
        let t = target();
        let bare = read(&example_text("tilesilu").replace(" pipe=2", ""))?;
        let got = run(&bare, &["pipeline"], &t, Strategy::Auto)?;
        ensure!(got.text().contains("for q in 0..16 pipe=2"));
        let mm = run(&example("mmsilu")?, &["pipeline"], &t, Strategy::Auto)?;
        ensure!(!mm.text().contains("pipe"));
        Ok(())
    }

    pub fn a_gemm_too_big_for_a_bank_is_refused_unless_split() -> Res {
        let text = OPEN.replace(" split", "").replace("mg[any]", "mg[u]");
        refused(
            lw(&read(&text)?, &nodes(2), Strategy::Auto),
            "a bank holds 256",
        )
    }

    pub fn cluster_refusals() -> Res {
        for (line, said) in [
            (
                "a = fill x[0 : +256, 32 : +64] : mx7a[256, 64] @A",
                "one packed chunk",
            ),
            (
                "a = fill x[0 : +255, 0 : +64] : mx7a[255, 64] @A",
                "whole groups",
            ),
            (
                "a = fill w[0 : +256, 0 : +64] : mx7b[256, 64] @A",
                "holds mx7a",
            ),
        ] {
            let text = format!(
                "target ktpu.v9\nfn k.l2(x: mx7a[512, 512] @dram(gm=64, nk=2), w: mx7b[512, 512] @dram(gn=64, nk=2))\n    par u in 0..1 on mg[u]\n        {line}\n"
            );
            refused(lw(&read(&text)?, &target(), Strategy::Auto), said)?;
        }
        Ok(())
    }

    pub fn a_vector_kernel_lowers_to_a_loop_of_two_iterations() -> Res {
        for name in ["silu", "add", "softmax", "layernorm"] {
            let text = lw(&example(name)?, &target(), Strategy::Band)?;
            let image = first_image(&text)?;
            ensure!(
                image.contains("loop s15") && image.matches("vdrain").count() == 2,
                "{name}"
            );
            ensure!(image.matches("vbar").count() >= 2, "{name}");
        }
        Ok(())
    }

    pub fn an_mxfp7_store_walks_the_a_packing() -> Res {
        let text = lw(&example("mlp")?, &target(), Strategy::Band)?;
        ensure!(text.contains("desc a1 = p_a1 walk=((32, 8), (16384, 4))"));
        ensure!(text.contains("ainc a1 += 256"));
        ensure!(Regex::new(r"run mlp_n0_vc0_0\([^)]*\ba2\+0\b")
            .unwrap()
            .is_match(&text));
        let strip = |s: &str| {
            s.split_once('[')
                .map(|x| x.1)
                .unwrap_or("")
                .trim_end_matches([':', ']', ','])
                .to_string()
        };
        let moves: Vec<Vec<&str>> = text
            .lines()
            .filter(|l| l.trim().ends_with("vl=16"))
            .map(|l| l.split_whitespace().collect::<Vec<_>>())
            .filter(|m| m.len() > 2 && m[1].contains('['))
            .collect();
        let pairs: BTreeSet<(String, String)> = moves
            .iter()
            .take(8)
            .map(|m| (strip(m[1]), strip(m[2])))
            .collect();
        let want: BTreeSet<(String, String)> = (0..4)
            .flat_map(|i| (0..2).map(move |q| ((2 * i + q).to_string(), (4 * q + i).to_string())))
            .collect();
        ensure!(pairs == want, "{pairs:?}");
        Ok(())
    }

    pub fn a_large_conversion_is_cut_into_whole_moves() -> Res {
        let text = "target ktpu.v9\nfn k.l2(x: f16[1024, 1024] @dram, a: mx7a[1024, 1024] @dram(gm=64, nk=1))\n    quant a <- x\n";
        let l1 = lw(&read(text)?, &target(), Strategy::Auto)?;
        let lines: Vec<&str> = l1.lines().filter(|l| l.contains("quant")).collect();
        ensure!(lines.len() == 4, "{lines:?}");
        ensure!(
            lines
                .iter()
                .all(|l| l.split("->").next().unwrap().contains("(256, 2048)")),
            "{lines:?}"
        );
        Ok(())
    }

    pub fn a_large_move_is_cut_into_whole_moves_covering_the_walk() -> Res {
        let w = |n: &str, lv: &[(i64, i64)]| -> Walk { (n.to_string(), 0, lv.to_vec()) };
        let cases: Vec<(&str, &str, Vec<Walk>)> = vec![
            (
                "x: f16[2048, 1024] @dram, y: f16[2048, 1024] @dram",
                "copy y <- x",
                vec![w("x", &[(131072, 32)]), w("y", &[(131072, 32)])],
            ),
            (
                "x: f16[1024, 512] @dram, y: f16[1024, 512] @dram",
                "copy y <- x",
                vec![w("x", &[(32768, 32)]), w("y", &[(32768, 32)])],
            ),
            (
                "x: f16[1024, 2048] @dram, y: f16[1024, 1536] @dram",
                "copy y <- x[:, 0:1536]",
                vec![w("x", &[(1024, 4096), (96, 32)]), w("y", &[(98304, 32)])],
            ),
            (
                "x: f16[1024, 1536] @dram, y: f16[1024, 2048] @dram",
                "copy y <- x walk=((2, 1572864), (49152, 32)) dwalk=((1024, 4096), (96, 32))",
                vec![w("x", &[(98304, 32)]), w("y", &[(1024, 4096), (96, 32)])],
            ),
            (
                "y: f16[4096, 1024] @dram",
                "fill y value=0",
                vec![w("y", &[(262144, 32)])],
            ),
            (
                "y: f16[3, 70000, 16] @dram",
                "fill y value=7 walk=((3, 2240000), (70000, 32))",
                vec![w("y", &[(3, 2240000), (70000, 32)])],
            ),
        ];
        for (decl, stmt, want) in cases {
            let text = lw(
                &read(&mover_program(decl, stmt))?,
                &target(),
                Strategy::Auto,
            )?;
            let lines = mover_lines(&text, stmt.split(' ').next().unwrap());
            ensure!(lines.len() > 1, "{stmt}");
            let mut got = Vec::new();
            for ln in &lines {
                let ws = walks(ln);
                ensure!(ws.len() == want.len(), "{ln}");
                ensure!(
                    ws.iter().all(|w| addresses(w).len() as i64 <= MOVE_WORDS),
                    "{ln}"
                );
                ensure!(ws.iter().all(|w| w.2.iter().all(|l| l.0 <= 0xFFFF)), "{ln}");
                got.extend(zipped(&ws));
            }
            let mut want = zipped(&want);
            got.sort();
            want.sort();
            ensure!(got == want, "{stmt}");
        }
        Ok(())
    }

    pub fn a_large_transform_is_cut_into_few_whole_moves() -> Res {
        let text = "target ktpu.v9\nfn k.l2(x: f16[13824, 32] @dram, t: f16[55296, 16] @dram)\n    gt4 t <- x walk=((3456, 256), (2, 32), (4, 64)) dwalk=((3456, 512), (2, 128))\n";
        let lines = mover_lines(&lw(&read(text)?, &target(), Strategy::Auto)?, "gt4");
        ensure!(lines.len() == 2, "{}", lines.len());
        let mut got = Vec::new();
        for ln in &lines {
            let ws = walks(ln);
            ensure!(ws.len() == 2);
            ensure!(addresses(&ws[0]).len() as i64 <= MOVE_WORDS);
            let (src, dst) = (addresses(&ws[0]), addresses(&ws[1]));
            got.extend((0..dst.len()).map(|k| (src[4 * k..4 * k + 4].to_vec(), dst[k].clone())));
        }
        let src = addresses(&("x".into(), 0, vec![(3456, 256), (2, 32), (4, 64)]));
        let dst = addresses(&("t".into(), 0, vec![(3456, 512), (2, 128)]));
        let want: Vec<_> = (0..dst.len())
            .map(|k| (src[4 * k..4 * k + 4].to_vec(), dst[k].clone()))
            .collect();
        ensure!(got == want);
        Ok(())
    }

    pub fn a_large_move_whose_walks_do_not_nest_is_cut_step_for_step() -> Res {
        let want = vec![
            ("x".to_string(), 0, vec![(3, 262144), (8192, 32)]),
            ("y".to_string(), 0, vec![(2, 393216), (12288, 32)]),
        ];
        let lines = pieces_cover(
            "x: f16[24576, 16] @dram, y: f16[24576, 16] @dram",
            "copy y <- x walk=((3, 262144), (8192, 32)) dwalk=((2, 393216), (12288, 32))",
            "copy",
            &want,
        )?;
        ensure!(lines.len() > 1);
        Ok(())
    }

    pub fn a_copy_of_more_levels_than_a_walker_holds_is_cut() -> Res {
        let src = vec![
            (2, 32),
            (3, 64),
            (2, 192),
            (3, 384),
            (2, 1152),
            (2, 2304),
            (2, 4608),
        ];
        let walk: Vec<String> = src.iter().map(|(n, s)| format!("({n}, {s})")).collect();
        let want = vec![
            ("x".to_string(), 0, src.clone()),
            ("y".to_string(), 0, vec![(288, 32)]),
        ];
        let lines = pieces_cover(
            "x: f16[288, 16] @dram, y: f16[288, 16] @dram",
            &format!("copy y <- x walk=({}) dwalk=((288, 32),)", walk.join(", ")),
            "copy",
            &want,
        )?;
        ensure!(lines.len() > 1);
        Ok(())
    }

    pub fn a_gather_past_one_moves_index_list_or_row_words_is_cut() -> Res {
        let t = target();
        let re = Regex::new(
            r"gather t \+ (\d+) -> (w\(.*\)) index=i \+ (\d+) n=(\d+) pitch=(\d+) words=(\d+)",
        )
        .unwrap();
        for (rows, cols) in [(5000i64, 64i64), (40, 1 << 18)] {
            let text = lw(
                &read(&format!(
                    "target ktpu.v9\nfn k.l2(t: f16[64, {cols}] @dram, i: u32[{rows}] @dram, y: f16[{rows}, {cols}] @dram)\n    gather y <- t index=i\n"
                ))?,
                &t,
                Strategy::Auto,
            )?;
            let row_words = cols / 16;
            let mut seen = Vec::new();
            for ln in text
                .lines()
                .map(str::trim)
                .filter(|l| l.starts_with("gather "))
            {
                let m = re.captures(ln).ok_or(ln.to_string())?;
                let n = |k: usize| m[k].parse::<i64>().unwrap();
                let (col, idx, cnt, pitch, words) = (n(1), n(3), n(4), n(5), n(6));
                let dst = addresses(&walks(&m[2])[0]);
                ensure!(
                    cnt <= t.max_index && idx % 32 == 0 && words <= 0xFFFF,
                    "{ln}"
                );
                ensure!(pitch == cols * 2 && col % 32 == 0, "{ln}");
                for r in 0..cnt {
                    for c in 0..words {
                        seen.push((idx / 4 + r, col / 32 + c, dst[(r * words + c) as usize].1));
                    }
                }
            }
            seen.sort();
            let want: Vec<(i64, i64, i64)> = (0..rows)
                .flat_map(|r| (0..row_words).map(move |c| (r, c, (r * row_words + c) * 32)))
                .collect();
            ensure!(seen == want, "{rows} x {cols}");
        }
        Ok(())
    }

    /// The walks of `pieces` side by side, word for word.
    fn pieces_walked(pieces: &[(Vec<i64>, Vec<Levels>)]) -> Vec<Vec<(String, i64)>> {
        pieces
            .iter()
            .flat_map(|(offs, lvs)| {
                let ws: Vec<Walk> = offs
                    .iter()
                    .zip(lvs)
                    .map(|(o, lv)| ("w".to_string(), *o, lv.clone()))
                    .collect();
                zipped(&ws)
            })
            .collect()
    }

    fn whole_walked(ws: &[Levels]) -> Vec<Vec<(String, i64)>> {
        let ws: Vec<Walk> = ws
            .iter()
            .map(|lv| ("w".to_string(), 0, lv.clone()))
            .collect();
        zipped(&ws)
    }

    pub fn cut_pieces_take_every_step_in_order_within_the_limits() -> Res {
        let t = target();
        let mut x: u64 = 0x2545_f491_4f6c_dd1d;
        let mut next = |m: u64| {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            x % m
        };
        let totals: Vec<i64> = (0..9)
            .flat_map(|a| {
                (0..3)
                    .flat_map(move |b| (0..3).map(move |c| 2i64.pow(a) * 3i64.pow(b) * 5i64.pow(c)))
            })
            .collect();
        for seed in 0..40 {
            let total = totals[next(totals.len() as u64) as usize];
            let mut walk = |mut total: i64| -> Levels {
                let mut counts = Vec::new();
                while total > 1 {
                    let ds: Vec<i64> = (2..=total).filter(|d| total % d == 0).take(6).collect();
                    let f = ds[next(ds.len() as u64) as usize];
                    counts.push(f);
                    total /= f;
                }
                let lv: Levels = counts
                    .iter()
                    .map(|&n| (n, 32 * [1, 2, 3, -1, 7][next(5) as usize]))
                    .collect();
                if lv.is_empty() {
                    vec![(1, 32)]
                } else {
                    lv
                }
            };
            let ws = vec![walk(total), walk(total)];
            let limit = [1, 2, 7, 64, 1000, MOVE_WORDS][next(6) as usize];
            let pieces = cut(&ws, limit, None, 0, &t);
            for (_, lvs) in &pieces {
                let n: i64 = lvs[0].iter().map(|l| l.0).product();
                ensure!(n <= limit.max(1), "seed {seed}: {n} steps past {limit}");
                ensure!(
                    lvs.iter()
                        .all(|lv| ref_merge(&ref_squeeze(lv)).len() <= t.levels),
                    "seed {seed}: {lvs:?}"
                );
            }
            ensure!(
                pieces_walked(&pieces) == whole_walked(&ws),
                "seed {seed}: {ws:?}"
            );
        }
        Ok(())
    }

    pub fn a_window_beside_a_dense_walk_is_cut_at_its_levels() -> Res {
        let ws = vec![
            vec![(8, 36992), (32, 1088), (32, 32), (3, 1088), (3, 32)],
            vec![(73728, 32)],
        ];
        let pieces = cut(&ws, MOVE_WORDS, None, 0, &target());
        ensure!(pieces.len() == 8, "{} pieces", pieces.len());
        ensure!(pieces_walked(&pieces) == whole_walked(&ws));
        Ok(())
    }

    pub fn three_walks_split_only_at_boundaries_that_nest() -> Res {
        let ws = vec![
            vec![(6, 32)],
            vec![(2, 96), (3, 32)],
            vec![(3, 64), (2, 32)],
        ];
        let pieces = cut(&ws, 1, None, 0, &target());
        ensure!(pieces_walked(&pieces) == whole_walked(&ws));
        ensure!(pieces.len() == 6, "{} pieces of one step", pieces.len());
        Ok(())
    }

    pub fn a_small_move_stays_one_line() -> Res {
        let text = "target ktpu.v9\nfn k.l2(x: f16[64, 128] @dram, y: f16[64, 128] @dram)\n    copy y <- x\n";
        let l1 = lw(&read(text)?, &target(), Strategy::Auto)?;
        ensure!(mover_lines(&l1, "copy") == ["copy w(x + 0, (512, 32)) -> w(y + 0, (512, 32))"]);
        Ok(())
    }

    fn stage_lines(text: &str) -> Vec<String> {
        text.split("fn ")
            .nth(1)
            .unwrap_or("")
            .lines()
            .skip(1)
            .map(|l| l.trim().to_string())
            .collect()
    }

    pub fn stages_wait_only_for_what_they_touch() -> Res {
        let text = concat!(
            "target ktpu.v9\n",
            "fn k.l2(x: f16[64, 128] @dram, y: f16[64, 128] @dram, z: f16[64, 128] @dram)\n",
            "    copy y <- x\n",
            "    par c in 0..1 on vc[0]\n",
            "        for t in 0..16\n",
            "            xl = load x[t*4 : +4, :] : f16[4, 128] @l1\n",
            "            v = unpack xl : f32[4, 128] @reg\n",
            "            p = pack v : f16[4, 128] @l1\n",
            "            store z[t*4 : +4, :] <- p\n",
            "    copy x <- z\n",
        );
        let lines = stage_lines(&lw(&read(text)?, &target(), Strategy::Auto)?);
        let run_at = lines
            .iter()
            .position(|l| l.starts_with("run "))
            .ok_or("no run")?;
        ensure!(!lines[..run_at].iter().any(|l| l.starts_with("wait")));
        let last = lines.iter().position(|l| l == "move").ok_or("no move")?;
        ensure!(
            run_at <= last
                && lines[run_at..last]
                    .iter()
                    .any(|l| l == "wait vc[0] upto=k1_vc0")
        );
        ensure!(!lines.iter().any(|l| l == "barrier"));
        Ok(())
    }

    pub fn pipe_sends_the_next_tile_before_waiting() -> Res {
        let lines = stage_lines(&lw(&example("tilesilu")?, &target(), Strategy::Auto)?);
        let first_wait = lines
            .iter()
            .position(|l| l.starts_with("wait"))
            .ok_or("no wait")?;
        let drains: Vec<usize> = (0..lines.len())
            .filter(|&i| lines[i].starts_with("drain n=128 to=vc[0]"))
            .collect();
        ensure!(drains.len() >= 2 && drains[0] < drains[1] && drains[1] < first_wait);
        ensure!(lines[drains[0]].contains("l1=256") && lines[drains[1]].contains("l1=384"));
        Ok(())
    }

    pub fn transfer_refusals() -> Res {
        for (body, said) in [
            (
                "    par c in 0..1 on vc[0]\n        h = recv mg[0] : f16[4, 32] @l1(gt4)\n",
                "sent nothing",
            ),
            (
                "    par u in 0..1 on mg[0]\n        a = fill x : mx7a[4, 64] @A\n        b = fill w : mx7b[4, 64] @B\n        c = gemm a, b : f32[4, 4] @acc\n        drain c -> vc[0]\n",
                "nothing receives",
            ),
        ] {
            let text = format!(
                "target ktpu.v9\nfn k.l2(x: mx7a[4, 64] @dram(gm=1, nk=2), w: mx7b[4, 64] @dram(gn=1, nk=2))\n{body}"
            );
            refused(lw(&read(&text)?, &target(), Strategy::Auto), said)?;
        }
        Ok(())
    }

    pub fn sticky_rewrites_its_values_in_place() -> Res {
        let text = lw(&example("lazysoftmax")?, &target(), Strategy::Auto)?;
        let image = first_image(&text)?;
        let after = image.split_once("skipz s14\n").ok_or("no skipz")?.1;
        let block: Vec<&str> = after
            .split('\n')
            .take_while(|l| l.starts_with(&" ".repeat(12)))
            .map(str::trim)
            .collect();
        let dest = |l: &str| {
            l.split_whitespace()
                .nth(1)
                .unwrap_or("")
                .trim_end_matches(',')
                .to_string()
        };
        let first: Vec<String> = image
            .lines()
            .filter(|l| l.contains("vexp2d"))
            .take(8)
            .map(dest)
            .collect();
        let redo: Vec<String> = block
            .iter()
            .filter(|l| l.starts_with("vexp2d"))
            .map(|l| dest(l))
            .collect();
        ensure!(redo == first, "{redo:?} vs {first:?}");
        ensure!(image.contains("vsync fe on=math\n        getstk s14"));
        Ok(())
    }

    pub fn sticky_refusals() -> Res {
        for (body, said) in [
            (
                "            if sticky\n                q = mul v, 2.0 : f32[8, 128] @reg\n            o = mul q, 1.0 : f32[8, 128] @reg\n",
                "only inside if sticky",
            ),
            (
                "            if sticky\n                xl = load x[t*8 : +8, :] : f16[8, 128] @l1\n            o = mul v, 1.0 : f32[8, 128] @reg\n",
                "math and reductions",
            ),
        ] {
            let text = format!(
                "target ktpu.v9\nfn k.l2(x: f16[64, 128] @dram, y: f16[64, 128] @dram)\n    par c in 0..1 on vc[0]\n        for t in 0..8\n            xl = load x[t*8 : +8, :] : f16[8, 128] @l1\n            v = unpack xl : f32[8, 128] @reg\n{body}            ol = pack o : f16[8, 128] @l1\n            store y[t*8 : +8, :] <- ol\n"
            );
            refused(lw(&read(&text)?, &target(), Strategy::Auto), said)?;
        }
        Ok(())
    }

    pub fn a_gt4_band_reduces_to_one_register_every_chunk_holds() -> Res {
        let text = lw(&example("mmsoftmax")?, &target(), Strategy::Auto)?;
        let lines: Vec<&str> = first_image(&text)?.lines().map(str::trim).collect();
        let at = lines
            .iter()
            .position(|l| *l == "vmax v4[0:], v4[0:], v4[4:] vl=64")
            .ok_or("no fold")?;
        ensure!(
            at >= 3
                && lines[at - 3..at] == ["vmax v4, v0, v1", "vmax v5, v2, v3", "vmax v4, v4, v5"]
        );
        let bits: Vec<char> = lines[at + 1..at + 5]
            .iter()
            .map(|l| {
                l.split("xor(v4, ")
                    .nth(1)
                    .and_then(|x| x.chars().next())
                    .unwrap_or('?')
            })
            .collect();
        ensure!(bits == ['8', '4', '2', '1'], "{bits:?}");
        ensure!(lines[at + 5] == "vmov v4[4:], v4[0:] vl=64");
        ensure!(lines
            .iter()
            .any(|l| l.starts_with("vexp2d v0, v") && l.ends_with(", v4")));
        Ok(())
    }

    pub fn a_compare_is_a_predicate_and_two_moves() -> Res {
        let text = lw(&example("leaky")?, &target(), Strategy::Band)?;
        let image = first_image(&text)?;
        ensure!(image.contains("vcmpgt p1, v0, k0") && image.contains("k1 if=p1"));
        ensure!(image.matches("vsel ").count() == 16);
        Ok(())
    }

    pub fn select_turns_a_sign_select_of_a_scale_into_max_or_min() -> Res {
        let cases: [(&str, &str, &str, Option<&str>); 7] = [
            ("cmpgt v, 0.0", "0.1", "m, v, s", Some("max v, s")),
            ("cmpgt v, 0.0", "0.1", "m, s, v", Some("min v, s")),
            ("cmplt v, 0.0", "0.1", "m, s, v", Some("max v, s")),
            ("cmpgt 0.0, v", "0.1", "m, s, v", Some("max v, s")),
            ("cmpgt v, 0.0", "4.0", "m, v, s", Some("min v, s")),
            ("cmpgt v, 0.0", "-0.5", "m, v, s", None),
            ("cmpgt v, 1.0", "0.1", "m, v, s", None),
        ];
        for (cmp, a, arms, want) in cases {
            let src = example_text("leaky")
                .replace("cmpgt v, 0.0", cmp)
                .replace("mul v, 0.1", &format!("mul v, {a}"))
                .replace("sel m, v, s", &format!("sel {arms}"));
            let got = run(&read(&src)?, &["select"], &target(), Strategy::Auto)?;
            let got = got.text();
            match want {
                None => ensure!(got.contains(&format!("sel {arms}")), "{cmp} {a} {arms}"),
                Some(w) => ensure!(
                    got.contains(&format!("o = {w}"))
                        && !got.contains("cmp")
                        && !got.contains("sel"),
                    "{cmp} {a} {arms}"
                ),
            }
        }
        Ok(())
    }

    pub fn modulo_pipelines_the_stream_loop_through_rings() -> Res {
        let text = lw(
            &vregs(&example("softmax")?, 128),
            &regs(target(), 128),
            Strategy::Modulo,
        )?;
        let image = first_image(&text)?.split("\n    send").next().unwrap();
        ensure!(image.contains("loop s15"));
        for s in [
            "vsync fill on=unpack",
            "vsync pack on=drain",
            "vsync unpack on=fill",
            "vsync drain on=pack",
        ] {
            ensure!(image.contains(s), "{s}");
        }
        Ok(())
    }

    pub fn a_vector_stage_in_a_loop_with_clusters_takes_the_shared_latency() -> Res {
        let t = target();
        let m = machine();
        ensure!(t.mem(false) == (m.vc.fill_alone.0 as i64, m.vc.fill_alone.1 as i64));
        ensure!(t.mem(true) == (m.vc.fill_shared.0 as i64, m.vc.fill_shared.1 as i64));
        let other = m
            .with_fact("vc.fill_shared", json!([7, 3]))
            .map_err(|e| e.to_string())?;
        let o = Target::of(&other).with_nodes(4);
        ensure!(o.mem(true) == (7, 3) && o.mem(false) == t.mem(false));
        let src = example_text("mmsilu");
        let (head, rest) = src.split_once("    par u").unwrap();
        let body: String = format!("    par u{rest}")
            .lines()
            .map(|l| format!("    {l}\n"))
            .collect();
        let looped = format!("{head}    for i in 0..2\n{body}");
        let (alone, beside) = (read(&src)?, read(&looped)?);
        ensure!(lw(&alone, &t, Strategy::Auto)? == lw(&alone, &o, Strategy::Auto)?);
        ensure!(lw(&beside, &t, Strategy::Auto)? != lw(&beside, &o, Strategy::Auto)?);
        Ok(())
    }

    pub fn a_fill_lands_the_memory_latency_after_its_last_word() -> Res {
        ensure!(lead(64, (330, 5)) == 330 + 5 * 64);
        ensure!(lead(64, (16, 1)) + 384 == lead(64, (400, 1)));
        Ok(())
    }

    pub fn modulo_rotates_the_predicate_over_its_copies() -> Res {
        let image = lw(
            &vregs(&example("leaky")?, 128),
            &regs(target(), 128),
            Strategy::Modulo,
        )?;
        for s in ["vcmpgt p1,", "vcmpgt p2,", "if=p1", "if=p2"] {
            ensure!(image.contains(s), "{s}");
        }
        Ok(())
    }

    pub fn an_image_past_imem_is_refused_and_retiled_to_one_that_fits() -> Res {
        let t = nodes(1);
        for (n, most) in [(80, 8), (160, 4)] {
            let m = read(&chain_of(n))?;
            refused(
                lw(&m, &t, Strategy::Auto),
                "no image of the stream loop encodes",
            )?;
            let got = run(&m, &["retile"], &t, Strategy::Auto)?;
            let rows = rows_of(got.text()).ok_or("no tile")?;
            ensure!(rows <= most, "{n}: {rows}");
            lw(&got, &t, Strategy::Auto)?;
        }
        Ok(())
    }

    pub fn a_row_unpacked_before_the_loop_is_pinned_or_unpacked_where_read() -> Res {
        for pinned in [true, false] {
            pinned_or_unpacked(pinned).map_err(|e| format!("pin={pinned}: {e}"))?;
        }
        Ok(())
    }

    fn pinned_or_unpacked(pinned: bool) -> Res {
        let text = forced(&read(PIN)?, &nodes(1), Strategy::Band, None, Some(pinned))?;
        let image = images(&text)?;
        let g = first_word_at(r"vfill a\d+ -> l1\[(\d+)\]\n", image)?;
        let (head, lp) = image.split_once("loop s15").ok_or("no loop")?;
        let reads = Regex::new(&format!(r"vunpk \S+ <- l1\[{g}\]")).unwrap();
        let before = reads.find_iter(head).count();
        ensure!(before == pinned as usize && reads.is_match(lp) != pinned);
        if !pinned {
            let fills = Regex::new(r"vfill a\d+ -> l1\[(\d+)\] rel").unwrap();
            let packs = Regex::new(r"vpack \S+ -> l1\[(\d+)\]").unwrap();
            let spans: Vec<(i64, i64)> = fills
                .captures_iter(lp)
                .map(|c| c[1].parse::<i64>().unwrap())
                .map(|w| (w, w + 64))
                .chain(
                    packs
                        .captures_iter(lp)
                        .map(|c| c[1].parse::<i64>().unwrap())
                        .map(|w| (w, w + 8)),
                )
                .collect();
            ensure!(
                spans.iter().all(|&(a, b)| b <= g || g + 8 <= a),
                "{spans:?}"
            );
        }
        let got = packed(image, 0.0, Tag::Word, None)?;
        ensure!(got.len() == 32, "{} packs", got.len());
        for p in &got {
            let Val::Op(o, a) = p else {
                return Err(format!("{p:?}"));
            };
            ensure!(
                o == "vmul" && a[1] == op("vadd", vec![Val::X(Some(g)), Val::N(2.0)]),
                "{p:?}"
            );
            ensure!(matches!(a[0], Val::X(Some(w)) if w != g), "{p:?}");
        }
        Ok(())
    }

    pub fn a_shape_seen_before_is_not_searched_again() -> Res {
        let blocks = [
            (0, 128, "2.0"),
            (128, 128, "2.0"),
            (256, 128, "2.0"),
            (384, 64, "2.0"),
            (448, 64, "3.0"),
        ];
        let pars: String = blocks
            .iter()
            .enumerate()
            .map(|(j, (at, w, k))| {
                BLOCK
                    .replace("{j}", &j.to_string())
                    .replace("{at}", &at.to_string())
                    .replace("{w}", &w.to_string())
                    .replace("{k}", k)
            })
            .collect();
        let m = read(&format!("{BLOCKS}{pars}"))?;
        let t = nodes(1);
        let search = Search::new(Strategy::Auto);
        let remembered = lower::lower(&m, &t, &search).map_err(|e| e.to_string())?;
        ensure!(
            search.chosen().len() == 3,
            "{} shapes",
            search.chosen().len()
        );
        let timed = search.timed();
        let again = lower::lower(&m, &t, &search).map_err(|e| e.to_string())?;
        ensure!(again == remembered && search.chosen().len() == 3 && search.timed() == timed);
        ensure!(lw(&m, &t, Strategy::Auto)? == remembered);
        Ok(())
    }

    pub fn a_row_unpacked_before_the_loop_packs_in_it() -> Res {
        for pin in [true, false] {
            let text = forced(&read(PACKED)?, &nodes(1), Strategy::Band, None, Some(pin))?;
            let image = images(&text)?;
            let g = first_word_at(r"vfill a\d+ -> l1\[(\d+)\]\n", image)?;
            let got: Vec<Val> = packed(image, 0.0, Tag::Word, None)?
                .into_iter()
                .filter(|p| matches!(p, Val::X(_)))
                .collect();
            let want: Vec<Val> = (0..4)
                .flat_map(|_| (0..8).map(|r| Val::X(Some(g + 8 * r))))
                .collect();
            ensure!(got == want, "pin={pin}: {got:?}");
        }
        Ok(())
    }

    pub fn a_row_not_pinned_reaches_a_reduction_through_its_product() -> Res {
        let text = forced(&read(FOLDED)?, &nodes(1), Strategy::Band, None, Some(false))?;
        let image = images(&text)?;
        let g = first_word_at(r"vfill a\d+ -> l1\[(\d+)\]\n", image)?;
        let (head, lp) = image.split_once("loop s15").ok_or("no loop")?;
        let reads = Regex::new(&format!(r"vunpk \S+ <- l1\[{g}")).unwrap();
        ensure!(!reads.is_match(head) && reads.is_match(lp));
        Ok(())
    }

    pub fn a_row_not_pinned_keeps_its_bytes_or_is_no_candidate() -> Res {
        let m = read(KEEP)?;
        forced(&m, &nodes(1), Strategy::Band, None, Some(true))?;
        refused(
            forced(&m, &nodes(1), Strategy::Band, None, Some(false)),
            "vector registers",
        )
    }

    pub fn a_value_stored_through_two_views_is_packed_once_for_both() -> Res {
        for trips in [2i64, 3, 4] {
            let rows = 8 * trips;
            let src = TWIN
                .replace("{rows2}", &(2 * rows).to_string())
                .replace("{rows}", &rows.to_string())
                .replace("{trips}", &trips.to_string());
            let text = lw(&read(&src)?, &nodes(1), Strategy::Auto)?;
            let mut drains = Drains::new();
            let packs = packed(images(&text)?, 0.0, Tag::Count, Some(&mut drains))?;
            ensure!(packs.len() as i64 == rows, "{trips}: {} packs", packs.len());
            let names: Vec<&str> = drains.iter().map(|d| d.0.as_str()).collect();
            let want: Vec<&str> = (0..trips).flat_map(|_| ["a1", "a2"]).collect();
            ensure!(names == want, "{trips}: {names:?}");
            for (k, (_, got)) in drains.iter().enumerate() {
                let trip = k as i64 / 2;
                let want: Vec<Option<Val>> = (0..8)
                    .map(|r| {
                        Some(op(
                            "vmul",
                            vec![Val::X(Some(8 * trip + r + 1)), Val::N(2.0)],
                        ))
                    })
                    .collect();
                ensure!(*got == want, "{trips}: drain {k}");
            }
        }
        Ok(())
    }

    pub fn a_value_stored_in_two_l1_layouts_is_packed_into_each() -> Res {
        let text = lw(&read(LAYOUTS)?, &nodes(1), Strategy::Auto)?;
        let mut drains = Drains::new();
        let packs = packed(images(&text)?, 0.0, Tag::Count, Some(&mut drains))?;
        ensure!(packs.len() == 8, "{} packs", packs.len());
        let names: BTreeSet<&str> = drains.iter().map(|d| d.0.as_str()).collect();
        ensure!(names == BTreeSet::from(["a1", "a2"]) && drains.len() == 8);
        let mut seen: HashMap<&str, i64> = HashMap::new();
        for (d, got) in &drains {
            let trip = seen.entry(d.as_str()).or_insert(-1);
            *trip += 1;
            let want = op("vmul", vec![Val::X(Some(*trip + 1)), Val::N(2.0)]);
            ensure!(got[0] == Some(want), "{d} trip {trip}: {:?}", got[0]);
        }
        Ok(())
    }

    pub fn a_staged_constant_is_set_again_past_a_skipped_block() -> Res {
        let (chain, mut want) = consts(16);
        let mut g = String::new();
        for i in 1..=16 {
            let from = if i == 1 {
                "e16".to_string()
            } else {
                format!("g{}", i - 1)
            };
            let c = 1.0 + i as f64 / 64.0;
            g += &format!("            g{i} = mul {from}, {c} : f32[8, 128] @reg\n");
            want = op("vmul", vec![want, Val::N(c)]);
        }
        let h = op("vfma", vec![want, Val::N(3.25), Val::N(3.5)]);
        let src = STICKY.replace("{chain}", &format!("{chain}{g}"));
        let text = forced(&read(&src)?, &nodes(1), Strategy::Auto, Some(false), None)?;
        let image = images(&text)?;
        let staging = Regex::new(r"vfma \S+, \S+, (s13, k3|k3, s13)").unwrap();
        ensure!(image.contains("skipz") && staging.is_match(image));
        for (sticky, k) in [
            (0.0, op("vmul", vec![h.clone(), Val::N(3.75)])),
            (
                1.0,
                op(
                    "vmul",
                    vec![op("vmul", vec![h.clone(), Val::N(3.75)]), Val::N(3.75)],
                ),
            ),
        ] {
            let got = packed(image, sticky, Tag::None, None)?;
            ensure!(
                got.len() == 16 && got.iter().all(|x| *x == k),
                "sticky {sticky}"
            );
        }
        Ok(())
    }

    pub fn constants_past_the_scalar_file_are_held_or_staged() -> Res {
        let set = Regex::new(r"seti (s13|k3) = ").unwrap();
        for hold in [true, false] {
            for (n, trips) in [(18usize, 2i64), (18, 3), (40, 2)] {
                let (chain, want) = consts(n);
                let src = CONSTS
                    .replace("{chain}", &chain)
                    .replace("{n}", &n.to_string())
                    .replace("{trips}", &trips.to_string())
                    .replace("{rows}", &(8 * trips).to_string());
                let text = forced(&read(&src)?, &nodes(1), Strategy::Auto, Some(hold), None)?;
                let image = images(&text)?;
                let got = packed(image, 0.0, Tag::None, None)?;
                let at = format!("hold={hold} {n}/{trips}");
                ensure!(got.len() as i64 == 8 * trips, "{at}: {} packs", got.len());
                ensure!(got.iter().all(|x| *x == want), "{at}");
                let looped = &image[image.find("loop s15").ok_or("no loop")?..];
                ensure!(set.is_match(looped) == !hold, "{at}: staged");
                ensure!(image.contains("vmov") == hold, "{at}: held");
            }
        }
        Ok(())
    }

    pub fn a_view_off_a_word_is_refused() -> Res {
        let t = nodes(1);
        let near = FAR.replace("70000", "256");
        let bad = near.replace(
            "load x[c*32 + t*4 : +4, 0 : +128]",
            "load x[c*32 + t*4 : +4, 8 : +128]",
        );
        refused(
            lw(&read(&bad)?, &t, Strategy::Auto),
            "a descriptor starts on a 32-byte word",
        )?;
        let steps = near.replace(
            "load x[c*32 + t*4 : +4, 0 : +128]",
            "load x[c*32 : +4, t*8 : +128]",
        );
        refused(
            lw(&read(&steps)?, &t, Strategy::Auto),
            "a descriptor steps whole words",
        )
    }

    pub fn a_store_of_a_value_not_the_views_size_is_refused() -> Res {
        let bad = FAR
            .replace("store out[c*32 + t*4 : +4, 0 : +128] <- p", "store out[c*32 + t*4 : +4, 0 : +128] <- q")
            .replace(
                "            p = pack e : f16[4, 128] @l1(gt4)\n",
                "            p = pack e : f16[4, 128] @l1(gt4)\n            q = load x[0 : +4, 0 : +32] : f16[4, 32] @l1(gt4)\n",
            )
            .replace("70000", "128");
        refused(
            lw(&read(&bad)?, &nodes(1), Strategy::Auto),
            "the view holds 32 words, the value 8",
        )
    }

    pub fn a_gt4_sum_of_squares_folds_the_product() -> Res {
        let text = lw(
            &vregs(&read(&norm(256))?, 128),
            &regs(target(), 128),
            Strategy::Band,
        )?;
        let image = first_image(&text)?;
        let trip = image
            .split("loop s15")
            .nth(1)
            .ok_or("no loop")?
            .split("vpack")
            .next()
            .unwrap();
        let reg = Regex::new(r"^v\d+(?:\[[^\]]*\])?$").unwrap();
        let folds = trip
            .lines()
            .filter_map(|l| l.trim().strip_prefix("vfma "))
            .filter(|rest| {
                let a: Vec<&str> = rest.split(", ").collect();
                a.len() == 4 && a.iter().all(|x| reg.is_match(x)) && a[2] == a[1] && a[3] == a[0]
            })
            .count();
        ensure!(folds >= 4, "{folds} folds");
        Ok(())
    }

    pub fn a_store_without_l1_room_packs_over_its_trips_load() -> Res {
        let text = lw(
            &vregs(&read(&norm(1024))?, 128),
            &regs(target(), 128),
            Strategy::Band,
        )?;
        let image = first_image(&text)?;
        ensure!(image.contains("vsync pack on=unpack") && image.contains("vsync fill on=drain"));
        let set = |p: &str| -> BTreeSet<String> {
            Regex::new(p)
                .unwrap()
                .captures_iter(image)
                .map(|c| c[1].to_string())
                .collect()
        };
        let loads = set(r"vfill a\d+ -> l1\[(\d+)\] rel");
        let drains = set(r"vdrain a\d+ <- l1\[(\d+)\] rel");
        ensure!(
            !drains.is_empty() && drains.is_subset(&loads),
            "{drains:?} {loads:?}"
        );
        Ok(())
    }

    pub fn a_wide_band_past_the_registers_is_refused() -> Res {
        refused(
            lw(
                &vregs(&read(&norm(1024))?, 64),
                &regs(target(), 64),
                Strategy::Auto,
            ),
            "needs more than 64 vector registers",
        )
    }

    pub fn modulo_emission_reads_every_value_it_wrote() -> Res {
        let lower_ln = || {
            lw(
                &vregs(&read(&norm(256))?, 128),
                &regs(target(), 128),
                Strategy::Modulo,
            )
        };
        lower_ln()?;
        #[cfg(feature = "mutants")]
        {
            use ktpu_l2::mutant::{scoped, State};
            let one = State {
                active: Some("l2.vector/colour-one"),
                ..State::default()
            };
            refused(scoped(one, lower_ln), "which holds")?;
        }
        Ok(())
    }

    fn mop(eng: char, reads: Vec<String>, writes: Vec<String>, occ: i64) -> MOp {
        MOp {
            op: String::new(),
            #[cfg(feature = "witness")]
            tmpl: String::new(),
            syms: vec![],
            eng,
            reads,
            writes,
            occ,
            l1: None,
            t: 0,
            e: 0,
            deps: vec![],
            xreads: vec![],
        }
    }

    type Owner = HashMap<char, Vec<Option<usize>>>;
    type Queue = BinaryHeap<Reverse<(i64, usize)>>;

    /// `place` with its first fit found slot by slot: the reference.
    fn place_by_scan(ops: &mut [MOp], ii: i64) -> bool {
        let n = ops.len();
        if ops.iter().any(|o| o.occ > ii) {
            return false;
        }
        let pred: Vec<Vec<(usize, i64)>> = ops
            .iter()
            .map(|o| {
                o.deps
                    .iter()
                    .map(|&(p, lat, end)| (p, lat + if end { ops[p].occ } else { 0 }))
                    .collect()
            })
            .collect();
        let mut succ: Vec<Vec<(usize, i64)>> = vec![Vec::new(); n];
        for (i, ps) in pred.iter().enumerate() {
            for &(p, d) in ps {
                succ[p].push((i, d));
            }
        }
        let mut height = vec![0i64; n];
        for i in (0..n).rev() {
            height[i] = succ[i]
                .iter()
                .map(|&(s, d)| d + height[s])
                .max()
                .unwrap_or(ops[i].occ);
        }
        let slot = |t: i64| t.rem_euclid(ii) as usize;
        let mut owner: Owner = "MUP"
            .chars()
            .map(|e| (e, vec![None; ii as usize]))
            .collect();
        let mut at: Vec<Option<i64>> = vec![None; n];
        let mut last: Vec<Option<i64>> = vec![None; n];
        let mut queue: Queue = (0..n).map(|i| Reverse((-height[i], i))).collect();
        let mut budget = (PLACE_BUDGET + 1) * n as i64;
        let evict = |j: usize, owner: &mut Owner, at: &mut [Option<i64>], queue: &mut Queue| {
            let row = owner.get_mut(&ops[j].eng).unwrap();
            for b in 0..ops[j].occ {
                row[slot(at[j].unwrap() + b)] = None;
            }
            at[j] = None;
            queue.push(Reverse((-height[j], j)));
        };
        while let Some(Reverse((_, i))) = queue.pop() {
            if at[i].is_some() {
                continue;
            }
            budget -= 1;
            if budget < 0 {
                return false;
            }
            let (eng, occ) = (ops[i].eng, ops[i].occ);
            let early = pred[i]
                .iter()
                .filter_map(|&(p, d)| at[p].map(|a| a + d))
                .max()
                .unwrap_or(0);
            let lo = match last[i] {
                Some(l) if l >= early => l + 1,
                _ => early,
            };
            let free = |owner: &Owner, t: i64| (0..occ).all(|b| owner[&eng][slot(t + b)].is_none());
            let t = match (lo..lo + ii).find(|&t| free(&owner, t)) {
                Some(t) => t,
                None => {
                    let held: BTreeSet<usize> =
                        (0..occ).filter_map(|b| owner[&eng][slot(lo + b)]).collect();
                    for j in held {
                        evict(j, &mut owner, &mut at, &mut queue);
                    }
                    lo
                }
            };
            for b in 0..occ {
                owner.get_mut(&eng).unwrap()[slot(t + b)] = Some(i);
            }
            at[i] = Some(t);
            last[i] = Some(t);
            for &(s, d) in &succ[i] {
                if at[s].is_some_and(|a| a < t + d) {
                    evict(s, &mut owner, &mut at, &mut queue);
                }
            }
        }
        for (i, o) in ops.iter_mut().enumerate() {
            o.t = at[i].unwrap();
            o.e = o.t + o.occ;
        }
        true
    }

    pub fn modulo_placement_takes_the_first_free_run() -> Res {
        let timing = core().timing;
        let mut rng = Rng(0x9e37_79b9_7f4a_7c15);
        for seed in 0..60 {
            let n = rng.range(1, 40) as usize;
            let ops: Vec<MOp> = (0..n)
                .map(|i| {
                    let eng = ['M', 'M', 'M', 'U', 'P'][rng.below(5) as usize];
                    let k = rng.range(0, 2);
                    let reads = if i == 0 {
                        vec![]
                    } else {
                        (0..k)
                            .map(|_| format!("%{}", rng.below(i as u64)))
                            .collect()
                    };
                    mop(eng, reads, vec![format!("%{i}")], rng.range(1, 12))
                })
                .collect();
            let lower = ['M', 'U', 'P']
                .iter()
                .map(|e| {
                    ops.iter()
                        .filter(|o| o.eng == *e)
                        .map(|o| o.occ)
                        .sum::<i64>()
                })
                .max()
                .unwrap();
            for ii in [lower, lower + rng.range(0, 30), rng.range(1, lower.max(2))] {
                let (mut a, mut b) = (ops.clone(), ops.clone());
                internals::link(&mut a, &timing);
                internals::link(&mut b, &timing);
                ensure!(
                    internals::place(&mut a, ii) == place_by_scan(&mut b, ii),
                    "seed {seed} ii {ii}"
                );
                let at = |x: &[MOp]| x.iter().map(|o| (o.t, o.e)).collect::<Vec<_>>();
                ensure!(at(&a) == at(&b), "seed {seed} ii {ii}");
            }
        }
        Ok(())
    }

    type Phys = HashMap<(String, i64), i64>;

    /// `colour` checking each register's every arc: the reference.
    fn colour_by_overlap(life: &Life, ii: i64, k: i64, regs: i64, first: i64) -> Option<Phys> {
        let period = ii * k;
        let mut arcs = Vec::new();
        for (x, (s, e)) in life {
            if e - s > period {
                return None;
            }
            for j in 0..k {
                let a = (s + j * ii).rem_euclid(period);
                arcs.push((a, a + e - s, x.clone(), j));
            }
        }
        arcs.sort();
        let overlap = |a: (i64, i64), b: (i64, i64)| {
            [-period, 0, period]
                .iter()
                .any(|sh| a.0 < b.1 + sh && b.0 + sh < a.1)
        };
        let mut busy: HashMap<i64, Vec<(i64, i64)>> = HashMap::new();
        let mut out = HashMap::new();
        for (s, e, x, j) in arcs {
            let r = (first..regs).find(|r| {
                busy.get(r)
                    .is_none_or(|ivs| ivs.iter().all(|iv| !overlap((s, e), *iv)))
            })?;
            busy.entry(r).or_default().push((s, e));
            out.insert((x, j), r);
        }
        Some(out)
    }

    pub fn modulo_colouring_matches_the_arc_by_arc_check() -> Res {
        let mut rng = Rng(0x2545_f491_4f6c_dd1d);
        for seed in 0..80 {
            let (ii, k) = (rng.range(1, 40), rng.range(1, 6));
            let life: Life = (0..rng.range(1, 30))
                .map(|i| {
                    let s = rng.range(0, 3 * ii);
                    (format!("%{i}"), (s, s + rng.range(0, ii * k + 2)))
                })
                .collect();
            let (regs, first) = (rng.range(1, 24), rng.range(0, 2));
            ensure!(
                internals::colour(&life, ii, k, regs, first)
                    == colour_by_overlap(&life, ii, k, regs, first),
                "seed {seed}"
            );
        }
        Ok(())
    }

    pub fn modulo_stops_once_ii_spans_an_iteration() -> Res {
        let mut ops: Vec<MOp> = (0..40)
            .map(|i| mop('M', vec![], vec![format!("%{i}")], 1))
            .collect();
        ops.push(mop(
            'M',
            (0..40).map(|i| format!("%{i}")).collect(),
            vec!["%z".into()],
            1,
        ));
        let got = plan(&ops, 16, &HashMap::new(), 8, 100_000, 0, 0, &core());
        let tried = internals::last_ii();
        ensure!(got.is_none() && tried > 0 && tried < 200, "{tried}");
        Ok(())
    }

    pub fn modulo_parse_reads_an_ops_own_destination() -> Res {
        let s = |xs: &[&str]| xs.iter().map(|x| x.to_string()).collect::<Vec<_>>();
        let o = parse(
            "vadd {0}, {0}, merge({0}[4 :], 4) vl=64",
            &s(&["%1"]),
            4,
            &core(),
        );
        ensure!(
            o.reads == s(&["%1"]) && o.writes == s(&["%1"]) && o.occ == 4,
            "{o:?}"
        );
        ensure!(o.xreads == s(&["%1"]), "{o:?}");
        let o = parse(
            "vadd {0}[4 :], {1}[0 :], {1}[4 :] vl=64",
            &s(&["%1", "%2"]),
            4,
            &core(),
        );
        ensure!(
            o.reads == s(&["%2"]) && o.writes == s(&["%1"]) && o.xreads.is_empty(),
            "{o:?}"
        );
        Ok(())
    }

    pub fn modulo_placement_runs_each_op_back_to_back() -> Res {
        let core = core();
        let mut ops: Vec<MOp> = [
            ("vunpk {0} <- l1[0] f16", vec!["%1"]),
            ("vmul {0}, {1}, s0", vec!["%2", "%1"]),
            ("vmax {0}[0 :], {1}[0 :], {1}[4 :] vl=64", vec!["%3", "%2"]),
            ("vadd {0}, {1}, merge({1}, 8)", vec!["%4", "%3"]),
            ("vmul {0}, {1}, s0", vec!["%5", "%2"]),
            ("vpack {0} -> l1[64] f16", vec!["%5"]),
        ]
        .into_iter()
        .map(|(t, s)| {
            let s: Vec<String> = s.into_iter().map(str::to_string).collect();
            parse(t, &s, 4, &core)
        })
        .collect();
        internals::link(&mut ops, &core.timing);
        let ii = 30;
        ensure!(internals::place(&mut ops, ii));
        for eng in ['M', 'U', 'P'] {
            let slots: Vec<i64> = ops
                .iter()
                .filter(|o| o.eng == eng)
                .flat_map(|o| (0..o.occ).map(move |b| (o.t + b).rem_euclid(ii)))
                .collect();
            let distinct: BTreeSet<i64> = slots.iter().copied().collect();
            ensure!(distinct.len() == slots.len(), "{eng}: {slots:?}");
        }
        for o in &ops {
            ensure!(o.e - o.t == o.occ);
            for &(p, lat, end) in &o.deps {
                ensure!(o.t >= if end { ops[p].e } else { ops[p].t } + lat, "{o:?}");
            }
        }
        Ok(())
    }

    pub fn modulo_colouring_compares_arcs_on_the_circle() -> Res {
        let life: Life = vec![("%a".into(), (0, 100)), ("%b".into(), (1800, 1900))];
        let phys = internals::colour(&life, 300, 3, 8, 0).ok_or("no colouring")?;
        for j in 0..3 {
            ensure!(
                phys[&("%a".to_string(), j)] != phys[&("%b".to_string(), j)],
                "{j}"
            );
        }
        Ok(())
    }

    pub fn a_vector_body_past_its_registers_is_refused() -> Res {
        let text = "target ktpu.v9\nfn k.l2(x: f16[40, 128] @dram)\n    par c in 0..1 on vc[c]\n        xl = load x : f16[40, 128] @l1\n        v = unpack xl : f32[40, 128] @reg\n";
        refused(
            lw(&read(text)?, &target(), Strategy::Auto),
            "needs more than 32 vector registers",
        )
    }

    // ------------------------------------------------------- l2/test_stage.py

    pub fn a_one_word_move_goes_through_a_contiguous_scratch() -> Res {
        for (params, stmt, want) in rewrites() {
            let got = staged(&read(&kernel(&params, &[stmt]))?, &nodes(1))?;
            ensure!(statements(&got) == want, "{stmt}: {:?}", statements(&got));
        }
        Ok(())
    }

    pub fn a_staged_move_in_a_loop_keeps_its_symbolic_offsets() -> Res {
        let m = read(&kernel(
            &format!("{TAB}, y: f16[21, 128] @dram"),
            &[
                "for i in 0..2",
                "    gather y[i : +20, i*16 : +16] <- t index=idx",
            ],
        ))?;
        let got = statements(&staged(&m, &nodes(1))?);
        ensure!(
            got == [
                "h0 = buffer : f16[20, 16] @dram",
                "for i in 0..2",
                "gather h0 <- t index=idx",
                "copy y[i : +20, i * 16 : +16] <- h0",
            ],
            "{got:?}"
        );
        Ok(())
    }

    pub fn each_staged_move_takes_its_own_scratch_after_the_written_ones() -> Res {
        let m = read(&kernel(
            &format!("{TAB}, y: f16[21, 128] @dram, h0: f16[8] @dram"),
            &[
                "s = buffer : f16[21, 128] @dram",
                "gather y[:, 0 : +16] <- t index=idx",
                "scatter t <- s[:, 0 : +16] index=idx",
            ],
        ))?;
        let got = statements(&staged(&m, &nodes(1))?);
        ensure!(
            got == [
                "s = buffer : f16[21, 128] @dram",
                "h1 = buffer : f16[21, 16] @dram",
                "h2 = buffer : f16[21, 16] @dram",
                "gather h1 <- t index=idx",
                "copy y[:, 0 : +16] <- h1",
                "copy h2 <- s[:, 0 : +16]",
                "scatter t <- h2 index=idx",
            ],
            "{got:?}"
        );
        Ok(())
    }

    pub fn a_move_on_several_nodes_stages_through_a_slab_each() -> Res {
        let m = read(&kernel(
            &format!("{TAB}, y: f16[16, 128] @dram"),
            &[
                "par n in 0..2 on node[n]",
                "    gather y[n*8 : +8, 16 : +16] <- t index=idx[n*8 : +8]",
            ],
        ))?;
        let got = statements(&staged(&m, &nodes(2))?);
        ensure!(
            got == [
                "h0 = buffer : f16[2, 8, 16] @dram",
                "par n in 0..2 on node[n]",
                "gather h0[n] <- t index=idx[n * 8 : +8]",
                "copy y[n * 8 : +8, 16 : +16] <- h0[n]",
            ],
            "{got:?}"
        );
        Ok(())
    }

    pub fn a_move_with_a_longer_run_on_one_side_is_kept() -> Res {
        let kept: [(String, &str); 7] = [
            (
                format!("{TAB}, y: f16[21, 16] @dram"),
                "gather y <- t index=idx",
            ),
            (
                "t: f16[64, 32] @dram, idx: u32[24] @dram, y: f16[21, 128] @dram".into(),
                "gather y[:, 32 : +32] <- t index=idx",
            ),
            (
                "t: f16[64, 7, 16] @dram, idx: u32[24] @dram, y: f16[3, 8, 128] @dram".into(),
                "gather y[:, 0 : +7, 16 : +16] <- t index=idx",
            ),
            (
                format!("{TAB}, s: f16[21, 16] @dram"),
                "scatter t <- s index=idx",
            ),
            (
                "x: f16[8, 16] @dram, y: f16[8, 128] @dram".into(),
                "copy y[:, 32 : +16] <- x",
            ),
            (
                "x: f16[8, 128] @dram, y: f16[8, 16] @dram".into(),
                "copy y <- x[:, 32 : +16]",
            ),
            (
                "x: f16[8, 256] @dram, y: f16[16, 128] @dram".into(),
                "copy y <- x perm=(1, 0)",
            ),
        ];
        for (params, stmt) in kept {
            let m = read(&kernel(&params, &[stmt]))?;
            ensure!(staged(&m, &nodes(1))?.text() == m.text(), "{stmt}");
            ensure!(l1_moves(&m, &PASSES)?.is_empty(), "{stmt}");
        }
        Ok(())
    }

    pub fn a_move_whose_instances_want_different_scratches_is_kept_and_found() -> Res {
        let m = read(&kernel(
            &format!("{TAB}, y: f16[21, 128] @dram"),
            &[
                "for i in 0..2",
                "    gather y[0 : 20 + i, i*16 : +16] <- t index=idx",
            ],
        ))?;
        ensure!(staged(&m, &nodes(1))?.text() == m.text());
        ensure!(l1_moves(&m, &PASSES)? == ["gather", "gather"]);
        Ok(())
    }

    pub fn a_move_of_one_word_is_kept_and_still_found() -> Res {
        let m = read(&kernel(
            "x: f16[16] @dram, y: f16[4, 16] @dram",
            &["copy y[2] <- x"],
        ))?;
        ensure!(staged(&m, &nodes(1))?.text() == m.text());
        ensure!(l1_moves(&m, &PASSES)? == ["copy"]);
        Ok(())
    }

    pub fn a_mover_that_takes_one_word_runs_keeps_every_move() -> Res {
        for (params, stmt, _) in rewrites() {
            let m = read(&kernel(&params, &[stmt]))?;
            ensure!(staged(&m, &safe())?.text() == m.text(), "{stmt}");
        }
        Ok(())
    }

    pub fn the_lowered_l1_holds_no_one_word_move() -> Res {
        for (params, stmt, _) in rewrites() {
            let m = read(&kernel(&params, &[stmt]))?;
            ensure!(!l1_moves(&m, &unstaged())?.is_empty(), "{stmt}: unstaged");
            ensure!(l1_moves(&m, &PASSES)?.is_empty(), "{stmt}: staged");
        }
        Ok(())
    }

    pub fn the_staged_moves_run_in_order_in_one_mover_step() -> Res {
        let (params, stmt, _) = rewrites().remove(0);
        let l1 = compile(&read(&kernel(&params, &[stmt]))?, &nodes(1), &PASSES, true)?;
        let lines: Vec<&str> = l1.lines().map(str::trim).collect();
        let into = lines
            .iter()
            .position(|l| l.starts_with("gather t+0 -> w(h0"))
            .ok_or("no gather")?;
        let out = lines
            .iter()
            .position(|l| l.starts_with("copy w(h0"))
            .ok_or("no copy")?;
        ensure!(into < out);
        ensure!(!lines[into..out]
            .iter()
            .any(|l| l.ends_with("move posted") || *l == "move"));
        Ok(())
    }

    pub fn a_staged_move_waits_for_the_last_iterations_read_of_its_scratch() -> Res {
        let m = read(&kernel(
            &format!("{TAB}, y: f16[21, 128] @dram, x: f16[8, 128] @dram, z: f16[8, 128] @dram"),
            &[
                "for i in 0..2",
                "    gather y[:, i*16 : +16] <- t index=idx",
                "    par c in 0..1 on vc[0]",
                "        for r in 0..1",
                "            v = load x[i*4 : +4, 0 : +128] : f16[4, 128] @l1",
                "            w = unpack v : f32[4, 128] @reg",
                "            p = pack w : f16[4, 128] @l1",
                "            store z[i*4 : +4, 0 : +128] <- p",
            ],
        ))?;
        let l1 = compile(&m, &nodes(1), &PASSES, false)?;
        let plus = Regex::new(r" ?\+ ?").unwrap();
        let lines: Vec<String> = l1
            .split_once("fn k.l1")
            .ok_or("no fn")?
            .1
            .lines()
            .map(|l| plus.replace_all(l.trim(), "+").to_string())
            .collect();
        let into: Vec<usize> = (0..lines.len())
            .filter(|&i| lines[i].starts_with("gather t+0 -> w(h0"))
            .collect();
        let out: Vec<usize> = (0..lines.len())
            .filter(|&i| lines[i].starts_with("copy w(h0"))
            .collect();
        ensure!(into.len() == 2 && out.len() == 2, "{into:?} {out:?}");
        ensure!(into[0] < out[0] && out[0] < into[1] && into[1] < out[1]);
        let step = (1..into[1])
            .rev()
            .find(|&i| lines[i].ends_with("move posted"))
            .ok_or("no step")?;
        ensure!(
            out[0] < step
                && lines[out[0]..step]
                    .iter()
                    .any(|l| l.starts_with("wait moves"))
        );
        ensure!(one_word_moves(&l1).is_empty());
        Ok(())
    }

    pub fn the_check_reads_each_sides_innermost_run() -> Res {
        for (line, one) in [
            ("copy w(x+0, (8, 256)) -> w(y+0, (8, 512))", true),
            ("copy w(x+0, (8, 32)) -> w(y+0, (8, 512))", false),
            ("copy w(x+0, (8, 256)) -> w(y+0, (8, 32))", false),
            ("copy w(x+0, (2, 512), (4, 32)) -> w(y+0, (8, 256))", false),
            ("copy w(x+0, (2, 128), (4, 32)) -> w(y+0, (8, 256))", false),
            ("copy w(x+0, (4, 32), (2, 128)) -> w(y+0, (8, 256))", true),
            (
                "copy w(x+0, (8, 32)) -> w(y+0, (8, 32)) nocoal burst=1",
                true,
            ),
            ("copy w(x+0, (8, 256)) -> w(y+0, (8, 32)) nocoal", true),
            ("copy w(x+0, (8, -32)) -> w(y+0, (8, 256))", true),
            ("copy w(x+0, (8, 32)) -> w(y+0, (8, 32))", false),
            ("copy w(x+0, (n, 32)) -> w(y+0, (8, 256))", true),
            (
                "gather t+0 -> w(y+0, (21, 256)) index=i+0 n=21 pitch=32 words=1",
                true,
            ),
            (
                "gather t+0 -> w(y+0, (21, 32)) index=i+0 n=21 pitch=32 words=1",
                false,
            ),
            (
                "gather t+0 -> w(y+0, (21, 256)) index=i+0 n=21 pitch=64 words=2",
                false,
            ),
            (
                "scatter w(s+0, (21, 256)) -> t+0 index=i+0 n=21 pitch=32 words=1 fp",
                true,
            ),
            (
                "scatter w(s+0, (21, 32)) -> t+0 index=i+0 n=21 pitch=32 words=1",
                false,
            ),
            ("fill w(y+0, (8, 256)) value=0", false),
        ] {
            ensure!(one_word(line) == one, "{line}");
        }
        Ok(())
    }

    pub fn the_check_finds_moves_inside_blocks() -> Res {
        let (params, stmt, _) = rewrites().remove(0);
        let l1 = lw(&read(&kernel(&params, &[stmt]))?, &nodes(1), Strategy::Auto)?;
        ensure!(one_word_moves(&l1) == ["gather"]);
        Ok(())
    }

    // ---------------------------------------------------------- test_banks.py

    fn leaky() -> Vec<Event> {
        let s = |i| Symbol(i).operand(Select::Whole);
        let op = |stmt, ids: &[u32]| Event::Op {
            stmt,
            syms: ids.iter().copied().map(Symbol).collect(),
        };
        vec![
            op(
                ir::memory(ir::Memory::Unpack, s(1), 0, Some("f16"), false, vec![]).unwrap(),
                &[1],
            ),
            op(
                ir::math("vmul", s(2), vec![s(1), Term::name("s0")], vec![]),
                &[2, 1],
            ),
            op(ir::math("vmax", s(3), vec![s(1), s(2)], vec![]), &[3, 1, 2]),
            op(
                ir::memory(ir::Memory::Pack, s(3), 64, Some("f16"), false, vec![]).unwrap(),
                &[3],
            ),
        ]
    }

    fn core() -> Core {
        Core::of(&target())
    }

    /// The register-file bank each source of the `vmax` (the beat reading x
    /// and 0.1 x) reads at every beat: chunk `base + stride * beat` mod the
    /// chunks, its bank that chunk mod `vc.rf_banks`.
    fn vmax_banks(code: &[Event], core: &Core) -> Result<Vec<(i64, i64)>, String> {
        let st = code
            .iter()
            .find_map(|c| match c {
                Event::Op { stmt, .. } if stmt.op == "vmax" => Some(stmt),
                _ => None,
            })
            .ok_or("no vmax")?;
        let walk = |t: &Term| -> Result<(i64, i64), String> {
            match t {
                Term::Name(_) => Ok((0, 1)),
                Term::View(_, args) => match args.as_slice() {
                    [Term::Slice(Some(b), None)] => Ok((b.as_int().unwrap() as i64, 1)),
                    [b] => Ok((b.as_int().unwrap() as i64, 0)),
                    _ => Err("select".into()),
                },
                _ => Err("operand".into()),
            }
        };
        let (a, b) = (walk(&st.args[1])?, walk(&st.args[2])?);
        let bank = |(base, stride): (i64, i64), beat: i64| {
            (base + stride * beat).rem_euclid(core.chunks) % core.rf_banks as i64
        };
        Ok((0..core.chunks).map(|i| (bank(a, i), bank(b, i))).collect())
    }

    pub fn two_reads_of_one_beat_go_to_opposite_banks() -> Res {
        let core = core();
        let before = vmax_banks(&leaky(), &core)?;
        ensure!(before.iter().all(|(a, b)| a == b), "{before:?}");
        let after = vmax_banks(&banks::rotate(leaky(), 2, &core), &core)?;
        ensure!(after.iter().all(|(a, b)| a != b), "{after:?}");
        let rho = banks::choose(&leaky(), 2, &core);
        let rotated: Vec<&String> = ["%1", "%2"]
            .iter()
            .filter_map(|s| rho.keys().find(|k| k == s))
            .collect();
        ensure!(rotated.len() == 1, "{rho:?}");
        let placed = banks::rotate(leaky(), 2, &core);
        ensure!(placed != leaky());
        ensure!(placed.len() == leaky().len());
        for c in &placed {
            if let Event::Op { stmt, .. } = c {
                if stmt.op == "vunpk" && matches!(&stmt.args[0], Term::View(_, _)) {
                    ensure!(stmt.kw("n").and_then(Term::as_int) == Some(8), "{stmt:?}");
                }
            }
        }
        Ok(())
    }

    pub fn four_copies_need_no_placement() -> Res {
        ensure!(banks::rotate(leaky(), 4, &core()) == leaky());
        ensure!(banks::choose(&leaky(), 4, &core()).is_empty());
        Ok(())
    }
}

// ------------------------------------------------- the tests and their mutants

macro_rules! ports {
    ($($name:ident => $kill:expr,)*) => {
        $(
            #[test]
            fn $name() {
                if let Err(e) = port::$name() {
                    panic!("{e}");
                }
            }
        )*
        /// Each port and the mutant that kills it (`None`: no mutant of the
        /// crate reaches it).
        #[allow(dead_code)]
        const PORTS: &[(&str, fn() -> Res, Option<&str>)] = &[$((stringify!($name), port::$name, $kill),)*];
    };
}

ports! {
    shared_fills_lower_to_the_node_rendezvous => Some("l2.lower/share-unpaired"),
    each_pass_writes_its_choice_in => Some("l2.passes/split-off"),
    retile_takes_the_row_tile_the_issue_model_times_fastest => Some("l2.vector/slowest-first"),
    step_runs_trips_too_far_apart_for_ainc_as_a_node_loop => Some("l2.passes/step-near"),
    step_keeps_each_instance_to_its_own_trips => Some("l2.passes/step-when-dropped"),
    step_finds_a_value_a_trip_carries_from_the_last => Some("l2.passes/step-carried-ignored"),
    retile_fits_a_tile_the_core_does_not_hold => Some("l2.passes/retile-bounds-unscaled"),
    retile_scales_rows_not_a_column_run_of_the_same_size => Some("l2.passes/retile-columns-scaled"),
    retile_scales_a_trip_count_that_depends_on_the_instance => Some("l2.passes/retile-bounds-unscaled"),
    retile_covers_the_rows_the_trips_did => Some("l2.passes/retile-bounds-unscaled"),
    retile_keeps_a_band_made_before_the_loop => Some("l2.passes/retile-invariant-scaled"),
    retile_splits_columns_no_row_tile_holds => Some("l2.passes/retile-no-columns"),
    retile_refuses_when_no_tile_lowers_naming_why => Some("l2.passes/retile-keeps-unlowered"),
    space_turns_an_any_tile_into_a_transfer => Some("l2.passes/space-off"),
    space_keeps_an_any_tile_too_big_to_land_in_memory => Some("l2.passes/space-off"),
    pipeline_lets_a_node_loop_of_two_unit_kinds_run_ahead => Some("l2.passes/pipeline-off"),
    a_gemm_too_big_for_a_bank_is_refused_unless_split => Some("l2.cluster/bank-128"),
    cluster_refusals => Some("l2.memory/lo-ignored"),
    a_vector_kernel_lowers_to_a_loop_of_two_iterations => Some("l2.vector/loop-s13"),
    an_mxfp7_store_walks_the_a_packing => Some("l2.buffer/mx7-layout-unread"),
    a_large_conversion_is_cut_into_whole_moves => Some("l2.mover/no-cut"),
    a_large_move_is_cut_into_whole_moves_covering_the_walk => Some("l2.mover/no-cut"),
    a_large_transform_is_cut_into_few_whole_moves => Some("l2.mover/no-cut"),
    a_large_move_whose_walks_do_not_nest_is_cut_step_for_step => Some("l2.mover/no-cut"),
    a_copy_of_more_levels_than_a_walker_holds_is_cut => Some("l2.mover/ndim-7"),
    a_gather_past_one_moves_index_list_or_row_words_is_cut => Some("l2.mover/pitch-doubled"),
    cut_pieces_take_every_step_in_order_within_the_limits => Some("l2.mover/no-cut"),
    a_window_beside_a_dense_walk_is_cut_at_its_levels => Some("l2.mover/no-cut"),
    three_walks_split_only_at_boundaries_that_nest => Some("l2.mover/no-cut"),
    a_small_move_stays_one_line => Some("l2.mover/no-merge"),
    stages_wait_only_for_what_they_touch => Some("l2.sync/never-clash"),
    pipe_sends_the_next_tile_before_waiting => Some("l2.sync/no-ahead"),
    transfer_refusals => Some("l2.lower/unreceived-ignored"),
    sticky_rewrites_its_values_in_place => Some("l2.vector/stk-s13"),
    sticky_refusals => Some("l2.vector/sticky-unchecked"),
    a_gt4_band_reduces_to_one_register_every_chunk_holds => Some("l2.vector/band-no-broadcast"),
    a_compare_is_a_predicate_and_two_moves => Some("l2.vector/cmp-swapped"),
    select_turns_a_sign_select_of_a_scale_into_max_or_min => Some("l2.passes/select-off"),
    modulo_pipelines_the_stream_loop_through_rings => Some("l2.vector/no-modulo"),
    a_vector_stage_in_a_loop_with_clusters_takes_the_shared_latency => Some("l2.lower/fill-alone"),
    a_fill_lands_the_memory_latency_after_its_last_word => Some("l2.vector/lead-latency-only"),
    modulo_rotates_the_predicate_over_its_copies => Some("l2.vector/pred-one-copy"),
    an_image_past_imem_is_refused_and_retiled_to_one_that_fits => Some("l2.vector/imem-unbounded"),
    a_row_unpacked_before_the_loop_is_pinned_or_unpacked_where_read => Some("l2.vector/pin-unforced"),
    a_shape_seen_before_is_not_searched_again => Some("l2.vector/search-again"),
    a_row_unpacked_before_the_loop_packs_in_it => Some("l2.vector/pinned-row-fixed"),
    a_row_not_pinned_reaches_a_reduction_through_its_product => Some("l2.vector/pin-unforced"),
    a_row_not_pinned_keeps_its_bytes_or_is_no_candidate => Some("l2.vector/pin-unforced"),
    a_value_stored_through_two_views_is_packed_once_for_both => Some("l2.vector/no-twin"),
    a_value_stored_in_two_l1_layouts_is_packed_into_each => Some("l2.vector/pack-first-dest"),
    a_staged_constant_is_set_again_past_a_skipped_block => Some("l2.vector/staged-across-blocks"),
    constants_past_the_scalar_file_are_held_or_staged => Some("l2.vector/hold-unforced"),
    a_view_off_a_word_is_refused => Some("l2.memory/lo-ignored"),
    a_store_of_a_value_not_the_views_size_is_refused => Some("l2.vector/view-size-unchecked"),
    a_gt4_sum_of_squares_folds_the_product => Some("l2.vector/no-fusion"),
    a_store_without_l1_room_packs_over_its_trips_load => Some("l2.vector/l1-384"),
    a_wide_band_past_the_registers_is_refused => Some("l2.vector/registers-unrefused"),
    modulo_emission_reads_every_value_it_wrote => Some("l2.vector/colour-one"),
    modulo_placement_takes_the_first_free_run => Some("l2.vector/place-keeps-early"),
    modulo_colouring_matches_the_arc_by_arc_check => Some("l2.vector/colour-one"),
    modulo_stops_once_ii_spans_an_iteration => Some("l2.vector/ii-unbounded"),
    modulo_parse_reads_an_ops_own_destination => Some("l2.vector/xreads-dropped"),
    modulo_placement_runs_each_op_back_to_back => Some("l2.vector/place-overlaps"),
    modulo_colouring_compares_arcs_on_the_circle => Some("l2.vector/colour-one"),
    a_vector_body_past_its_registers_is_refused => Some("l2.vector/registers-unrefused"),
    a_one_word_move_goes_through_a_contiguous_scratch => Some("l2.passes/stage-off"),
    a_staged_move_in_a_loop_keeps_its_symbolic_offsets => Some("l2.passes/stage-off"),
    each_staged_move_takes_its_own_scratch_after_the_written_ones => Some("l2.passes/stage-off"),
    a_move_on_several_nodes_stages_through_a_slab_each => Some("l2.passes/stage-off"),
    a_move_with_a_longer_run_on_one_side_is_kept => Some("l2.passes/stage-every-move"),
    a_move_whose_instances_want_different_scratches_is_kept_and_found => Some("l2.passes/stage-first-plan"),
    a_move_of_one_word_is_kept_and_still_found => Some("l2.passes/stage-single-word"),
    a_mover_that_takes_one_word_runs_keeps_every_move => Some("l2.passes/stage-one-word-ignored"),
    the_lowered_l1_holds_no_one_word_move => Some("l2.passes/stage-off"),
    the_staged_moves_run_in_order_in_one_mover_step => Some("l2.passes/stage-off"),
    a_staged_move_waits_for_the_last_iterations_read_of_its_scratch => Some("l2.sync/never-clash"),
    the_check_reads_each_sides_innermost_run => None,
    the_check_finds_moves_inside_blocks => None,
    two_reads_of_one_beat_go_to_opposite_banks => Some("l2.banks/rotates-none"),
    four_copies_need_no_placement => Some("l2.banks/drops-op"),
}

/// Each port fails its property under its killing mutant: an error, not a
/// panic.
#[cfg(feature = "mutants")]
#[test]
fn each_port_has_its_killing_mutant() {
    use ktpu_l2::mutant::{scoped, State, NAMES};
    use std::panic::{catch_unwind, AssertUnwindSafe};
    let mut survived = Vec::new();
    for (name, f, kill) in PORTS {
        let Some(m) = kill else { continue };
        assert!(NAMES.contains(m), "{name}: no mutant {m}");
        let state = State {
            active: Some(*m),
            ..State::default()
        };
        let got = catch_unwind(AssertUnwindSafe(|| scoped(state, *f)));
        match got {
            Ok(Err(e)) => eprintln!("{name} <= {m}: {}", e.lines().next().unwrap_or("")),
            Err(_) => survived.push(format!("{name} panics under {m}")),
            Ok(Ok(())) => survived.push(format!("{name} survives {m}")),
        }
    }
    assert!(survived.is_empty(), "{survived:#?}");
}
