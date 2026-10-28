//! One modulo trip using the parallel band statement emitters.

use super::emit::typed::Emitter;
use super::*;
use crate::lower::vector::gen::typed::{self as ir, Event};

type Trip = (Vec<Event>, usize, HashSet<String>);

impl Ctx<'_> {
    pub(in crate::lower::vector) fn modulo_typed(mut self) -> VResult<Trip> {
        let mut g = Emitter::default();
        let pre = self.pre.clone();
        let body = self.lp.as_ref().unwrap().body.clone();
        let mut all = pre.clone();
        all.extend(ops_of(&body));
        self.constants_typed(&mut g, &all);
        for n in &pre {
            self.prologue_typed(&mut g, n)?;
        }
        if pre.iter().any(|n| n.op_name() == "unpack") {
            g.fixed(ir::instruction(
                "vsync",
                vec![Term::name("fe")],
                vec![ir::kw("on", Term::name("unpack"))],
            ));
        }
        let head = g.code.len();
        self.iteration_typed(&mut g, &body, 0, false)?;
        let trip = crate::lower::vector::gen::predicate::fuse(g.code.split_off(head));
        g.code.extend(trip);
        Ok((g.code, head, self.pinned))
    }
}
