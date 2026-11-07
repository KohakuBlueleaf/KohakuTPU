//! Executable shares of the four-MG, two-VC flash schedule.

use std::ops::Range;

use crate::trace::{fail, R};

/// The reference packing has eight 128-query tiles and 1024 keys per head.
pub const TEMPLATE: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../../language/kohakutpu/language/examples/l1/fused/flash.ktpu"
));

/// Byte origins in the shared allocations, independent of local ring indices.
#[derive(Clone, Debug, Default)]
pub struct Origins {
    pub q: i64,
    pub k: i64,
    pub v: i64,
    pub y: i64,
    pub s: i64,
    pub o: i64,
    pub li: i64,
}

#[derive(Clone, Debug)]
pub struct Share {
    pub heads: Range<i64>,
    pub query_tiles: Range<i64>,
    pub origins: Origins,
}

impl Share {
    /// Preserve the local stream indices; map only external addresses to global
    /// heads and queries. Rings and ping-pong banks always start at local zero.
    pub(super) fn validate(&self) -> R<()> {
        let h = &self.heads;
        let q = &self.query_tiles;
        if h.start < 0
            || h.end > 10
            || h.start >= h.end
            || q.start < 0
            || q.end > 8
            || q.start >= q.end
        {
            return fail("DSL.KERN.020", "flash share outside the reference packing");
        }
        let origins = &self.origins;
        if [
            origins.q, origins.k, origins.v, origins.y, origins.s, origins.o, origins.li,
        ]
        .iter()
        .any(|x| *x < 0 || *x % 64 != 0 || *x > i64::MAX - 2 * 1024 * 1024)
        {
            return fail("DSL.KERN.020", "flash origins must be aligned byte offsets");
        }
        Ok(())
    }

    pub fn text(&self) -> R<String> {
        self.validate()?;
        let h = &self.heads;
        let q = &self.query_tiles;
        let nq = q.end - q.start;
        let per_head = 4 * nq;
        let nt = (h.end - h.start) * per_head;
        let mut text = TEMPLATE.to_string();
        // Substitute expression sites, never numeric tokens in instruction images.
        for (from, to) in [
            ("when T % 8 < 2", format!("when T % {nq} < 2")),
            ("(T/8)*64K", format!("(T/{nq})*64K")),
            ("q + T*8K", format!("q + (T/{nq})*64K + (T % {nq})*8K")),
            ("t % 32", format!("t % {per_head}")),
            ("t/32", format!("t/{per_head}")),
            (
                "y + (t - 5)*4K",
                format!("y + ((t - 5)/{per_head})*128K + ((t - 5) % {per_head})*4K"),
            ),
            (
                "y + t*4K",
                format!("y + (t/{per_head})*128K + (t % {per_head})*4K"),
            ),
            ("for T in 0..4", format!("for T in 0..{}", (nt / 4).min(4))),
            ("for t in 0..321", format!("for t in 0..{}", nt + 1)),
            ("after(t, 320)", format!("after(t, {nt})")),
            ("runp(t, 320)", format!("runp(t, {nt})")),
            ("when t < 320", format!("when t < {nt}")),
            (
                "for t in 315..320",
                format!("for t in {}..{nt}", (nt - 5).max(0)),
            ),
        ] {
            text = text.replace(from, &to);
        }
        Ok(self.rebase(&text))
    }

    pub(super) fn rebase(&self, text: &str) -> String {
        let h = &self.heads;
        let q = &self.query_tiles;
        let origins = &self.origins;
        let offsets = [
            ("q", origins.q + h.start * 65536 + q.start * 8192),
            ("k", origins.k + h.start * 65536),
            ("v", origins.v + h.start * 65536),
            ("y", origins.y + h.start * 131072 + q.start * 16384),
            ("s", origins.s),
            ("o", origins.o),
            ("li", origins.li),
        ];
        // Buffer references are expressions; declarations and image arguments are
        // lexical bindings and retain their names.
        let mut result = String::new();
        for line in text.lines() {
            let mut line = line.to_string();
            if !line.trim_start().starts_with('#')
                && !line.starts_with("fn ")
                && !line.starts_with("image ")
                && !line.starts_with("macro ")
            {
                for (name, offset) in offsets {
                    if offset != 0 {
                        line =
                            line.replace(&format!("{name} + "), &format!("{name} + {offset} + "));
                    }
                }
            }
            result.push_str(&line);
            result.push('\n');
        }
        result
    }
}
