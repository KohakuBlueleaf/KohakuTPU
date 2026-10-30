//! How a padded MX7 quantisation splits into pieces (L2.MOV.010): cuts at
//! entries and layout blocks, only where the source's valid extent ends.

/// One run of a destination axis: its start past the axis's first index,
/// its length and the valid source entries it holds (`0`: zero padding).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Run {
    pub at: i64,
    pub n: i64,
    pub live: i64,
}

/// An axis's runs (`valid` of `size` from the source): each wholly inside
/// one `block` or spanning whole blocks, cut at `entry`s.
pub fn runs(start: i64, size: i64, valid: i64, entry: i64, block: i64) -> Vec<Run> {
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
        out.push(Run { at, n, live });
        at += n;
    }
    out
}

/// Every row run with every column run (`(start, size, valid)` each) of a
/// destination of `groups` lane groups and `nk` K blocks.
pub fn pieces(
    rows: (i64, i64, i64),
    cols: (i64, i64, i64),
    groups: i64,
    nk: i64,
) -> Vec<(Run, Run)> {
    let rr = runs(rows.0, rows.1, rows.2, 4, 4 * groups);
    let cc = runs(cols.0, cols.1, cols.2, 32, 32 * nk);
    rr.iter()
        .flat_map(|r| cc.iter().map(move |c| (*r, *c)))
        .collect()
}

/// Bytes of a deferred quant's finite status (L2.MOV.010): a 128-byte
/// record for each of `cores` cores of each piece holding source entries.
pub fn status_bytes(cores: i64, pieces: &[(Run, Run)]) -> i64 {
    let live = pieces
        .iter()
        .filter(|(r, c)| r.live != 0 && c.live != 0)
        .count() as i64;
    STATUS_RECORD * cores * live
}

/// Bytes of a quant's deferred status on `cores` cores: [`status_bytes`] of
/// its [`pieces`] when padded, else one piece's records.
pub fn quant_status_bytes(
    cores: i64,
    padded: bool,
    rows: (i64, i64, i64),
    cols: (i64, i64, i64),
    groups: i64,
    nk: i64,
) -> i64 {
    if padded {
        status_bytes(cores, &pieces(rows, cols, groups, nk))
    } else {
        STATUS_RECORD * cores
    }
}

/// Bytes of one core's finite status record.
pub const STATUS_RECORD: i64 = 128;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_k_tail_cuts_where_the_source_ends() {
        let run = |at, n, live| Run { at, n, live };
        assert_eq!(
            runs(0, 128, 96, 32, 64),
            vec![run(0, 64, 32), run(64, 32, 32), run(96, 32, 0)]
        );
        assert_eq!(runs(0, 128, 128, 32, 64), vec![run(0, 128, 32)]);
        assert_eq!(pieces((0, 64, 40), (0, 128, 96), 16, 2).len(), 2 * 3);
    }

    #[test]
    fn a_status_holds_a_record_a_core_of_each_live_piece() {
        // Rows: 40 live then 24 padding; columns: two live runs, one padding.
        let p = pieces((0, 64, 40), (0, 128, 96), 16, 2);
        assert_eq!(status_bytes(4, &p), 128 * 4 * 2);
        assert_eq!(
            status_bytes(4, &pieces((0, 64, 64), (0, 128, 128), 16, 2)),
            128 * 4
        );
    }
}
