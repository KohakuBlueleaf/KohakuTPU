//! The session's ops: tensors made, elementwise ops fused, reductions,
//! matmul, reshape, transpose, numpy.

use super::*;

impl<'a> Session<'a> {
    pub fn new(m: &'a Machine) -> Session<'a> {
        Session {
            r: Runner::new(m, Graph::default(), Exec::default()),
            ts: Vec::new(),
            fuse: true,
        }
    }

    fn push(&mut self, t: Tensor) -> usize {
        self.ts.push(t);
        self.ts.len() - 1
    }

    pub fn steps(&self) -> &[Step] {
        &self.r.x.steps
    }

    /// `set_fusion`: the previous setting.
    pub fn set_fusion(&mut self, on: bool) -> bool {
        std::mem::replace(&mut self.fuse, on)
    }

    /// `tensor`: host bytes (`dtype` elements, `shape`) uploaded.
    pub fn tensor(&mut self, shape: &[i64], dtype: &str, host: Digest) -> Result<usize, Refusal> {
        if dtype != "float16"
            && dtype != "float32"
            && !crate::spec::mutant::on("num.api/int-tensor-accepted")
        {
            return unsupported(
                "API.TEN.001",
                format!("a {dtype} tensor: the card computes on f16 / f32"),
            );
        }
        let st = self.r.x.storage(host.bytes.max(self.r.hw.word));
        let at = self.r.x.addr(&st, 0);
        self.r.x.write(at, host.bytes, host.sha256.clone());
        let s = self.r.x.store_mut(&st);
        s.host = Some(host);
        s.dtype = Some(dtype.into());
        Ok(self.push(Tensor {
            shape: shape.to_vec(),
            buf: Some((st, dtype.into())),
            node: None,
            stat: false,
            t_of: None,
        }))
    }

    fn check_flat(&self, t: usize, why: &str) -> Result<(), Refusal> {
        if self.ts[t].t_of.is_some() {
            return unsupported(
                "API.TEN.006",
                format!("{why}: a transposed view is read only as matmul's right operand"),
            );
        }
        Ok(())
    }

    /// `_fusable`: may lazy `a` be inlined into an expression over `view`?
    fn fusable(
        &self,
        a: usize,
        view: &[i64],
        wide: bool,
        have_prod: &Option<KE>,
        have_red: usize,
    ) -> bool {
        let Some(node) = self.ts[a]
            .node
            .as_ref()
            .filter(|n| self.fuse && n.view == view)
        else {
            return false;
        };
        let reds = reduces(&node.expr).len();
        let prod = prod_of(&node.expr);
        if let Some(p) = &prod {
            let other = have_prod.as_ref().is_some_and(|h| !Rc::ptr_eq(h, p));
            if other || have_red > 0 {
                return false;
            }
        }
        if (prod.is_some() || have_prod.is_some()) && reds > 0 {
            return false;
        }
        let widest = self.r.max_block();
        !(reds > 0 && *view.last().unwrap() > widest && (wide || have_red + reds > 1))
    }

    /// `_leaf`: a number as a constant, a fusable lazy tensor inlined, any
    /// other tensor realised and read.
    fn leaf(
        &mut self,
        a: Arg,
        view: &[i64],
        wide: bool,
        state: &mut (Option<KE>, usize),
    ) -> Result<KE, Refusal> {
        let a = match a {
            Arg::N(v) => return Ok(Rc::new(K::Const(v))),
            Arg::T(t) => t,
        };
        self.check_flat(a, "an elementwise operand")?;
        if self.ts[a].node.is_some() && self.fusable(a, view, wide, &state.0, state.1) {
            let e = self.ts[a].node.as_ref().unwrap().expr.clone();
            state.1 += reduces(&e).len();
            if state.0.is_none() {
                state.0 = prod_of(&e);
            }
            return Ok(e);
        }
        self.realize(a)?;
        Ok(Rc::new(K::Ref(a)))
    }

    /// `_ew`: an elementwise op.
    pub fn ew(&mut self, name: &str, args: &[Arg]) -> Result<usize, Refusal> {
        let ts: Vec<usize> = args
            .iter()
            .filter_map(|a| match a {
                Arg::T(t) => Some(*t),
                Arg::N(_) => None,
            })
            .collect();
        let shapes: Vec<&[i64]> = ts.iter().map(|t| self.ts[*t].shape.as_slice()).collect();
        let Some(shape) = broadcast(&shapes) else {
            let all: Vec<String> = shapes.iter().map(|s| py_tuple(s)).collect();
            return unsupported(
                "API.OP.002",
                format!(
                    "{name} of shapes [{}]: they do not broadcast",
                    all.join(", ")
                ),
            );
        };
        let stat = ts.iter().all(|t| self.ts[*t].stat);
        for t in &ts {
            let x = &self.ts[*t];
            if !stat && x.stat && x.size() > 1 && x.shape.last() != Some(&1) {
                return unsupported(
                    "API.READ.002",
                    format!(
                        "a row statistic {} broadcast as a row of values against {}: keep the reduced axis (keepdim=True)",
                        py_tuple(&x.shape),
                        py_tuple(&shape)
                    ),
                );
            }
        }
        let mut view = rows_view(&shape, stat);
        if stat {
            for t in &ts {
                if let Some(n) = &self.ts[*t].node {
                    let k = view.len() - 1;
                    if n.view.len() == view.len() && n.view[..k] == view[..k] && n.view[k] != 1 {
                        view = n.view.clone();
                        break;
                    }
                }
            }
        }
        let mut state = (None, 0);
        let mut ls = Vec::new();
        for a in args {
            ls.push(self.leaf(*a, &view, !stat, &mut state)?);
        }
        let node = Node {
            expr: Rc::new(K::Op(name.into(), ls)),
            view,
        };
        Ok(self.push(Tensor {
            shape,
            buf: None,
            node: Some(node),
            stat,
            t_of: None,
        }))
    }

    /// `_reduce`: a reduction along the last axis (or every value).
    pub fn reduce(
        &mut self,
        op: &str,
        t: usize,
        axis: Option<i64>,
        keepdim: bool,
    ) -> Result<usize, Refusal> {
        self.check_flat(t, &format!("a {op}"))?;
        let ndim = self.ts[t].shape.len() as i64;
        let Some(axis) = axis else {
            if self.ts[t].stat {
                return unsupported(
                    "API.OP.003",
                    format!("a {op} over every row statistic: reduce the rows themselves"),
                );
            }
            self.realize(t)?;
            let size = self.ts[t].size();
            let flat_t = self.reshape(t, &[1, size])?;
            let r = self.reduce(op, flat_t, Some(-1), true)?;
            let shape = if keepdim {
                vec![1; ndim as usize]
            } else {
                vec![]
            };
            let node = self.ts[r].node.clone();
            return Ok(self.push(Tensor {
                shape,
                buf: None,
                node,
                stat: true,
                t_of: None,
            }));
        };
        if axis != -1 && axis != ndim - 1 {
            return unsupported(
                "API.OP.003",
                format!(
                    "a {op} over axis {axis} of {}: rows are the last axis",
                    py_tuple(&self.ts[t].shape)
                ),
            );
        }
        if self.ts[t].stat {
            return unsupported(
                "API.OP.003",
                format!("a {op} along a row statistic's rows: reduce the rows themselves"),
            );
        }
        let view = self.ts[t].rows();
        let mut state = (None, 1);
        let mut leaf = self.leaf(Arg::T(t), &view, false, &mut state)?;
        if state.0.is_some() {
            self.realize(t)?;
            leaf = Rc::new(K::Ref(t));
        }
        let mut shape = self.ts[t].shape[..ndim as usize - 1].to_vec();
        if keepdim {
            shape.push(1);
        }
        let node = Node {
            expr: Rc::new(K::Reduce(op.into(), leaf)),
            view,
        };
        Ok(self.push(Tensor {
            shape,
            buf: None,
            node: Some(node),
            stat: true,
            t_of: None,
        }))
    }

    /// `matmul`: `a @ b`, `b` the `.T` view of an [N, K] tensor.
    pub fn matmul(&mut self, a: usize, b: usize) -> Result<usize, Refusal> {
        let Some(w) = self.ts[b].t_of else {
            return unsupported(
                "API.OP.005",
                "x @ w with w as [K, N]: the clusters read B as [N, K] rows; store the weight [N, K] and write x @ w.T",
            );
        };
        let (sa, sb, sw) = (
            self.ts[a].shape.clone(),
            self.ts[b].shape.clone(),
            self.ts[w].shape.clone(),
        );
        if sa.len() < 2 || sa.last() != sw.last() {
            return unsupported(
                "API.OP.005",
                format!(
                    "matmul of {} and {}: K differs or A is not 2-D",
                    py_tuple(&sa),
                    py_tuple(&sb)
                ),
            );
        }
        if sw.len() > 2 && sw[..sw.len() - 2] != sa[..sa.len() - 2] {
            return unsupported(
                "API.OP.005",
                format!(
                    "batched B {} for A {}: one B, or one a batch",
                    py_tuple(&sw),
                    py_tuple(&sa)
                ),
            );
        }
        self.check_flat(a, "matmul's left operand")?;
        self.realize(a)?;
        let mut shape = sa[..sa.len() - 1].to_vec();
        shape.push(sw[sw.len() - 2]);
        let node = Node {
            expr: Rc::new(K::Prod(a, w)),
            view: shape.clone(),
        };
        Ok(self.push(Tensor {
            shape,
            buf: None,
            node: Some(node),
            stat: false,
            t_of: None,
        }))
    }

    /// `reshape`: realised, a tensor over the same buffer (`-1` inferred).
    pub fn reshape(&mut self, t: usize, shape: &[i64]) -> Result<usize, Refusal> {
        let size = self.ts[t].size();
        let known: i64 = shape.iter().filter(|d| **d != -1).product();
        let holes = shape.iter().filter(|d| **d == -1).count();
        let shape: Vec<i64> = shape
            .iter()
            .map(|d| {
                if *d == -1 && known != 0 {
                    size / known
                } else {
                    *d
                }
            })
            .collect();
        if holes > 1 || shape.iter().product::<i64>() != size {
            return unsupported(
                "API.TEN.005",
                format!(
                    "cannot reshape array of size {size} into shape {}",
                    py_tuple(&shape)
                ),
            );
        }
        if self.ts[t].t_of.is_some() {
            return unsupported(
                "API.TEN.005",
                "a reshape of a transposed view: only matmul reads one",
            );
        }
        self.realize(t)?;
        let (buf, stat) = (self.ts[t].buf.clone(), self.ts[t].stat);
        Ok(self.push(Tensor {
            shape,
            buf,
            node: None,
            stat,
            t_of: None,
        }))
    }

    /// `.T`: the last two axes swapped, a view of the same buffer.
    pub fn transpose(&mut self, t: usize) -> Result<usize, Refusal> {
        let n = self.ts[t].shape.len();
        if n < 2 {
            return unsupported("API.TEN.006", format!("a transpose of a rank-{n} tensor"));
        }
        if let Some(base) = self.ts[t].t_of {
            return Ok(base);
        }
        self.realize(t)?;
        let mut shape = self.ts[t].shape.clone();
        shape.swap(n - 2, n - 1);
        let buf = self.ts[t].buf.clone();
        Ok(self.push(Tensor {
            shape,
            buf,
            node: None,
            stat: false,
            t_of: Some(t),
        }))
    }

    /// `numpy`: realised (its base, for a transposed view).
    pub fn numpy(&mut self, t: usize) -> Result<(), Refusal> {
        self.realize(t)?;
        if let Some(base) = self.ts[t].t_of {
            self.numpy(base)?;
        }
        Ok(())
    }
}
