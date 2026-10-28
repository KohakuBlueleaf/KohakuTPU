//! Partition a padded view at entry and packed-layout boundaries, not DRAM.

use super::*;

pub(super) struct Piece {
    pub dax: Vec<M::Ax>,
    pub soff: i64,
    pub rows: i64,
    pub words: i64,
}

/// Runs wholly inside one layout block or spanning complete blocks. Cut only
/// where the valid extent ends; interiors retain the ordinary batched schedule.
fn runs(start: i64, size: i64, valid: i64, entry: i64, block: i64) -> Vec<(i64, i64, i64)> {
    let mut out = Vec::new();
    let mut at = 0;
    while at < size {
        let left = (valid - at).max(0);
        let live = left.min(entry);
        let extent = if live == entry {
            left / entry * entry
        } else if live == 0 {
            size - at
        } else {
            entry
        };
        let pos = start + at;
        let n = if pos % block != 0 {
            extent.min(block - pos % block)
        } else if extent >= block {
            extent / block * block
        } else {
            extent
        };
        out.push((at, n, live));
        at += n;
    }
    out
}

pub(super) fn pieces(
    db: &Buffer,
    dax: &[M::Ax],
    sax: &[M::Ax],
    soff: i64,
    pitch: i64,
) -> Vec<Piece> {
    let g = db.get(if db.dtype == "mx7a" { "gm" } else { "gn" });
    let nk = db.get("nk");
    let rr = runs(dax[0].lo, dax[0].size, sax[0].size, 4, 4 * g);
    let cc = runs(dax[1].lo, dax[1].size, sax[1].size, 32, 32 * nk);
    let mut out = Vec::new();
    for (r, nr, rows) in rr {
        for &(c, nc, cols) in &cc {
            out.push(Piece {
                dax: vec![
                    M::Ax {
                        lo: dax[0].lo + r,
                        size: nr,
                        kept: Some(true),
                    },
                    M::Ax {
                        lo: dax[1].lo + c,
                        size: nc,
                        kept: Some(true),
                    },
                ],
                // Empty pieces never read the source or advertise a read span.
                soff: if rows == 0 || cols == 0 {
                    soff
                } else {
                    soff + r * pitch + 2 * c
                },
                rows: if cols == 0 { 0 } else { rows },
                words: cols / 16,
            });
        }
    }
    out
}
