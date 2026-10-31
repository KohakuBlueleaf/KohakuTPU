//! The instance's code: constants (L2.VC.012), the prologue before the
//! stream loop, and one loop iteration's band order (L2.VC.005, .013, .016).

use super::*;

pub(super) mod typed;

impl<'a> Ctx<'a> {
    // ------------------------------------------------------------- constants

    // -------------------------------------------------------------- prologue

    pub(super) fn get(
        &self,
        o: &Op,
        t: &Term,
        kind: Option<&str>,
        local: Option<&IndexMap<String, Val>>,
    ) -> VResult<Val> {
        let Term::Name(n) = t else {
            return Err(self.fail(
                &o.st,
                "L2.REF.003",
                format!("wanted a value name, got {}", repr(t)),
            ));
        };
        let v = local.and_then(|l| l.get(n)).or_else(|| self.vals.get(n));
        let Some(v) = v else {
            return Err(self.fail(&o.st, "L2.REF.003", format!("no value {n}")));
        };
        if let Some(k) = kind {
            if v.kind != k {
                return Err(self.fail(
                    &o.st,
                    "L2.REF.003",
                    format!("{n} is a {} value, not {k}", v.kind),
                ));
            }
        }
        Ok(v.clone())
    }

    // ------------------------------------------------------------- iteration

    /// One `if sticky` block of math whose new names stay inside it.
    fn check_if(&self, body: &[Arc<Node>]) -> VResult<()> {
        if crate::mutant::on("l2.vector/sticky-unchecked") {
            return Ok(());
        }
        let blocks: Vec<usize> = body
            .iter()
            .enumerate()
            .filter(|(_, o)| matches!(o.as_ref(), Node::If(_)))
            .map(|(i, _)| i)
            .collect();
        if blocks.len() > 1 {
            return Err(self.fail(
                body[blocks[1]].st(),
                "L2.OP.027",
                "one if sticky block a stream loop",
            ));
        }
        let at = blocks[0];
        let mut before: HashSet<String> = body[..at]
            .iter()
            .filter_map(|o| o.target().map(str::to_string))
            .collect();
        before.extend(self.vals.keys().cloned());
        let mut inner: HashSet<String> = HashSet::new();
        for o in ops_of(body[at].body()) {
            let x = op(&o);
            if ["load", "store", "send", "unpack", "pack"].contains(&x.op.as_str()) {
                return Err(self.fail(&x.st, "L2.OP.027", "if sticky holds math and reductions"));
            }
            if let Some(t) = &x.target {
                if !before.contains(t) {
                    inner.insert(t.clone());
                }
            }
        }
        for o in ops_of(&body[at + 1..]) {
            let x = op(&o);
            for a in x.st.positional() {
                if let Some(n) = value_name(a) {
                    if inner.contains(n) {
                        return Err(self.fail(
                            &x.st,
                            "L2.OP.027",
                            format!("{n} is made only inside if sticky"),
                        ));
                    }
                }
            }
        }
        Ok(())
    }
}
