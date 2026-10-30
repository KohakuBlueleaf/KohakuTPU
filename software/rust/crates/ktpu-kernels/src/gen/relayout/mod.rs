//! Relayouts of f16 elements on the card, exact, as one kernel of the
//! cheapest parts the card has (`kernels/relayout.py`, `forms.relayout`).
//!
//! The walk pair is a set of axes `(count, source stride, destination
//! stride)` in order of destination stride, adjacent axes both walks step as
//! one merged; the innermost destination axis picks the form:
//!
//! - [`Words`]: both walks whole words: one mover walk.
//! - runs ([`Runs`]): the axis contiguous in the source too. Runs are cut
//!   into segments inside one vector-core row of destination words; a
//!   segment's source words are one gathered row (and the next word's row
//!   when it is rotated), rotated and masked in the vector core, and
//!   scattered back as whole memory lines (`card.dram.line_bytes`,
//!   L1.VER.017). A class (rotation, lane mask) is one loop; a width's
//!   segments share their gathers and scatters. Segments sharing a line are
//!   in different layers, each merging over the line as the last left it.
//! - [`Pull`]: the axis a broadcast of at most a word's lanes of copies: each
//!   destination word's lanes from at most [`PULL_SOURCES`] source words,
//!   staged by the mover; one vector-core program takes each lane from its
//!   (source, rotation), a code row saying which.
//! - [`Splat`]: the axis a longer broadcast: each value a row of copies,
//!   then runs from those rows.
//! - transpose ([`Exchange`]): the axis strided, another contiguous: runs
//!   into `[nb, A', B']` (padding 0), the transpose, runs out; a 2-element
//!   contiguous side a lane [`Shuffle`].
//!
//! Other walk pairs are [`Unplanned`] (`gen::lanes` takes them). The index
//! lists and masks are data; the kernel depends on the plan's shape only.
//!
//! The destination port is the destination's words from its line
//! ([`dst_words`]: a storage holds its word-rounded bytes, API.MEM.002).
//! Where they end inside a line and runs write the destination, the parts
//! write the lines of a scratch and one walk copies its words out.

mod emit;
mod forms;
mod repr;
mod tables;

pub use emit::{emit_steps, relayout_kernel, scratch_rows, step_ports};
pub use forms::{canon, dst_words, plan, positions, word_levels};
pub use repr::shape_repr;

use ktpu_machine::Machine;

/// Vector-core rows a trip takes.
pub const ROWS: i64 = 2;
/// Index entries a word holds: a list (and a class's rows) from a word.
pub const ENTRIES: i64 = 8;
/// Words one move takes (the stack's move bound, ISA.MV.007): a longer walk
/// is cut into moves, each writing part of the lines at its cuts.
pub const MOVE_WORDS: i64 = 16384;
/// Source words a destination word's lanes come from at most in a [`Pull`].
pub const PULL_SOURCES: usize = 2;
/// Contiguous source sides a lane shuffle takes: a program's B stream loads,
/// compress and tap masks and store fit a core's memory views.
pub const SHUFFLE: &[i64] = &[2];
/// The planner's relayouts all `gen::lanes`', as before this module: the
/// regold's reverse proof.
pub const LANES_MUTANT: &str = "relayout/lanes-only";
/// The destination port as whole lines from the destination's line: past
/// its words where they end inside a line (API.MEM.002).
pub const LINES_MUTANT: &str = "relayout/line-overrun";
/// Whether a [`Pull`]'s codes are predecoded into a selector tree
/// ([`plan`]'s `tree`; Python's `KTPU_PULL_TREE` default).
pub const PULL_TREE: bool = true;
/// Codes of a [`Pull`] lane: 0, or the destination's lane as it is.
pub const ZERO: i64 = -1;
pub const KEEP: i64 = -2;

/// The machine facts a relayout is planned with.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Hw {
    /// f16 values a word (`vc.lanes`).
    pub lanes: i64,
    /// Values a vector-core row (`vc.vlmax`).
    pub row: i64,
    /// Bytes a word (`die.word_bytes`).
    pub word: i64,
    /// Words a memory line (`card.dram.line_bytes / word`).
    pub line: i64,
    /// Vector cores trips are split over.
    pub cores: i64,
    /// Levels of a mover walk (`mover.levels`).
    pub levels: i64,
    /// Bytes every scratch buffer starts on: lines and GT4 entries.
    pub grain: i64,
    pub descriptors: i64,
}

impl Hw {
    /// `relayout.hw_of(machine, cores)`, its cores the machine's vector cores.
    pub fn of(m: &Machine) -> Hw {
        let word = m.die.word_bytes as i64;
        let line = (m.card.dram.line_bytes as i64 / word).max(1);
        let gt4 = m.mover.xform.gt4_rows as i64 * word;
        Hw {
            lanes: m.vc.lanes as i64,
            row: m.vc.vlmax as i64,
            word,
            line,
            cores: m.vc.count as i64,
            levels: m.mover.levels as i64,
            grain: lcm(line * word, gt4),
            descriptors: m.vc.descriptors as i64,
        }
    }

    pub fn row_words(&self) -> i64 {
        self.row / self.lanes
    }

    pub fn line_values(&self) -> i64 {
        self.line * self.lanes
    }
}

pub(crate) fn gcd(a: i64, b: i64) -> i64 {
    if b == 0 {
        a.abs()
    } else {
        gcd(b, a.rem_euclid(b))
    }
}

pub(crate) fn lcm(a: i64, b: i64) -> i64 {
    a / gcd(a, b) * b
}

/// `(count, byte stride)` mover levels, outermost first.
pub type Levels = Vec<(i64, i64)>;

/// Words `w0`.. (`words` of them) of `dst` := 0, by the mover.
#[derive(Clone, Debug, PartialEq)]
pub struct Fill {
    pub dst: String,
    pub w0: i64,
    pub words: i64,
}

/// Buffer `src` walked by `levels` from byte `at` into `dst` walked by
/// `dlevels` from byte `dat`, by the mover.
#[derive(Clone, Debug, PartialEq)]
pub struct Words {
    pub src: String,
    pub dst: String,
    pub levels: Levels,
    pub at: i64,
    pub dlevels: Levels,
    pub dat: i64,
}

/// A class: staging rows `row0`.. (`rows`, whole trips on every core)
/// rotated by `rot` (mask row `rmask`), selected under mask row `mask` (-1:
/// every lane) over the layer's base.
#[derive(Clone, Debug, PartialEq)]
pub struct Cls {
    pub rot: i64,
    pub rmask: i64,
    pub mask: i64,
    pub row0: i64,
    pub rows: i64,
}

/// Staging rows scattered together, each to lines no other row of the layer
/// writes (line list at `at_d`); lanes outside a row's mask take `base`:
/// "zero", or "dst" (the lines as they are).
#[derive(Clone, Debug, PartialEq)]
pub struct Layer {
    pub k: i64,
    pub base: String,
    pub row0: i64,
    pub rows: i64,
    pub at_d: i64,
    pub classes: Vec<Cls>,
}

/// Rows `words` destination words wide, staging rows `row0`.. (`rows`);
/// their source rows' index list at `at_s`, the next words' at `at_s2`.
#[derive(Clone, Debug, PartialEq)]
pub struct Group {
    pub words: i64,
    pub row0: i64,
    pub rows: i64,
    pub at_s: i64,
    pub at_s2: Option<i64>,
    pub layers: Vec<Layer>,
}

/// Segments from word table `src` (`src_words` words; index lists count rows
/// of `pitch` words; staged after `margin` words of 0 when set) into word
/// table `dst`.
#[derive(Clone, Debug, PartialEq)]
pub struct Runs {
    pub src: String,
    pub src_words: i64,
    pub pitch: i64,
    pub margin: i64,
    pub dst: String,
    pub groups: Vec<Group>,
}

/// Words `w0`.. (`count`) of `src`: each value a row of copies of `dst`; the
/// mask table's all-ones rows from `ones`.
#[derive(Clone, Debug, PartialEq)]
pub struct Splat {
    pub src: String,
    pub w0: i64,
    pub count: i64,
    pub dst: String,
    pub ones: i64,
}

/// How a [`Pull`] slot stages its source words: one mover walk (byte levels,
/// byte at), or a word gather (its index list).
#[derive(Clone, Debug, PartialEq)]
pub enum Slot {
    Walk(Levels, i64),
    List(i64),
}

/// Destination words `w0`.. (`words`, whole vector-core rows; `rows` staged,
/// whole trips): slot t's staged row holds, at each destination word, a
/// source word. Lane l of word w takes lane `(l + k) mod lanes` of slot t's
/// word where the code row (mask rows `codes`..) holds `t lanes + k`
/// (`pairs`: every such code), 0 where [`ZERO`], the destination's lane
/// (`base` "dst") where [`KEEP`]. Words `w0`.. (`out` of them) of the result
/// are the destination's. `tree`: the codes predecoded into selector bit
/// rows, the valid and zero rows (-1: none).
#[derive(Clone, Debug, PartialEq)]
pub struct Pull {
    pub src: String,
    pub dst: String,
    pub w0: i64,
    pub words: i64,
    pub rows: i64,
    pub out: i64,
    pub slots: Vec<Slot>,
    pub codes: i64,
    pub pairs: Vec<i64>,
    pub base: String,
    pub zeros: bool,
    pub tree: Option<(Vec<i64>, i64, i64)>,
}

/// The transpose of `src` `[nb, A, B]` into `dst` `[nb, B, A]` (its lane
/// masks the mask table's rows from `masks`).
#[derive(Clone, Debug, PartialEq)]
pub struct Exchange {
    pub src: String,
    pub dst: String,
    pub nb: i64,
    pub a: i64,
    pub b: i64,
    pub masks: i64,
}

/// One program of a [`Shuffle`]: its first compress mask row, the compress
/// stages `(rotation, mask row)`, the tap mask rows.
pub type Prog = (i64, Vec<(i64, i64)>, Vec<i64>);

/// `dst[b, n, i]` := `src[at + n sb + i B + b]` (b < `B`, n < `nb`, i <
/// `A`; rows `Ap` apart): the B word streams of the source by mover walks,
/// then one vector-core program per b compressing each stream's lanes.
#[derive(Clone, Debug, PartialEq)]
pub struct Shuffle {
    pub src: String,
    pub at: i64,
    pub nb: i64,
    pub sb: i64,
    pub a: i64,
    pub b: i64,
    pub ap: i64,
    pub dst: String,
    pub progs: Vec<Prog>,
}

/// One part of a plan, in the order it runs.
#[derive(Clone, Debug, PartialEq)]
pub enum Part {
    Fill(Fill),
    Words(Words),
    Runs(Runs),
    Splat(Splat),
    Pull(Pull),
    Exchange(Exchange),
    Shuffle(Shuffle),
}

/// A relayout: its parts in order, its scratch buffers `(name, rows, values
/// a row)`, its index lists (fp32) and mask rows (`row` f16 values each),
/// the source and destination port words.
#[derive(Clone, Debug, PartialEq)]
pub struct Plan {
    pub parts: Vec<Part>,
    pub scratch: Vec<(String, i64, i64)>,
    pub index: Vec<f32>,
    pub masks: Vec<Vec<f32>>,
    pub src_words: i64,
    pub dst_words: i64,
}

impl Plan {
    /// The mask table as the f16 bits the kernel's `mask` port holds: every
    /// entry an integer of magnitude below 2048 (0 / 1 masks, pull codes).
    pub fn mask_bits(&self) -> Vec<u16> {
        self.masks
            .iter()
            .flatten()
            .map(|&v| f16_int(v as i64))
            .collect()
    }
}

/// The f16 bits of the integer `v` (`|v| < 2048`, exact).
fn f16_int(v: i64) -> u16 {
    if v == 0 {
        return 0;
    }
    let (sign, a) = (u16::from(v < 0) << 15, v.unsigned_abs());
    let e = 63 - a.leading_zeros() as u64;
    let mantissa = ((a << (10 - e)) & 0x3ff) as u16;
    sign | (((e + 15) as u16) << 10) | mantissa
}

#[cfg(test)]
mod tests {
    #[test]
    fn integers_have_their_f16_bits() {
        let want = [
            (0, 0x0000),
            (1, 0x3c00),
            (2, 0x4000),
            (3, 0x4200),
            (31, 0x4fc0),
            (2047, 0x67ff),
            (-1, 0xbc00),
            (-2, 0xc000),
        ];
        for (v, bits) in want {
            assert_eq!(super::f16_int(v), bits, "{v}");
        }
    }
}

/// A walk pair no form here takes (`gen::lanes` takes it).
#[derive(Clone, Debug, PartialEq)]
pub struct Unplanned(pub String);
