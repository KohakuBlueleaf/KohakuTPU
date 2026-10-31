//! Partition a padded view at entry and packed-layout boundaries, not DRAM
//! (`ktpu_machine::packing`).

use super::*;

pub(super) struct Piece {
    pub dax: Vec<M::Ax>,
    pub soff: i64,
    pub rows: i64,
    pub words: i64,
}

/// Bytes a deferred status of this quant takes on `cores` cores.
pub(super) fn status_bytes(
    db: &Buffer,
    dax: &[M::Ax],
    sax: &[M::Ax],
    padded: bool,
    cores: i64,
) -> i64 {
    ktpu_machine::packing::quant_status_bytes(
        cores,
        padded,
        (dax[0].lo, dax[0].size, sax[0].size),
        (dax[1].lo, dax[1].size, sax[1].size),
        db.get(if db.dtype == "mx7a" { "gm" } else { "gn" }),
        db.get("nk"),
    )
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
    ktpu_machine::packing::pieces(
        (dax[0].lo, dax[0].size, sax[0].size),
        (dax[1].lo, dax[1].size, sax[1].size),
        g,
        nk,
    )
    .into_iter()
    .map(|(r, c)| Piece {
        dax: vec![
            M::Ax {
                lo: dax[0].lo + r.at,
                size: r.n,
                kept: Some(true),
            },
            M::Ax {
                lo: dax[1].lo + c.at,
                size: c.n,
                kept: Some(true),
            },
        ],
        // Empty pieces never read the source or advertise a read span.
        soff: if r.live == 0 || c.live == 0 {
            soff
        } else {
            soff + r.at * pitch + 2 * c.at
        },
        rows: if c.live == 0 { 0 } else { r.live },
        words: c.live / 16,
    })
    .collect()
}
