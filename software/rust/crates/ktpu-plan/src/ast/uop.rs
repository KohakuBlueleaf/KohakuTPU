//! The part of tinygrad's UOp graph the lowering reads and rewrites
//! (tinygrad 0.14 `uop/ops.py`, `dtype.py`): hash-consed nodes (equal op,
//! dtype, sources and argument are one node), `toposort`, `substitute`
//! (`graph_rewrite` bottom-up to a fixed point), the dtype rules a rebuilt
//! node takes, and the constructors the lowering calls (`range`, `const`,
//! the operators, `where`, `valid`, `cast`).

use std::collections::HashMap;

use serde_json::Value;

use crate::input::Uops;

pub type Id = usize;

/// A node's argument, as the lowering reads it.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Arg {
    None,
    Int(i64),
    /// a float constant, by its bits
    Float(u64),
    Bool(bool),
    Invalid,
    /// `RANGE`: `(axis id, AxisType)` and any further fields
    Range {
        id: i64,
        axis: String,
        rest: String,
    },
    /// `REDUCE`: `(op, n)`
    Reduce {
        op: String,
        n: i64,
    },
    /// `CAST` / `BITCAST`: the dtype
    DType(String),
    /// `PARAM`: `ParamArg` (slot, dtype)
    Param {
        slot: i64,
        dtype: String,
        rest: String,
    },
    /// anything else, by its JSON text
    Other(String),
}

impl Arg {
    /// The argument of `emit.plain` JSON for op `op`.
    pub fn read(op: &str, v: &Value) -> Arg {
        match (op, v) {
            (_, Value::Null) => Arg::None,
            ("CONST", Value::Bool(b)) => Arg::Bool(*b),
            ("CONST", Value::Number(n)) if n.is_i64() => Arg::Int(n.as_i64().unwrap()),
            ("CONST", Value::Number(n)) => Arg::Float(n.as_f64().unwrap().to_bits()),
            ("CONST", Value::String(s)) => match s.as_str() {
                "inf" => Arg::Float(f64::INFINITY.to_bits()),
                "-inf" => Arg::Float(f64::NEG_INFINITY.to_bits()),
                "nan" => Arg::Float(f64::NAN.to_bits()),
                _ => Arg::Other(v.to_string()),
            },
            ("CONST", Value::Object(o)) if o.get("repr") == Some(&Value::from("Invalid")) => {
                Arg::Invalid
            }
            ("RANGE", Value::Array(a)) if a.len() >= 2 => Arg::Range {
                id: a[0].as_i64().unwrap_or(0),
                axis: enum_name(&a[1]),
                rest: Value::Array(a[2..].to_vec()).to_string(),
            },
            ("REDUCE", Value::Array(a)) if a.len() == 2 => Arg::Reduce {
                op: enum_name(&a[0]),
                n: a[1].as_i64().unwrap_or(0),
            },
            ("CAST" | "BITCAST", Value::Object(o)) if o.contains_key("dtype") => {
                Arg::DType(o["dtype"].as_str().unwrap_or("").to_string())
            }
            ("PARAM", Value::Object(o)) if o.get("type") == Some(&Value::from("ParamArg")) => {
                let f = &o["fields"];
                Arg::Param {
                    slot: f["slot"].as_i64().unwrap_or(-1),
                    dtype: f["dtype"]["dtype"].as_str().unwrap_or("").to_string(),
                    rest: v.to_string(),
                }
            }
            _ => Arg::Other(v.to_string()),
        }
    }

    pub fn float(&self) -> Option<f64> {
        match self {
            Arg::Float(b) => Some(f64::from_bits(*b)),
            _ => None,
        }
    }
}

/// `{"enum": "Class.NAME"}` -> `NAME`.
fn enum_name(v: &Value) -> String {
    v["enum"]
        .as_str()
        .map(|s| s.rsplit('.').next().unwrap_or(s).to_string())
        .unwrap_or_else(|| v.to_string())
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct UOp {
    pub op: String,
    pub dtype: String,
    pub src: Vec<Id>,
    pub arg: Arg,
}

/// Hash-consed nodes: `make` of equal fields returns the same id.
#[derive(Clone, Debug, Default)]
pub struct Graph {
    nodes: Vec<UOp>,
    intern: HashMap<UOp, Id>,
}

impl Graph {
    /// The graph of a `ktpu.uops` and its root.
    pub fn read(u: &Uops) -> (Graph, Id) {
        let mut g = Graph::default();
        let mut at = Vec::with_capacity(u.nodes.len());
        for n in &u.nodes {
            let src = n.src.iter().map(|i| at[*i]).collect();
            at.push(g.make(&n.op, &n.dtype, src, Arg::read(&n.op, &n.arg)));
        }
        let root = at[u.root];
        (g, root)
    }

    pub fn make(&mut self, op: &str, dtype: &str, src: Vec<Id>, arg: Arg) -> Id {
        let u = UOp {
            op: op.to_string(),
            dtype: dtype.to_string(),
            src,
            arg,
        };
        if let Some(id) = self.intern.get(&u) {
            return *id;
        }
        let id = self.nodes.len();
        self.nodes.push(u.clone());
        self.intern.insert(u, id);
        id
    }

    pub fn get(&self, id: Id) -> &UOp {
        &self.nodes[id]
    }

    pub fn op(&self, id: Id) -> &str {
        &self.nodes[id].op
    }

    pub fn src(&self, id: Id) -> &[Id] {
        &self.nodes[id].src
    }

    pub fn dtype(&self, id: Id) -> &str {
        &self.nodes[id].dtype
    }

    /// tinygrad's `toposort`: sources before users, the first source's
    /// subtree first.
    pub fn toposort(&self, root: Id) -> Vec<Id> {
        let mut done = vec![false; self.nodes.len()];
        let mut out = Vec::new();
        let mut stack = vec![(root, false)];
        while let Some((n, visited)) = stack.pop() {
            if done[n] {
                continue;
            }
            if !visited {
                stack.push((n, true));
                for s in self.nodes[n].src.iter().rev() {
                    stack.push((*s, false));
                }
            } else {
                done[n] = true;
                out.push(n);
            }
        }
        out
    }

    /// tinygrad's `substitute` (`graph_rewrite`, bottom-up): each node
    /// replaced by its image under `map` to a fixed point, then rebuilt over
    /// its rewritten sources, the rebuilt node rewritten again.
    pub fn substitute(&mut self, root: Id, map: &HashMap<Id, Id>) -> Id {
        let map: HashMap<Id, Id> = map
            .iter()
            .filter(|(k, v)| k != v)
            .map(|(k, v)| (*k, *v))
            .collect();
        if map.is_empty() {
            return root;
        }
        let mut memo: HashMap<Id, Id> = HashMap::new();
        self.rewrite(root, &map, &mut memo)
    }

    fn rewrite(&mut self, n: Id, map: &HashMap<Id, Id>, memo: &mut HashMap<Id, Id>) -> Id {
        if let Some(r) = memo.get(&n) {
            return *r;
        }
        let mut m = n;
        let mut seen = vec![m];
        while let Some(next) = map.get(&m) {
            assert!(!seen.contains(next), "infinite loop in fixed_point_rewrite");
            seen.push(*next);
            m = *next;
        }
        let src = self.nodes[m].src.clone();
        let new_src: Vec<Id> = src.iter().map(|s| self.rewrite(*s, map, memo)).collect();
        let out = if new_src == src {
            m
        } else {
            let u = self.nodes[m].clone();
            let dt = self.rebuild_dtype(&u, &new_src);
            let built = self.make(&u.op, &dt, new_src, u.arg);
            self.rewrite(built, map, memo)
        };
        memo.insert(n, out);
        out
    }

    /// `_rebuild_dtype`: the old dtype while every source keeps its own,
    /// else the dtype the op derives (`dtype_from_uop`).
    fn rebuild_dtype(&self, u: &UOp, new_src: &[Id]) -> String {
        let same = u
            .src
            .iter()
            .zip(new_src)
            .all(|(a, b)| self.nodes[*a].dtype == self.nodes[*b].dtype);
        if same {
            return u.dtype.clone();
        }
        self.derive_dtype(&u.op, new_src, &u.arg)
            .unwrap_or_else(|| u.dtype.clone())
    }

    /// `dtype_from_uop` for the ops a lowered kernel holds.
    pub fn derive_dtype(&self, op: &str, src: &[Id], arg: &Arg) -> Option<String> {
        let d = |i: usize| self.nodes[src[i]].dtype.clone();
        Some(match op {
            "STORE" | "SINK" | "END" => "void".into(),
            "INDEX" | "LOAD" | "REDUCE" | "RANGE" => d(0),
            "CMPLT" | "CMPNE" | "CMPEQ" => "bool".into(),
            "SIN" | "LOG2" | "EXP2" | "SQRT" | "RECIPROCAL" => {
                if self.is_invalid(src[0]) {
                    "bool".into()
                } else {
                    dtype::least_upper_float(&d(0))
                }
            }
            "WHERE" => dtype::promo(
                &src[1..]
                    .iter()
                    .map(|s| self.nodes[*s].dtype.as_str())
                    .collect::<Vec<_>>(),
            ),
            "PARAM" => match arg {
                Arg::Param { dtype, .. } => dtype.clone(),
                _ => return None,
            },
            "CAST" | "BITCAST" => match arg {
                Arg::DType(t) => t.clone(),
                _ => return None,
            },
            "CONST" => match arg {
                Arg::Invalid | Arg::Bool(_) => "bool".into(),
                Arg::Int(_) => "weakint".into(),
                Arg::Float(_) => "weakfloat".into(),
                _ => return None,
            },
            "NEG" | "TRUNC" | "NOT" => d(0),
            "ADD" | "SUB" | "MUL" | "FDIV" | "MAX" | "POW" | "FLOORDIV" | "FLOORMOD" | "CDIV"
            | "CMOD" | "AND" | "OR" | "XOR" | "MULACC" => dtype::promo(
                &src.iter()
                    .map(|s| self.nodes[*s].dtype.as_str())
                    .collect::<Vec<_>>(),
            ),
            _ => return None,
        })
    }

    pub fn is_invalid(&self, id: Id) -> bool {
        let u = &self.nodes[id];
        u.op == "CONST" && u.arg == Arg::Invalid
    }

    pub fn arg(&self, id: Id) -> &Arg {
        &self.nodes[id].arg
    }

    /// `UOp.alu`: op over `src`, the dtype derived (`dtype_from_uop`).
    pub fn alu(&mut self, op: &str, src: Vec<Id>, arg: Arg) -> Id {
        let dt = self
            .derive_dtype(op, &src, &arg)
            .unwrap_or_else(|| "void".into());
        self.make(op, &dt, src, arg)
    }

    /// `UOp.const(b, dtype)`: `dtype.const(b)` (an int dtype holds an int,
    /// a float one a float, bool a bool); Invalid is a bool.
    pub fn konst(&mut self, v: &Arg, dtype: &str) -> Id {
        let (dt, arg) = match v {
            Arg::Invalid => ("bool".to_string(), Arg::Invalid),
            _ if dtype == "bool" => ("bool".into(), Arg::Bool(num(v) != 0.0)),
            _ if dtype::is_float(dtype) => (dtype.into(), Arg::Float(num(v).to_bits())),
            Arg::Bool(b) => (dtype.into(), Arg::Int(*b as i64)),
            Arg::Float(f) => (dtype.into(), Arg::Int(f64::from_bits(*f) as i64)),
            _ => (dtype.into(), v.clone()),
        };
        self.make("CONST", &dt, Vec::new(), arg)
    }

    /// `UOp.const(b)` of a Python value (`dtypes.from_py`).
    pub fn konst_py(&mut self, v: &Arg) -> Id {
        let dt = match v {
            Arg::Bool(_) | Arg::Invalid => "bool",
            Arg::Float(_) => "weakfloat",
            _ => "weakint",
        };
        self.konst(v, dt)
    }

    /// `x.const_like(b)`: a constant of `x`'s dtype.
    pub fn const_like(&mut self, x: Id, v: &Arg) -> Id {
        let dt = self.nodes[x].dtype.clone();
        self.konst(v, &dt)
    }

    /// `UOp.range(end, axis_id, axis_type)`.
    pub fn range(&mut self, end: i64, id: i64, axis: &str) -> Id {
        let e = self.konst(&Arg::Int(end), "weakint");
        self.make(
            "RANGE",
            "weakint",
            vec![e],
            Arg::Range {
                id,
                axis: axis.into(),
                rest: "[]".into(),
            },
        )
    }

    /// `x.cast(dtype)`: `x` itself when already `dtype`.
    pub fn cast(&mut self, x: Id, dtype: &str) -> Id {
        if self.nodes[x].dtype == dtype {
            return x;
        }
        self.alu("CAST", vec![x], Arg::DType(dtype.into()))
    }

    fn promote_to(&mut self, t: Id, out: &str) -> Id {
        if self.is_invalid(t) {
            return t;
        }
        let u = self.nodes[t].clone();
        if (u.dtype == "weakint" || u.dtype == "weakfloat") && u.op == "CONST" {
            let w = dtype::weak(out);
            return self.konst(&u.arg, &w);
        }
        self.cast(t, out)
    }

    /// `_broadcasted`: both sides promoted to their least upper dtype, a
    /// weak constant kept weak.
    pub fn broadcasted(&mut self, x: Id, y: Id) -> (Id, Id) {
        let out = dtype::least_upper(&[&self.nodes[x].dtype.clone(), &self.nodes[y].dtype.clone()]);
        (self.promote_to(x, &out), self.promote_to(y, &out))
    }

    /// `x <op> y` (`_binop`).
    pub fn binop(&mut self, op: &str, x: Id, y: Id) -> Id {
        let (a, b) = self.broadcasted(x, y);
        self.alu(op, vec![a, b], Arg::None)
    }

    /// `x <op> v` of a Python number.
    pub fn binop_py(&mut self, op: &str, x: Id, v: &Arg) -> Id {
        let y = self.konst_py(v);
        self.binop(op, x, y)
    }

    /// `cond.where(x, y)`.
    pub fn where_(&mut self, cond: Id, x: Id, y: Id) -> Id {
        let (a, b) = self.broadcasted(x, y);
        self.alu("WHERE", vec![cond, a, b], Arg::None)
    }

    /// `x.valid(cond)`: `cond.where(x, Invalid)`.
    pub fn valid(&mut self, x: Id, cond: Id) -> Id {
        let inv = self.konst(&Arg::Invalid, "bool");
        self.where_(cond, x, inv)
    }

    /// `x.replace(arg=...)`.
    pub fn with_arg(&mut self, x: Id, arg: Arg) -> Id {
        let u = self.nodes[x].clone();
        if u.arg == arg {
            return x;
        }
        self.make(&u.op, &u.dtype, u.src, arg)
    }

    /// `x.replace(src=...)`: the node with these sources (its dtype kept).
    pub fn with_src(&mut self, x: Id, src: Vec<Id>) -> Id {
        let u = self.nodes[x].clone();
        if u.src == src {
            return x;
        }
        self.make(&u.op, &u.dtype, src, u.arg)
    }

    /// `UOp.ranges`: the ranges `u` is in, a REDUCE's or END's own ended.
    pub fn ranges(&self, u: Id) -> Vec<Id> {
        let mut memo: HashMap<Id, Vec<Id>> = HashMap::new();
        for n in self.toposort(u) {
            let node = &self.nodes[n];
            let mut ret: Vec<Id> = Vec::new();
            for s in &node.src {
                let mut rs = memo[s].clone();
                if self.nodes[*s].op == "RANGE" && !rs.contains(s) {
                    rs.insert(0, *s);
                }
                for r in rs {
                    if !ret.contains(&r) {
                        ret.push(r);
                    }
                }
            }
            if matches!(node.op.as_str(), "REDUCE" | "END" | "STAGE") {
                for er in &node.src[1..] {
                    if self.nodes[*er].op == "RANGE" {
                        ret.retain(|r| r != er);
                    } else {
                        let inner = memo[er].clone();
                        ret.retain(|r| !inner.contains(r));
                    }
                }
            }
            memo.insert(n, ret);
        }
        let mut out = memo.remove(&u).unwrap_or_default();
        if self.nodes[u].op == "RANGE" && !out.contains(&u) {
            out.insert(0, u);
        }
        out
    }
}

fn num(v: &Arg) -> f64 {
    match v {
        Arg::Int(i) => *i as f64,
        Arg::Bool(b) => *b as i64 as f64,
        Arg::Float(f) => f64::from_bits(*f),
        _ => f64::NAN,
    }
}

/// The dtype lattice (`dtype.py` `promo_lattice`, `least_upper_dtype`).
pub mod dtype {
    /// The parents of `d` in the promotion lattice.
    fn up(d: &str) -> &'static [&'static str] {
        match d {
            "bool" => &["weakint"],
            "weakint" => &["char", "uchar"],
            "char" => &["short"],
            "short" => &["int"],
            "int" => &["long"],
            "long" => &["weakfloat"],
            "uchar" => &["short", "ushort"],
            "ushort" => &["int", "uint"],
            "uint" => &["long", "ulong"],
            "ulong" => &["weakfloat"],
            "weakfloat" => &["fp8e4m3", "fp8e5m2", "fp8e4m3fnuz", "fp8e5m2fnuz"],
            "fp8e4m3" | "fp8e5m2" | "fp8e4m3fnuz" | "fp8e5m2fnuz" => &["half", "bfloat16"],
            "half" | "bfloat16" => &["float"],
            "float" => &["double"],
            _ => &[],
        }
    }

    /// The lattice order tinygrad's `min` takes (`DType` by priority,
    /// itemsize, name): the rank of each name.
    fn rank(d: &str) -> (i64, i64, &str) {
        let (p, size) = match d {
            "bool" => (0, 1),
            "weakint" => (0, 800),
            "char" => (1, 8),
            "uchar" => (2, 8),
            "short" => (3, 16),
            "ushort" => (4, 16),
            "int" => (5, 32),
            "uint" => (6, 32),
            "long" => (7, 64),
            "ulong" => (8, 64),
            "weakfloat" => (9, 800),
            "fp8e4m3" | "fp8e4m3fnuz" => (10, 8),
            "fp8e5m2" | "fp8e5m2fnuz" => (11, 8),
            "half" => (12, 16),
            "bfloat16" => (13, 16),
            "float" => (14, 32),
            "double" => (15, 64),
            _ => (99, 0),
        };
        (p, size, d)
    }

    fn parents(d: &str) -> Vec<String> {
        let mut out = vec![d.to_string()];
        if d == "double" {
            return out;
        }
        for p in up(d) {
            for x in parents(p) {
                if !out.contains(&x) {
                    out.push(x);
                }
            }
        }
        out
    }

    /// `least_upper_dtype`.
    pub fn least_upper(ds: &[&str]) -> String {
        let mut common = parents(ds[0]);
        for d in &ds[1..] {
            let p = parents(d);
            common.retain(|x| p.contains(x));
        }
        common
            .into_iter()
            .min_by(|a, b| rank(a).cmp(&rank(b)))
            .unwrap_or_else(|| ds[0].to_string())
    }

    /// `promo_dtype` of sources: their least upper bound.
    pub fn promo(ds: &[&str]) -> String {
        least_upper(ds)
    }

    /// `least_upper_float`.
    pub fn least_upper_float(d: &str) -> String {
        if d == "weakint" {
            return "weakfloat".into();
        }
        if is_float(d) {
            return d.to_string();
        }
        least_upper(&[d, "float"])
    }

    /// `weak_dtype`.
    pub fn weak(d: &str) -> String {
        if is_float(d) {
            "weakfloat".into()
        } else if is_int(d) {
            "weakint".into()
        } else {
            d.into()
        }
    }

    pub fn is_int(d: &str) -> bool {
        matches!(
            d,
            "char" | "short" | "int" | "long" | "uchar" | "ushort" | "uint" | "ulong" | "weakint"
        )
    }

    pub fn is_float(d: &str) -> bool {
        matches!(
            d,
            "half"
                | "bfloat16"
                | "float"
                | "double"
                | "weakfloat"
                | "fp8e4m3"
                | "fp8e5m2"
                | "fp8e4m3fnuz"
                | "fp8e5m2fnuz"
        )
    }
}
