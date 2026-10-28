//! Vector-core statements (DSL.VEC): load, unpack, math, reductions, pack,
//! store.

use super::{fail, here, Trace, View, R, V};
use g::ValueOp as Op;
use ktpu_graph as g;

/// A math operand: a number or a traced value.
#[derive(Clone, Copy, Debug)]
pub enum Arg {
    Num(f64),
    Val(V),
}

impl From<V> for Arg {
    fn from(v: V) -> Arg {
        Arg::Val(v)
    }
}

impl From<f64> for Arg {
    fn from(x: f64) -> Arg {
        Arg::Num(x)
    }
}

/// A float operand as the graph holds it (`inf`, `-inf`, `nan` by name).
pub fn number(x: f64) -> g::Number {
    if x.is_finite() {
        g::Number::F(x)
    } else if x.is_nan() {
        g::Number::Named("nan".into())
    } else if x > 0.0 {
        g::Number::Named("inf".into())
    } else {
        g::Number::Named("-inf".into())
    }
}

const UNARY: [Op; 7] = [
    Op::Exp2,
    Op::Inv,
    Op::Rsqrt,
    Op::Log2,
    Op::Neg,
    Op::Abs,
    Op::Mov,
];
const BINARY: [Op; 8] = [
    Op::Add,
    Op::Sub,
    Op::Mul,
    Op::Max,
    Op::Min,
    Op::Exp2d,
    Op::Xlane,
    Op::Xrot,
];
const TERNARY: [Op; 3] = [Op::Fma, Op::Fnma, Op::Sel];
const COMPARE: [Op; 3] = [Op::Cmplt, Op::Cmpgt, Op::Cmpeq];

fn prefix(op: Op) -> &'static str {
    match op {
        Op::Cmplt | Op::Cmpgt | Op::Cmpeq => "m",
        Op::Sel => "o",
        _ => "e",
    }
}

impl Trace {
    fn value_arg(&self, v: V) -> g::Operand {
        g::Operand::Value(self.value(v).name.clone())
    }

    /// `xlN = load VIEW` (DSL.VEC.001): a `tile=` buffer or `gt4` reads GT4
    /// bands; a one-row view read `gt4` is a `[mg.lanes, C]` row broadcast.
    #[track_caller]
    pub fn load(&mut self, view: &View, gt4: bool) -> R<V> {
        let at = here();
        self.need(Some(g::UnitKind::Vc), "load")?;
        let spec = &view.buf.spec;
        if !matches!(spec.dtype, g::Dtype::F16 | g::Dtype::F32) {
            return fail(
                "DSL.VEC.022",
                format!(
                    "a core loads f16 or f32, not {}, at {at}",
                    spec.dtype.name()
                ),
            );
        }
        let tiled = view.buf.tiled();
        let mode = (tiled || gt4)
            .then_some(g::Mode::Gt4)
            .filter(|_| !crate::mutants::on("dsl.ops/gt4_off"));
        let mut shape = view.shape();
        let row_bcast = gt4 && !tiled && (shape.len() == 1 || shape[0] == 1);
        if row_bcast {
            shape = vec![self.facts.band, *shape.last().expect("a dim")];
        }
        let args = vec![g::Operand::View(view.graph())];
        let ty = (spec.dtype, shape, g::ValSpace::L1, mode);
        let v = self.new_value(Op::Load, args, vec![], ty, "xl", None, at);
        self.set_tiled(v, tiled);
        self.values[v.0].row_bcast = row_bcast;
        Ok(v)
    }

    /// `vN = unpack X` (DSL.VEC.002, .003): to registers, once a value
    /// (a lane unpack each time), placed right after the value's load.
    #[track_caller]
    pub fn unpack(&mut self, x: V, lane: Option<i64>) -> R<V> {
        let at = here();
        self.need(Some(g::UnitKind::Vc), "unpack")?;
        let xv = self.value(x).clone();
        if xv.space != g::ValSpace::L1 {
            return fail("DSL.VEC.023", format!("unpack takes an @l1 value, at {at}"));
        }
        if let Some(l) = lane {
            if xv.shape.len() != 1 || !(0..self.facts.lanes).contains(&l) {
                return fail(
                    "DSL.VEC.003",
                    format!("unpack lane= takes a lane of a 1-D value, at {at}"),
                );
            }
        }
        if lane.is_none() {
            if let Some(u) = xv.unpacked {
                return Ok(u);
            }
        }
        let kw = lane
            .map(|l| vec![("lane".to_string(), g::Attr::Int(l))])
            .unwrap_or_default();
        let ty = (g::Dtype::F32, xv.shape.clone(), g::ValSpace::Reg, xv.mode);
        let args = vec![self.value_arg(x)];
        let out = self.new_value(Op::Unpack, args, kw, ty, "v", Some(x), at);
        if lane.is_none() {
            self.set_unpacked(x, out);
        } else {
            self.values[out.0].lane = true;
        }
        self.values[out.0].row_bcast = xv.row_bcast;
        Ok(out)
    }

    /// `x` as a register operand: an @l1 value unpacked here.
    #[track_caller]
    fn reg(&mut self, x: Arg) -> R<Arg> {
        let Arg::Val(v) = x else { return Ok(x) };
        match self.value(v).space {
            g::ValSpace::L1 => Ok(Arg::Val(self.unpack(v, None)?)),
            g::ValSpace::Reg => Ok(x),
            s => fail(
                "DSL.VEC.024",
                format!(
                    "{} is @{}: a cluster's, not a core's register value",
                    self.value(v).name,
                    s.name()
                ),
            ),
        }
    }

    /// One vector op over values and numbers (DSL.VEC.005, .011, .012).
    #[track_caller]
    pub fn math(&mut self, op: Op, args: &[Arg]) -> R<V> {
        let at = here();
        self.need(Some(g::UnitKind::Vc), op.name())?;
        let want = if UNARY.contains(&op) {
            1
        } else if BINARY.contains(&op) || COMPARE.contains(&op) {
            2
        } else if TERNARY.contains(&op) {
            3
        } else {
            return fail(
                "DSL.VEC.024",
                format!("no vector op {:?}, at {at}", op.name()),
            );
        };
        if args.len() != want {
            return fail(
                "DSL.VEC.024",
                format!(
                    "{} takes {want} operands, got {}, at {at}",
                    op.name(),
                    args.len()
                ),
            );
        }
        let mut regs = vec![];
        for a in args {
            regs.push(self.reg(*a)?);
        }
        let vals: Vec<V> = regs
            .iter()
            .filter_map(|r| match r {
                Arg::Val(v) => Some(*v),
                Arg::Num(_) => None,
            })
            .collect();
        if vals.is_empty() {
            return fail(
                "DSL.VEC.024",
                format!("{} of numbers alone: fold it in Python, at {at}", op.name()),
            );
        }
        let rows: Vec<&super::ValueData> = vals
            .iter()
            .map(|v| self.value(*v))
            .filter(|v| !v.stat())
            .collect();
        // The rows come from the data's rows; a row broadcast's are the
        // band's, so only with no other rows does a row statistic (one value
        // a row; a lane broadcast's are not rows) set them.
        let stat_rows: Vec<i64> = vals
            .iter()
            .map(|v| self.value(*v))
            .filter(|v| v.stat() && !v.lane)
            .map(|v| v.shape[0])
            .collect();
        let data_rows: Vec<i64> = rows
            .iter()
            .filter(|v| !v.row_bcast)
            .map(|v| v.shape[0])
            .collect();
        let row_bcast = data_rows.is_empty() && stat_rows.is_empty();
        let (shape, mode, bc) = if !rows.is_empty() {
            let shape = if rows.iter().all(|v| v.shape.len() == 2) {
                let n = if crate::mutants::on("dsl.ops/lane-lost") {
                    data_rows.iter().chain(stat_rows.iter()).max()
                } else {
                    data_rows.iter().max().or(stat_rows.iter().max())
                }
                .copied()
                .unwrap_or_else(|| rows.iter().map(|v| v.shape[0]).max().expect("a row"));
                vec![n, rows.iter().map(|v| v.shape[1]).max().expect("a row")]
            } else {
                let key = |s: &Vec<i64>| (s[0] * s[s.len() - 1], s.clone());
                rows.iter()
                    .map(|v| v.shape.clone())
                    .max_by(|a, b| key(a).cmp(&key(b)))
                    .expect("a row")
            };
            let mode = rows[0].mode;
            if rows.iter().any(|v| v.mode != mode) {
                return fail(
                    "DSL.VEC.029",
                    format!("{} mixes GT4 and plain rows, at {at}", op.name()),
                );
            }
            (shape, mode, true)
        } else {
            if COMPARE.contains(&op) || op == Op::Sel {
                return fail(
                    "DSL.VEC.029",
                    format!("{} runs on rows, not statistics, at {at}", op.name()),
                );
            }
            (self.value(vals[0]).shape.clone(), None, false)
        };
        let nodes = regs
            .iter()
            .map(|r| match r {
                Arg::Num(x) => g::Operand::Float(number(*x)),
                Arg::Val(v) if bc && self.value(*v).stat() => {
                    g::Operand::Bcast(self.value(*v).name.clone())
                }
                Arg::Val(v) => self.value_arg(*v),
            })
            .collect();
        let ty = (g::Dtype::F32, shape, g::ValSpace::Reg, mode);
        let out = self.new_value(op, nodes, vec![], ty, prefix(op), None, at);
        self.values[out.0].row_bcast = bc && row_bcast;
        Ok(out)
    }

    /// `sN = rmax X` / `rsum X` (DSL.VEC.013): a row statistic.
    #[track_caller]
    pub fn reduce(&mut self, op: Op, x: Arg) -> R<V> {
        let at = here();
        self.need(Some(g::UnitKind::Vc), op.name())?;
        if !matches!(op, Op::Rmax | Op::Rsum) {
            return fail(
                "DSL.VEC.030",
                format!("no reduction {:?}, at {at}", op.name()),
            );
        }
        let base = match self.reg(x)? {
            Arg::Val(v) if self.value(v).space == g::ValSpace::Reg && !self.value(v).stat() => v,
            _ => {
                return fail(
                    "DSL.VEC.030",
                    format!("{} reduces register rows, at {at}", op.name()),
                )
            }
        };
        let shape = vec![self.value(base).shape[0]];
        let args = vec![self.value_arg(base)];
        let ty = (g::Dtype::F32, shape, g::ValSpace::Reg, None);
        Ok(self.new_value(op, args, vec![], ty, "s", None, at))
    }

    /// `pN = pack X : DTYPE` (DSL.VEC.014, .016, .031). A statistic packs
    /// 16 a word; its `[R, 32]` form is not made (RUL.030).
    #[track_caller]
    pub fn pack(&mut self, x: V, dtype: g::Dtype, gt4: bool) -> R<V> {
        let at = here();
        self.need(Some(g::UnitKind::Vc), "pack")?;
        let xv = self.value(x).clone();
        let args = vec![self.value_arg(x)];
        if matches!(dtype, g::Dtype::Mx7a | g::Dtype::Mx7b) {
            if xv.mode != Some(g::Mode::Gt4) {
                return fail(
                    "DSL.VEC.032",
                    format!("an MXFP7 pack takes GT4 rows (4 x 32 blocks), at {at}"),
                );
            }
            let ty = (dtype, xv.shape.clone(), g::ValSpace::L1, None);
            return Ok(self.new_value(Op::Pack, args, vec![], ty, "q", None, at));
        }
        if xv.stat() {
            if gt4 && crate::mutants::on("kernels.vector/stat_rows_form") {
                let shape = vec![xv.shape[0], self.facts.kblock];
                let ty = (dtype, shape, g::ValSpace::L1, Some(g::Mode::Gt4));
                return Ok(self.new_value(Op::Pack, args, vec![], ty, "p", None, at));
            }
            if gt4 {
                return fail(
                    "RUL.030",
                    format!("a statistic packs 16 a word, not as [R, 32] rows, at {at}"),
                );
            }
            let ty = (dtype, vec![xv.shape[0]], g::ValSpace::L1, None);
            return Ok(self.new_value(Op::Pack, args, vec![], ty, "p", None, at));
        }
        let mode = (gt4 || xv.mode == Some(g::Mode::Gt4)).then_some(g::Mode::Gt4);
        let ty = (dtype, xv.shape.clone(), g::ValSpace::L1, mode);
        Ok(self.new_value(Op::Pack, args, vec![], ty, "p", None, at))
    }

    /// Materialise a compact statistic as ordinary GT4 register rows.
    #[track_caller]
    pub fn broadcast(&mut self, x: V, columns: i64) -> R<V> {
        let at = here();
        self.need(Some(g::UnitKind::Vc), "broadcast")?;
        let xv = self.value(x);
        if !xv.stat() || columns <= 0 || columns % self.facts.kblock != 0 {
            return fail(
                "DSL.VEC.034",
                "broadcast takes a statistic and whole column blocks",
            );
        }
        let args = vec![
            g::Operand::Bcast(xv.name.clone()),
            g::Operand::Float(number(1.0)),
        ];
        let ty = (
            g::Dtype::F32,
            vec![xv.shape[0], columns],
            g::ValSpace::Reg,
            Some(g::Mode::Gt4),
        );
        Ok(self.new_value(Op::Mul, args, vec![], ty, "v", None, at))
    }

    /// `store VIEW <- P` (DSL.VEC.017, .018, .034): an @l1 value of the
    /// buffer's tiling and element type as it is, else packed.
    #[track_caller]
    pub fn store(&mut self, view: &View, x: Arg) -> R<()> {
        let at = here();
        self.need(Some(g::UnitKind::Vc), "store")?;
        let spec = view.buf.spec.clone();
        let vshape = view.shape();
        let p = match x {
            Arg::Val(v)
                if self.value(v).space == g::ValSpace::L1
                    && self.value(v).tiled == view.buf.tiled()
                    && self.value(v).dtype == spec.dtype =>
            {
                v
            }
            _ => {
                let Arg::Val(r) = self.reg(x)? else {
                    return fail(
                        "DSL.VEC.033",
                        format!("store a value, not a number, at {at}"),
                    );
                };
                let rv = self.value(r).clone();
                if rv.stat() && vshape.len() == 1 && vshape[0] % rv.shape[0] == 0 {
                    let args = vec![self.value_arg(r)];
                    let ty = (spec.dtype, vshape.clone(), g::ValSpace::L1, None);
                    self.new_value(Op::Pack, args, vec![], ty, "p", None, at.clone())
                } else if rv.stat() {
                    self.pack(r, spec.dtype, false)?
                } else {
                    self.pack(r, spec.dtype, rv.mode == Some(g::Mode::Gt4))?
                }
            }
        };
        let pshape = &self.value(p).shape;
        let count = |s: &[i64]| s.iter().product::<i64>();
        if count(pshape) != count(&vshape) || pshape.last() != vshape.last() {
            return fail(
                "DSL.VEC.019",
                format!("a store of {pshape:?} values into a {vshape:?} view, at {at}"),
            );
        }
        let node = g::Node::Store {
            dst: view.graph(),
            src: self.value(p).name.clone(),
        };
        self.emit(node, at);
        Ok(())
    }

    /// `xlane X, K` (DSL.VEC.009): lane l takes lane `l xor k`.
    #[track_caller]
    pub fn xlane(&mut self, x: Arg, k: i64) -> R<V> {
        if !(0 < k && k < self.facts.lanes) {
            return fail(
                "DSL.VEC.027",
                format!("xlane takes a lane xor 1..15, got {k}"),
            );
        }
        self.math(Op::Xlane, &[x, Arg::Num(k as f64)])
    }

    /// `xrot X, K` (DSL.VEC.010): lane l takes lane `(l + k) mod lanes`.
    #[track_caller]
    pub fn xrot(&mut self, x: Arg, k: i64) -> R<V> {
        if !(0 < k && k < self.facts.lanes) {
            return fail(
                "DSL.VEC.027",
                format!("xrot takes a lane rotation 1..15, got {k}"),
            );
        }
        self.math(Op::Xrot, &[x, Arg::Num(k as f64)])
    }
}
