//! Cluster statements (DSL.CL): fill, zero, gemm, drain.

use super::{fail, here, Trace, ValueData, View, R, V};
use ktpu_graph as g;

impl Trace {
    /// Drain native accumulator subtiles into a peer cluster's accumulator.
    pub fn drain_add(&mut self, acc: V, unit: &super::Idx, node: i64) -> R<()> {
        let at = here();
        self.need(Some(g::UnitKind::Mg), "drain")?;
        let av = self.value(acc);
        if av.space != g::ValSpace::Acc {
            return fail("DSL.CL.008", format!("drain takes an @acc value, at {at}"));
        }
        let op = g::Node::Drain {
            src: av.name.clone(),
            dst: g::Operand::Unit(g::UnitRef {
                kind: g::UnitKind::Mg,
                index: Some(unit.graph()),
            }),
            add: true,
            node: Some(super::Idx::Int(node).graph()),
        };
        self.emit(op, at);
        Ok(())
    }

    /// `aN = fill VIEW : mx7a @A` / `bN = .. @B` (DSL.CL.001).
    #[track_caller]
    pub fn fill(&mut self, view: &View) -> R<V> {
        let at = here();
        self.need(Some(g::UnitKind::Mg), "fill")?;
        let dtype = view.buf.spec.dtype;
        let (space, prefix) = match dtype {
            g::Dtype::Mx7a => (g::ValSpace::A, "a"),
            g::Dtype::Mx7b => (g::ValSpace::B, "b"),
            d => {
                return fail(
                    "DSL.CL.006",
                    format!(
                        "fill reads an mx7a or mx7b buffer, not {}, at {at}",
                        d.name()
                    ),
                )
            }
        };
        let args = vec![g::Operand::View(view.graph())];
        let ty = (dtype, view.shape(), space, None);
        Ok(self.new_value(g::ValueOp::Fill, args, vec![], ty, prefix, None, at))
    }

    /// `accN = zero : f32[M, N] @acc` (DSL.CL.002).
    #[track_caller]
    pub fn zero(&mut self, m: i64, n: i64) -> R<V> {
        let at = here();
        self.need(Some(g::UnitKind::Mg), "zero")?;
        let ty = (g::Dtype::F32, vec![m, n], g::ValSpace::Acc, None);
        Ok(self.new_value(g::ValueOp::Zero, vec![], vec![], ty, "acc", None, at))
    }

    /// `ACC = gemm A, B, ACC` (DSL.CL.003): rebinds the accumulator's name.
    #[track_caller]
    pub fn gemm(&mut self, a: V, b: V, acc: V) -> R<V> {
        let at = here();
        self.need(Some(g::UnitKind::Mg), "gemm")?;
        let (av, bv, cv) = (
            self.value(a).clone(),
            self.value(b).clone(),
            self.value(acc).clone(),
        );
        if av.space != g::ValSpace::A || bv.space != g::ValSpace::B {
            return fail(
                "DSL.CL.007",
                format!("gemm reads an @A value and an @B value, at {at}"),
            );
        }
        let shape = vec![av.shape[0], bv.shape[0]];
        if cv.space != g::ValSpace::Acc || cv.shape != shape {
            return fail(
                "DSL.CL.007",
                format!("gemm accumulates into an f32{shape:?} @acc value, at {at}"),
            );
        }
        let data = ValueData {
            name: cv.name.clone(),
            dtype: g::Dtype::F32,
            shape,
            space: g::ValSpace::Acc,
            mode: None,
            unpacked: None,
            block: 0,
            stmt: 0,
            tiled: false,
            lane: false,
            row_bcast: false,
            composed: false,
        };
        let node = g::Node::Value {
            op: g::ValueOp::Gemm,
            target: cv.name.clone(),
            ty: data.graph_type(),
            args: vec![
                g::Operand::Value(av.name),
                g::Operand::Value(bv.name),
                g::Operand::Value(cv.name),
            ],
            kw: vec![],
        };
        Ok(self.rebound(data, node, at))
    }

    /// `drain ACC -> VIEW` (DSL.CL.004).
    #[track_caller]
    pub fn drain(&mut self, acc: V, dst: &View) -> R<()> {
        let at = here();
        self.need(Some(g::UnitKind::Mg), "drain")?;
        let av = self.value(acc);
        if av.space != g::ValSpace::Acc {
            return fail("DSL.CL.008", format!("drain takes an @acc value, at {at}"));
        }
        let node = g::Node::Drain {
            src: av.name.clone(),
            dst: g::Operand::View(dst.graph()),
            add: false,
            node: None,
        };
        self.emit(node, at);
        Ok(())
    }
}
