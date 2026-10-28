//! A reduction down the rows as a halving tree (`runtime/tree.py`).

use super::*;

impl Runner<'_> {
    /// `tree.column`: a reduction down the rows as a halving tree.
    #[allow(clippy::too_many_arguments)]
    pub fn column(
        &mut self,
        e: &E,
        ops: &[Operand],
        binds: &BTreeMap<String, Addr>,
        r: i64,
        w: i64,
        out: &Operand,
        out_at: &Addr,
        info: &Value,
    ) -> Result<(), Refusal> {
        let reds = expr::reduces(e);
        if reds.len() != 1 {
            return row_err("a reduction down the rows holds one reduction");
        }
        let mut scratch = Vec::new();
        let got = self.tree(
            e,
            &reds[0],
            ops,
            binds,
            (r, w),
            out,
            out_at,
            info,
            &mut scratch,
        );
        for a in &scratch {
            self.x.free(a);
        }
        got
    }

    #[allow(clippy::too_many_arguments)]
    fn tree(
        &mut self,
        e: &E,
        red: &E,
        ops: &[Operand],
        bnd: &BTreeMap<String, Addr>,
        (r, w): (i64, i64),
        out: &Operand,
        out_at: &Addr,
        info: &Value,
        scratch: &mut Vec<String>,
    ) -> Result<(), Refusal> {
        let Expr::Reduce(rop, rarg) = red.as_ref() else {
            unreachable!()
        };
        let op = fold_op(rop);
        let (post, keep) = substitute(e, Some(red), Some(&expr::load(0)), 1);
        let row = 2 * w;
        let full = Operand {
            pitch: w,
            ..Operand::new("full")
        };
        let views = self.m.facts().vc.descriptors as i64;
        let mut n = r;
        let mut cur: Option<String> = None;
        if n > 1 && 2 * ops.len() as i64 + 1 > views {
            let c = self.x.raw(n * row);
            scratch.push(c.clone());
            let at = self.x.addr(&c, 0);
            self.rows_run(rarg, ops, bnd, n, w, &full, &at, info)?;
            cur = Some(c);
        }
        while n > 1 {
            let h = n / 2;
            let nxt = self.x.raw((h + n % 2) * row);
            scratch.push(nxt.clone());
            let (lvl, lvl_ops, lvl_b) = match &cur {
                None => {
                    let k = ops.len();
                    let lvl = expr::op(op, vec![rarg.clone(), renumber(rarg, k)]);
                    let mut o = ops.to_vec();
                    o.extend(ops.iter().map(|x| shift(x, h, w)));
                    let mut b = BTreeMap::new();
                    for i in 0..k {
                        b.insert(format!("in{i}"), bnd[&format!("in{i}")].clone());
                        b.insert(format!("in{}", i + k), bnd[&format!("in{i}")].clone());
                    }
                    if n % 2 == 1 {
                        let last: Vec<Operand> = ops.iter().map(|x| shift(x, n - 1, w)).collect();
                        let at = self.x.addr(&nxt, h * row);
                        self.rows_run(rarg, &last, bnd, 1, w, &full, &at, info)?;
                    }
                    (lvl, o, b)
                }
                Some(c) => {
                    let lvl = expr::op(op, vec![expr::load(0), expr::load(1)]);
                    let o = vec![
                        Operand {
                            pitch: w,
                            rows: n,
                            ..Operand::new("full")
                        },
                        Operand {
                            pitch: w,
                            offset: h * w,
                            rows: n,
                            ..Operand::new("full")
                        },
                    ];
                    let b = BTreeMap::from([
                        ("in0".to_string(), self.x.addr(c, 0)),
                        ("in1".to_string(), self.x.addr(c, 0)),
                    ]);
                    if n % 2 == 1 {
                        let call = Call {
                            gen: "convert.copy_kernel".into(),
                            args: json!({ "nbytes": row }),
                        };
                        let at = BTreeMap::from([
                            ("src".to_string(), self.x.addr(c, (n - 1) * row)),
                            ("dst".to_string(), self.x.addr(&nxt, h * row)),
                        ]);
                        self.x.launch(call, "copy", json!({}), at);
                    }
                    (lvl, o, b)
                }
            };
            let at = self.x.addr(&nxt, 0);
            self.rows_run(&lvl, &lvl_ops, &lvl_b, h, w, &full, &at, info)?;
            cur = Some(nxt);
            n = h + n % 2;
        }
        let (head, mut head_ops, mut head_b) = match &cur {
            None => (
                rarg.clone(),
                ops.to_vec(),
                (0..ops.len())
                    .map(|i| (format!("in{i}"), bnd[&format!("in{i}")].clone()))
                    .collect::<BTreeMap<_, _>>(),
            ),
            Some(c) => (
                expr::load(0),
                vec![Operand {
                    pitch: w,
                    rows: 1,
                    ..Operand::new("full")
                }],
                BTreeMap::from([("in0".to_string(), self.x.addr(c, 0))]),
            ),
        };
        let base = head_ops.len();
        let fin = place_head(&post, &head, base);
        for (j, i) in keep.iter().enumerate() {
            head_ops.push(ops[*i].clone());
            head_b.insert(format!("in{}", base + j), bnd[&format!("in{i}")].clone());
        }
        self.rows_run(&fin, &head_ops, &head_b, 1, w, out, out_at, info)
    }
}
