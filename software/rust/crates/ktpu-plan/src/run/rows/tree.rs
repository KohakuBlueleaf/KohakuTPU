//! A reduction down the rows as a halving tree (`runtime/tree.py`).

use super::*;

impl Runner<'_> {
    /// `tree.column`: a reduction down the rows as a halving tree. A pending
    /// read is made inside the first level's kernel (both halves of it); a
    /// level reading it twice over, made first.
    #[allow(clippy::too_many_arguments)]
    pub fn column(
        &mut self,
        e: &E,
        ops: &[Operand],
        binds: &BTreeMap<String, Bound>,
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
        if r > 1 && self.knobs.rows.single_package_ast {
            if self.prepared_tree(e, ops, binds, (r, w), out, out_at, info)? {
                return Ok(());
            }
            let mut binds = self.made_all(binds)?;
            binds.insert("out".into(), out_at.clone());
            self.launch_reduction("column_tree_kernel", e, ops, binds, r, w, out, info.clone());
            return Ok(());
        }
        let views = self.m.facts().vc.descriptors as i64;
        let binds = if r % 2 != 0 || 2 * ops.len() as i64 + 1 > views {
            bound(self.made_all(binds)?)
        } else {
            binds.clone()
        };
        let mut scratch = Vec::new();
        let got = self.tree(
            e,
            &reds[0],
            ops,
            &binds,
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

    /// `tree.column`'s `prepared_tree` branch (a measured win, every plan's
    /// destination direct); whether it launched.
    #[allow(clippy::too_many_arguments)]
    fn prepared_tree(
        &mut self,
        e: &E,
        ops: &[Operand],
        binds: &BTreeMap<String, Bound>,
        (r, w): (i64, i64),
        out: &Operand,
        out_at: &Addr,
        info: &Value,
    ) -> Result<bool, Refusal> {
        let pending: BTreeMap<usize, usize> = binds
            .iter()
            .filter_map(|(k, b)| match b {
                Bound::Pend(id) => Some((k[2..].parse().unwrap(), *id)),
                Bound::At(_) => None,
            })
            .collect();
        let steps: serde_json::Map<String, Value> = pending
            .iter()
            .map(|(i, id)| (i.to_string(), costs::steps_recipe(&self.pending[*id].steps)))
            .collect();
        let recipe = json!({
            "e": expr_arg(e),
            "operands": ops.iter().map(Operand::json).collect::<Vec<_>>(),
            "R": r,
            "W": w,
            "out": out.json(),
            "steps": steps,
        });
        if pending.is_empty() || !costs::choose("prepared_tree", &recipe, &self.m.name) {
            return Ok(false);
        }
        let mut plans = BTreeMap::new();
        for (i, id) in &pending {
            match self.plans_of(&self.pending[*id].steps.clone())? {
                Some(p) if prepared::direct_destination(&p) => plans.insert(*i, p),
                _ => return Ok(false),
            };
        }
        let mut plain: BTreeMap<String, Addr> = binds
            .iter()
            .filter_map(|(k, b)| match b {
                Bound::At(a) => Some((k.clone(), a.clone())),
                Bound::Pend(_) => None,
            })
            .collect();
        plain.insert("out".into(), out_at.clone());
        let mut made = serde_json::Map::new();
        for (i, id) in &pending {
            let steps = &self.pending[*id].steps;
            made.insert(i.to_string(), steps.iter().map(Step::made_by).collect());
            let src = steps[0].src.clone().expect("a first step reads a source");
            plain.insert(format!("p{i}_x"), src);
            self.step_binds(&plans[i], &format!("p{i}_"), &mut plain);
        }
        let call = Call {
            gen: "reductions.column_tree_kernel".into(),
            args: json!({
                "e": expr_arg(e),
                "operands": ops.iter().map(Operand::json).collect::<Vec<_>>(),
                "R": r,
                "W": w,
                "out": out.json(),
                "cores": self.cores(),
                "made": made,
            }),
        };
        self.x.launch(call, "dsl", info.clone(), plain);
        Ok(true)
    }

    #[allow(clippy::too_many_arguments)]
    fn tree(
        &mut self,
        e: &E,
        red: &E,
        ops: &[Operand],
        bnd: &BTreeMap<String, Bound>,
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
        let row_split = (n / 2) % self.stat_rows() != 0 || n % 2 != 0;
        if n > 1
            && (2 * ops.len() as i64 + 1 > views
                || (ops.iter().any(|o| o.kind == "row") && row_split))
        {
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
                    let b = bound(BTreeMap::from([
                        ("in0".to_string(), self.x.addr(c, 0)),
                        ("in1".to_string(), self.x.addr(c, 0)),
                    ]));
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
                BTreeMap::from([("in0".to_string(), Bound::At(self.x.addr(c, 0)))]),
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
