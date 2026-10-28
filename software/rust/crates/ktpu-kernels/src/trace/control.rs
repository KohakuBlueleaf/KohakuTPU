//! Control statements (DSL.CTRL): `par`, `for`, `when`. Each runs its body
//! once with the variable as an index.

use super::{fail, here, var, Cond, Idx, Scope, Trace, R};
use ktpu_graph as g;

/// Which unit a `par` instance names: its variable, or `KIND[any]` (the
/// place pass places it).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum At {
    Var,
    Any,
}

fn prefix(kind: g::UnitKind) -> &'static str {
    match kind {
        g::UnitKind::Mg => "u",
        g::UnitKind::Vc => "c",
        g::UnitKind::Node => "n",
    }
}

fn par_node(v: &str, lo: &Idx, hi: &Idx, kind: g::UnitKind, at: Option<&Idx>) -> g::Node {
    g::Node::Par {
        var: v.to_string(),
        lo: lo.graph(),
        hi: hi.graph(),
        unit: g::UnitRef {
            kind,
            index: at.map(Idx::graph),
        },
        classed: false,
        repeats: None,
        advances: vec![],
        body: vec![],
    }
}

impl Trace {
    /// `par v in 0..n on KIND[v]` (DSL.CTRL.001, .011). More `mg` / `vc`
    /// instances than a node has, outside a par over nodes, span the
    /// machine's nodes: `par n on node[n]` around `par u on KIND[u]`, the
    /// variable `n * per + u`.
    #[track_caller]
    pub fn par(
        &mut self,
        n: impl Into<Idx>,
        on: g::UnitKind,
        at: At,
        body: impl FnOnce(&mut Trace, Idx) -> R<()>,
    ) -> R<()> {
        let at_line = here();
        if self.scope().kind.is_some() {
            return fail(
                "DSL.CTRL.012",
                format!("a par inside a unit's instance, at {at_line}"),
            );
        }
        let (lo, hi) = (Idx::Int(0), n.into());
        let per = match on {
            g::UnitKind::Mg => Some(self.facts.mg),
            g::UnitKind::Vc => Some(self.facts.vc),
            g::UnitKind::Node => None,
        };
        if let (Some(top), Some(per)) = (hi.int(), per) {
            if top > per && self.facts.nodes > 1 && !self.scope().node {
                return self.over_nodes(on, top, per, at, at_line, body);
            }
        }
        let v = self.fresh(prefix(on));
        let sym = var(&v);
        let unit = (at == At::Var).then_some(&sym);
        let node = par_node(&v, &lo, &hi, on, unit);
        let scope = Scope {
            kind: (on != g::UnitKind::Node).then_some(on),
            in_loop: false,
            sticky: false,
            node: on == g::UnitKind::Node || self.scope().node,
        };
        self.block(node, at_line, scope, |t| body(t, sym.clone()))
    }

    fn over_nodes(
        &mut self,
        on: g::UnitKind,
        count: i64,
        per: i64,
        at: At,
        at_line: String,
        body: impl FnOnce(&mut Trace, Idx) -> R<()>,
    ) -> R<()> {
        let nodes = self.facts.nodes;
        let (share, used, ok) = match at {
            At::Var => (per, count / per, count % per == 0 && count / per <= nodes),
            At::Any => (count / nodes, nodes, count % nodes == 0),
        };
        if !ok {
            return fail(
                "DSL.CTRL.001",
                format!(
                    "{count} {} instances do not split evenly over {nodes} nodes of {per}, at {at_line}",
                    on.name()
                ),
            );
        }
        let nv = self.fresh("n");
        let node = par_node(
            &nv,
            &Idx::Int(0),
            &Idx::Int(used),
            g::UnitKind::Node,
            Some(&var(&nv)),
        );
        let outer = Scope {
            kind: None,
            in_loop: false,
            sticky: false,
            node: true,
        };
        self.block(node, at_line.clone(), outer, |t| {
            let v = t.fresh(prefix(on));
            let unit = var(&v);
            let inner = par_node(
                &v,
                &Idx::Int(0),
                &Idx::Int(share),
                on,
                (at == At::Var).then_some(&unit),
            );
            let scope = Scope {
                kind: Some(on),
                ..Scope::default()
            };
            t.block(inner, at_line, scope, |t| {
                body(t, var(&nv).mul(share).add(var(&v)))
            })
        })
    }

    /// `for v in 0..n` (DSL.CTRL.004): variables `t1..` on a vector core,
    /// `k1..` elsewhere.
    #[track_caller]
    pub fn range(
        &mut self,
        n: impl Into<Idx>,
        body: impl FnOnce(&mut Trace, Idx) -> R<()>,
    ) -> R<()> {
        let at_line = here();
        let (lo, hi) = (Idx::Int(0), n.into());
        let prefix = if self.scope().kind == Some(g::UnitKind::Vc) {
            "t"
        } else {
            "k"
        };
        let v = self.fresh(prefix);
        let node = g::Node::For {
            var: v.clone(),
            lo: lo.graph(),
            hi: hi.graph(),
            pipe: None,
            body: vec![],
        };
        let s = self.scope();
        let scope = Scope {
            kind: s.kind,
            in_loop: true,
            sticky: s.sticky,
            node: false,
        };
        self.block(node, at_line, scope, |t| body(t, var(&v)))
    }

    /// `when COND` (DSL.CTRL.006).
    #[track_caller]
    pub fn when(&mut self, cond: Cond, body: impl FnOnce(&mut Trace) -> R<()>) -> R<()> {
        let at_line = here();
        let node = g::Node::When {
            cond: cond.graph(),
            body: vec![],
        };
        let scope = self.scope();
        self.block(node, at_line, scope, body)
    }
}
