//! Mover statements at node scope (DSL.MOV): copy, quant, gt4, fill,
//! gather, scatter.

use super::{fail, here, Trace, View, R};
use ktpu_graph as g;

/// `(count, signed byte stride)` levels, outermost first.
pub type Walk = [(i64, i64)];

/// A mover keyword as written: a flag, an int, a walk or an index list.
enum Kw<'a> {
    Flag(bool),
    Int(Option<i64>),
    Walk(Option<&'a Walk>),
    Index(&'a View),
}

impl Trace {
    fn mover(
        &mut self,
        op: g::MoverOp,
        dst: &View,
        src: Option<&View>,
        kw: &[(&str, Kw)],
        at: String,
    ) -> R<()> {
        self.need(None, op.name())?;
        let flags = kw
            .iter()
            .filter(|(_, v)| matches!(v, Kw::Flag(true)))
            .map(|(k, _)| k.to_string())
            .collect();
        let attrs = kw
            .iter()
            .filter_map(|(k, v)| {
                let a = match v {
                    Kw::Flag(_) | Kw::Int(None) | Kw::Walk(None) => return None,
                    Kw::Int(Some(i)) => g::Attr::Int(*i),
                    Kw::Walk(Some(w)) => g::Attr::Walk(w.to_vec()),
                    Kw::Index(v) => g::Attr::View(v.graph()),
                };
                Some((k.to_string(), a))
            })
            .collect();
        let node = g::Node::Mover {
            op,
            dst: dst.graph(),
            src: src.map(View::graph),
            flags,
            kw: attrs,
        };
        self.emit(node, at);
        Ok(())
    }

    /// `copy DST <- SRC [walk=..] [at=..] [dwalk=..] [dat=..]` (DSL.MOV.001,
    /// .002): a walk moves a whole buffer.
    #[track_caller]
    pub fn copy(
        &mut self,
        dst: &View,
        src: &View,
        walk: Option<(&Walk, Option<i64>)>,
        dwalk: Option<(&Walk, Option<i64>)>,
    ) -> R<()> {
        let at = here();
        if (walk.is_some() && !src.whole) || (dwalk.is_some() && !dst.whole) {
            return fail(
                "DSL.MOV.010",
                format!("a walk copy walks a whole buffer, at {at}"),
            );
        }
        let kw = [
            ("walk", Kw::Walk(walk.map(|w| w.0))),
            ("at", Kw::Int(walk.and_then(|w| w.1))),
            ("dwalk", Kw::Walk(dwalk.map(|w| w.0))),
            ("dat", Kw::Int(dwalk.and_then(|w| w.1))),
        ];
        self.mover(g::MoverOp::Copy, dst, Some(src), &kw, at)
    }

    /// `quant DST <- SRC` (DSL.MOV.003).
    #[track_caller]
    pub fn quant(&mut self, dst: &View, src: &View) -> R<()> {
        let at = here();
        self.mover(g::MoverOp::Quant, dst, Some(src), &[], at)
    }

    /// `finite=1` is an explicit caller-validated FP16 input contract.
    #[track_caller]
    pub fn quant_finite(&mut self, dst: &View, src: &View, finite: bool) -> R<()> {
        let kw = [("finite", Kw::Int(finite.then_some(1)))];
        self.mover(g::MoverOp::Quant, dst, Some(src), &kw, here())
    }

    /// Explicit quant scheduling attributes are part of the graph identity;
    /// `pad`: entries past the source pack zeros (L2 padded vector packing).
    #[track_caller]
    pub fn quant_scheduled(
        &mut self,
        dst: &View,
        src: &View,
        finite: bool,
        schedule: &[(&str, i64)],
        pad: bool,
    ) -> R<()> {
        let mut kw = vec![
            ("finite", Kw::Int(finite.then_some(1))),
            ("pad", Kw::Int(pad.then_some(1))),
        ];
        if !crate::mutants::on("kernels.quant/no-schedule") {
            kw.extend(schedule.iter().map(|&(k, v)| (k, Kw::Int(Some(v)))));
        }
        self.mover(g::MoverOp::Quant, dst, Some(src), &kw, here())
    }

    /// A checked [`Trace::quant_scheduled`] whose finite status drains into
    /// `status` with no barrier, checked by a later [`Trace::quant_check`]
    /// (L2.MOV.010).
    #[track_caller]
    pub fn quant_deferred(
        &mut self,
        dst: &View,
        src: &View,
        status: &View,
        schedule: &[(&str, i64)],
        pad: bool,
    ) -> R<()> {
        let mut kw = vec![
            ("status", Kw::Index(status)),
            ("pad", Kw::Int(pad.then_some(1))),
        ];
        kw.extend(schedule.iter().map(|&(k, v)| (k, Kw::Int(Some(v)))));
        self.mover(g::MoverOp::Quant, dst, Some(src), &kw, here())
    }

    /// `quant_check STATUS`: one barrier, then every deferred quant status
    /// record pending in `status` checked.
    #[track_caller]
    pub fn quant_check(&mut self, status: &View) -> R<()> {
        self.mover(g::MoverOp::QuantCheck, status, None, &[], here())
    }

    /// `gt4 DST <- SRC walk=.. dwalk=..` (DSL.MOV.004): whole buffers.
    #[track_caller]
    pub fn gt4(&mut self, dst: &View, src: &View, walk: &Walk, dwalk: &Walk) -> R<()> {
        let at = here();
        if !(src.whole && dst.whole) {
            return fail(
                "DSL.MOV.010",
                format!("a walk gt4 walks whole buffers, at {at}"),
            );
        }
        let kw = [
            ("walk", Kw::Walk(Some(walk))),
            ("at", Kw::Int(None)),
            ("dwalk", Kw::Walk(Some(dwalk))),
            ("dat", Kw::Int(None)),
        ];
        self.mover(g::MoverOp::Gt4, dst, Some(src), &kw, at)
    }

    /// `fill DST value=V [ew=E] [walk=..] [at=..]` (DSL.MOV.005).
    #[track_caller]
    pub fn fill_value(
        &mut self,
        dst: &View,
        value: i64,
        ew: Option<i64>,
        walk: Option<(&Walk, Option<i64>)>,
    ) -> R<()> {
        let at = here();
        if walk.is_some() && !dst.whole {
            return fail(
                "DSL.MOV.010",
                format!("a walk fill walks a whole buffer, at {at}"),
            );
        }
        let kw = [
            ("value", Kw::Int(Some(value))),
            ("ew", Kw::Int(ew)),
            ("walk", Kw::Walk(walk.map(|w| w.0))),
            ("at", Kw::Int(walk.and_then(|w| w.1))),
        ];
        self.mover(g::MoverOp::Fill, dst, None, &kw, at)
    }

    /// `gather DST <- SRC [fp] index=IDX [table_stride=B]` (DSL.MOV.007):
    /// `table_stride` bytes between the table's rows.
    #[track_caller]
    pub fn gather(
        &mut self,
        dst: &View,
        src: &View,
        index: &View,
        fp: bool,
        table_stride: Option<i64>,
    ) -> R<()> {
        let at = here();
        let kw = [
            ("index", Kw::Index(index)),
            ("fp", Kw::Flag(fp)),
            ("table_stride", Kw::Int(table_stride)),
        ];
        self.mover(g::MoverOp::Gather, dst, Some(src), &kw, at)
    }

    /// `scatter DST <- SRC [fp] index=IDX [table_stride=B]` (DSL.MOV.007).
    #[track_caller]
    pub fn scatter(
        &mut self,
        dst: &View,
        src: &View,
        index: &View,
        fp: bool,
        table_stride: Option<i64>,
    ) -> R<()> {
        let at = here();
        let kw = [
            ("index", Kw::Index(index)),
            ("fp", Kw::Flag(fp)),
            ("table_stride", Kw::Int(table_stride)),
        ];
        self.mover(g::MoverOp::Scatter, dst, Some(src), &kw, at)
    }
}
