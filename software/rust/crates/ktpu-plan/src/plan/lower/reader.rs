//! The stored value read as an expression over reads (`lower._Reader`):
//! loads, casts, reductions, ALU ops, index-only subexpressions folded
//! into tables.

use super::*;

fn alu_name(op: &str) -> Option<&'static str> {
    Some(match op {
        "ADD" => "add",
        "SUB" => "sub",
        "MUL" => "mul",
        "FDIV" => "div",
        "MAX" => "max",
        "NEG" => "neg",
        "RECIPROCAL" => "recip",
        "EXP2" => "exp2",
        "LOG2" => "log2",
        "SQRT" => "sqrt",
        "SIN" => "sin",
        "TRUNC" => "trunc",
        "POW" => "pow",
        "CMPLT" => "cmplt",
        "CMPNE" => "cmpne",
        "CMPEQ" => "cmpeq",
        "WHERE" => "where",
        "MULACC" => "fma",
        _ => return None,
    })
}

fn bool_name(op: &str) -> Option<&'static str> {
    Some(match op {
        "AND" => "and",
        "OR" => "or",
        "XOR" => "xor",
        _ => return None,
    })
}

/// `X.op`: a non-node argument becomes a constant.
fn op_of(name: &str, args: Vec<E>) -> E {
    expr::op(name, args)
}

/// An `ALU` / `BOOL` map's name (each suffixed `_` under
/// `tg.lower/alu_renamed`).
fn renamed(name: &str) -> String {
    if mutant::on("tg.lower/alu_renamed") {
        format!("{name}_")
    } else {
        name.to_string()
    }
}

pub struct Reader {
    pub(super) out_ranges: Vec<Id>,
    pub accesses: Vec<Access>,
    pub reduce_ranges: Vec<Id>,
    pub reductions: Vec<(E, Vec<Id>)>,
    pub(super) memo: HashMap<Id, E>,
    pub(super) imemo: HashMap<Id, bool>,
}

#[cfg(test)]
mod deletion;

impl Reader {
    pub(super) fn read(&mut self, g: &mut Graph, u: Id) -> Result<E, Refusal> {
        if let Some(e) = self.memo.get(&u) {
            return Ok(e.clone());
        }
        let out = self.read_new(g, u)?;
        self.memo.insert(u, out.clone());
        Ok(out)
    }

    fn read_new(&mut self, g: &mut Graph, u: Id) -> Result<E, Refusal> {
        if fold::index_only(g, u, &mut self.imemo) {
            if g.op(u) == "CONST" {
                return match g.arg(u) {
                    Arg::Bool(b) => Ok(expr::constant(if *b { 1.0 } else { 0.0 })),
                    Arg::Int(v) => Ok(expr::constant(*v as f64)),
                    Arg::Float(f) => Ok(expr::constant(f64::from_bits(*f))),
                    a => refuse("TG.PLAN.014", format!("a constant {a:?}")),
                };
            }
            return self.table(g, u, false);
        }
        let op = g.op(u).to_string();
        let src = g.src(u).to_vec();
        match op.as_str() {
            "INDEX" => return self.load(g, u),
            "CAST" => {
                let to = g.dtype(u).to_string();
                let x = self.read(g, src[0])?;
                if to == "bool" {
                    return Ok(op_of("cmpne", vec![x, expr::constant(0.0)]));
                }
                if !crate::uop::dtype::is_float(&to) {
                    return refuse("TG.PLAN.019", format!("a cast to dtypes.{to}"));
                }
                if mutant::on("tg.lower/cast_scaled") {
                    return Ok(op_of("mul", vec![x, expr::constant(1.0)]));
                }
                return Ok(x);
            }
            "BITCAST" => {
                return refuse(
                    "TG.PLAN.010",
                    "a BITCAST reinterprets bits; lanes hold E8M15 values, not bits",
                )
            }
            "REDUCE" => {
                let rop = match g.arg(u) {
                    Arg::Reduce { op, .. } => op.clone(),
                    _ => String::new(),
                };
                let name = match rop.as_str() {
                    "ADD" if mutant::on("tg.lower/reduce_all_max") => "max",
                    "ADD" => "sum",
                    "MAX" => "max",
                    "MUL" => "prod",
                    _ => return refuse("TG.PLAN.017", format!("a Ops.{rop} reduction")),
                };
                let rs = src[1..].to_vec();
                if rs.iter().any(|r| g.op(*r) != "RANGE") {
                    return refuse(
                        "TG.PLAN.017",
                        "a reduction over something other than loop ranges",
                    );
                }
                for r in &rs {
                    if !self.reduce_ranges.contains(r) {
                        self.reduce_ranges.push(*r);
                    }
                }
                let arg = self.read(g, src[0])?;
                let out = expr::reduce(name, arg);
                self.reductions.push((out.clone(), rs));
                return Ok(out);
            }
            "RECIPROCAL" if g.op(src[0]) == "SQRT" => {
                let x = self.read(g, g.src(src[0])[0])?;
                return Ok(op_of("rsqrt", vec![x]));
            }
            _ => {}
        }
        if let Some(name) = alu_name(&op) {
            let args: Vec<E> = src
                .iter()
                .map(|s| self.read(g, *s))
                .collect::<Result<_, _>>()?;
            if op == "WHERE" {
                if let Expr::Const(v) = args[0].as_ref() {
                    return Ok(if *v != 0.0 {
                        args[1].clone()
                    } else {
                        args[2].clone()
                    });
                }
            }
            return Ok(op_of(&renamed(name), args));
        }
        if let Some(name) = bool_name(&op) {
            if g.dtype(u) == "bool" {
                let args: Vec<E> = src
                    .iter()
                    .map(|s| self.read(g, *s))
                    .collect::<Result<_, _>>()?;
                return Ok(op_of(&renamed(name), args));
            }
        }
        if op == "THREEFRY" {
            return refuse(
                "TG.PLAN.010",
                "THREEFRY on data: a bit-mixing hash has no lane operation (make random tensors with numpy, or the mover's `gen`)",
            );
        }
        refuse("TG.PLAN.020", format!("no lowering for {op}"))
    }

    fn load(&mut self, g: &mut Graph, u: Id) -> Result<E, Refusal> {
        let Some(param) = param_of(g, u) else {
            return refuse("TG.PLAN.014", "a read of something other than a buffer");
        };
        let mut idx = g.src(u).get(1).copied();
        let mut gate = None;
        let (coef, off) =
            match idx {
                None => (Vec::new(), 0),
                Some(i) => {
                    if let Some((gt, inner)) = gated(g, i) {
                        gate = Some(gt);
                        idx = Some(inner);
                        if !fold::index_only(g, gt, &mut self.imemo) {
                            return refuse("TG.PLAN.014", "a padded read whose bound reads data");
                        }
                    }
                    match affine(g, idx.unwrap()) {
                        Some(f) => f,
                        None => return refuse(
                            "TG.PLAN.014",
                            "a read whose index is not affine in the loop (a reshape that divides)",
                        ),
                    }
                }
            };
        let (slot, dtype) = param_slot(g, param);
        let acc = Access {
            slot: Slot::Param(slot),
            dtype,
            coef,
            off,
            table: None,
            gate: false,
        };
        let i = match self.accesses.iter().position(|a| *a == acc) {
            Some(i) => i,
            None => {
                self.accesses.push(acc);
                self.accesses.len() - 1
            }
        };
        let load = expr::load(i);
        let Some(gt) = gate else { return Ok(load) };
        let bound = self.table(g, gt, true)?;
        if let Expr::Const(v) = bound.as_ref() {
            return Ok(if *v != 0.0 { load } else { expr::constant(0.0) });
        }
        Ok(op_of("where", vec![bound, load, expr::constant(0.0)]))
    }

    fn rank(&self, r: Id) -> usize {
        self.out_ranges
            .iter()
            .position(|x| *x == r)
            .unwrap_or(self.out_ranges.len())
    }

    /// `_table`: an index-only subexpression folded over the ranges it
    /// reads, a compact row-major f16 table (one value: a constant).
    fn table(&mut self, g: &mut Graph, u: Id, gate: bool) -> Result<E, Refusal> {
        if mutant::on("tg.lower/no_tables") {
            return Ok(expr::constant(0.0));
        }
        let mut rs: Vec<Id> = g
            .toposort(u)
            .into_iter()
            .filter(|r| g.op(*r) == "RANGE")
            .collect();
        rs.sort_by_key(|r| (self.rank(*r), range_id(g, *r).0));
        rs.dedup();
        let mut memo = HashMap::new();
        let arr =
            fold::evaluate(g, u, &rs, &mut memo).map_err(|e| Refusal::new("TG.PLAN.013", e.0))?;
        let dims: Vec<usize> = rs.iter().map(|r| extent(g, *r) as usize).collect();
        let arr = arr.cast(Ty::F64).broadcast(&dims);
        if !arr.is_empty() && (0..arr.len()).all(|i| arr.floats[i] == arr.floats[0]) {
            return Ok(expr::constant(arr.floats[0]));
        }
        let mut coef = Vec::new();
        let mut step = 1;
        for r in rs.iter().rev() {
            coef.push((*r, step));
            step *= extent(g, *r);
        }
        let bits = arr.floats.iter().map(|v| fold::f16_bits(*v)).collect();
        self.accesses.push(Access {
            slot: Slot::Table,
            dtype: "half".into(),
            coef,
            off: 0,
            table: Some(Table { shape: dims, bits }),
            gate,
        });
        Ok(expr::load(self.accesses.len() - 1))
    }
}
