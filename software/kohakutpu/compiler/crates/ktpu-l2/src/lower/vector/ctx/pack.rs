//! A band's packs into the buffers its stores and sends drain (L2.OP.025,
//! .026, L2.STAT.003, .008).

use super::*;

mod typed;

impl<'a> Ctx<'a> {
    pub(in crate::lower::vector::ctx) fn store_dests(
        &self,
        ops: &[Arc<Node>],
        target: &str,
        with_send: bool,
    ) -> Vec<usize> {
        let mut seen: IndexMap<Vec<i64>, usize> = IndexMap::new();
        for n in ops {
            let s = op(n);
            let pos = s.st.positional();
            let hit = (s.op == "store" && pos.get(2).and_then(|p| p.as_name()) == Some(target))
                || (with_send
                    && s.op == "send"
                    && pos.first().and_then(|p| p.as_name()) == Some(target));
            if hit {
                let d = self.of[&s.uid];
                // One pack a buffer: a later store of the same buffers wins the slot.
                seen.insert(self.descs[d].buf.clone(), d);
            }
        }
        seen.into_values().collect()
    }
}
