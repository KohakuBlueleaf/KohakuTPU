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

/// The reverse mutant of a classed `par`'s band: no schedule recorded.
pub const BANDLESS_MUTANT: &str = "kernels.class/bandless";

/// A classed `par`'s schedule (DSL.CTRL.014): its row-band iterations, and
/// the repeats of a node-level row band with each read's advance.
#[derive(Clone, Debug, Default)]
pub struct Classed {
    pub band: Option<i64>,
    pub repeats: Option<i64>,
    pub advances: Vec<i64>,
}

fn par_node(
    v: &str,
    lo: &Idx,
    hi: &Idx,
    kind: g::UnitKind,
    at: Option<&Idx>,
    classed: Option<&Classed>,
) -> g::Node {
    g::Node::Par {
        var: v.to_string(),
        lo: lo.graph(),
        hi: hi.graph(),
        unit: g::UnitRef {
            kind,
            index: at.map(Idx::graph),
        },
        classed: classed.is_some(),
        class_band: classed
            .and_then(|c| c.band)
            .filter(|_| !crate::mutants::on(BANDLESS_MUTANT)),
        repeats: classed.and_then(|c| c.repeats),
        advances: classed.map_or(vec![], |c| c.advances.clone()),
        body: vec![],
    }
}

impl Trace {
    /// A common stage on an ordered, validated grant; the body sees logical rank.
    pub fn on_nodes(
        &mut self,
        nodes: &crate::split::NodeSet,
        body: impl FnOnce(&mut Trace, Idx) -> R<()>,
    ) -> R<()> {
        if self.scope().node || self.scope().kind.is_some() {
            return fail("DSL.CTRL.001", "nested explicit sysnode scope");
        }
        let name = self.fresh("n");
        let rank = var(&name);
        let ids = nodes.ids();
        let p = ids.len() as i64;
        let mut physical = Idx::Int(ids[0]);
        for i in 1..ids.len() {
            physical = physical.add(rank.add(p - i as i64).div(p).mul(ids[i] - ids[i - 1]));
        }
        let node = par_node(
            &name,
            &Idx::Int(0),
            &Idx::Int(p),
            g::UnitKind::Node,
            Some(&physical),
            None,
        );
        let previous = self.stage_node;
        self.stage_node = (ids.len() == 1).then_some(ids[0]);
        let result = self.block(
            node,
            here(),
            Scope {
                node: true,
                ..Scope::default()
            },
            |t| body(t, rank),
        );
        self.stage_node = previous;
        result
    }

    /// Emit a share on one physical sysnode, independently of local unit counts.
    #[track_caller]
    pub fn on_node(&mut self, id: i64, body: impl FnOnce(&mut Trace) -> R<()>) -> R<()> {
        if id < 0 || id >= self.facts.nodes || self.scope().node || self.scope().kind.is_some() {
            return fail("DSL.CTRL.001", "invalid or nested explicit sysnode scope");
        }
        let name = self.fresh("n");
        let node = par_node(
            &name,
            &Idx::Int(id),
            &Idx::Int(id + 1),
            g::UnitKind::Node,
            Some(&var(&name)),
            None,
        );
        let scope = Scope {
            node: true,
            ..Scope::default()
        };
        let previous = self.stage_node.replace(id);
        let result = self.block(node, here(), scope, body);
        self.stage_node = previous;
        result
    }

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
        let node = par_node(&v, &lo, &hi, on, unit, None);
        let scope = Scope {
            kind: (on != g::UnitKind::Node).then_some(on),
            in_loop: false,
            sticky: false,
            node: on == g::UnitKind::Node || self.scope().node,
        };
        self.block(node, at_line, scope, |t| body(t, sym.clone()))
    }

    /// Local virtual unit instances, even on a machine with several nodes.
    /// Typed generators use this when ownership belongs to an enclosing split
    /// plan: placement may queue virtual instances but cannot invent owners.
    #[track_caller]
    pub fn par_local(
        &mut self,
        n: i64,
        on: g::UnitKind,
        at: At,
        body: impl FnOnce(&mut Trace, Idx) -> R<()>,
    ) -> R<()> {
        let units = match on {
            g::UnitKind::Mg => self.facts.mg,
            g::UnitKind::Vc => self.facts.vc,
            g::UnitKind::Node => return fail("DSL.CTRL.001", "local units are MG or VC"),
        };
        if n <= 0 || self.scope().kind.is_some() || at == At::Var && n > units {
            return fail("DSL.CTRL.001", "invalid local unit instances");
        }
        let name = self.fresh(prefix(on));
        let index = var(&name);
        let node = par_node(
            &name,
            &Idx::Int(0),
            &Idx::Int(n),
            on,
            (at == At::Var).then_some(&index),
            None,
        );
        let scope = Scope {
            kind: Some(on),
            node: self.scope().node,
            ..Scope::default()
        };
        self.block(node, here(), scope, |t| body(t, index))
    }

    /// `par v in 0..n on vc[v] classed=1 ..`: this node's vector cores, one
    /// image a core (DSL.CTRL.014).
    #[track_caller]
    pub fn par_classed(
        &mut self,
        n: i64,
        classed: Classed,
        body: impl FnOnce(&mut Trace, Idx) -> R<()>,
    ) -> R<()> {
        let at_line = here();
        let on = g::UnitKind::Vc;
        let v = self.fresh(prefix(on));
        let sym = var(&v);
        let (lo, hi) = (Idx::Int(0), Idx::Int(n));
        let node = par_node(&v, &lo, &hi, on, Some(&sym), Some(&classed));
        let scope = Scope {
            kind: Some(on),
            in_loop: false,
            sticky: false,
            node: self.scope().node,
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
            At::Any if count % nodes == 0 => (count / nodes, nodes, true),
            // The place pass queues what no even split takes on one node.
            At::Any => (count, 1, true),
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
            None,
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
                None,
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
        self.range_pipe(n, None, body)
    }

    /// `for v in 0..n [pipe=K]` (DSL.CTRL.005): at node scope K iterations
    /// in flight.
    #[track_caller]
    pub fn range_pipe(
        &mut self,
        n: impl Into<Idx>,
        pipe: Option<i64>,
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
            pipe,
            body: vec![],
        };
        let s = self.scope();
        let scope = Scope {
            kind: s.kind,
            in_loop: true,
            sticky: s.sticky,
            node: s.node,
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
