//! Slice word walks without enumerating their data-path elements.

use crate::trace::{fail, Buf, Trace, R};
use std::collections::BTreeMap;

#[derive(Clone, Debug)]
pub(crate) struct Piece {
    pub source: i64,
    pub destination: i64,
    pub levels: Vec<(i64, i64)>,
    pub words: i64,
}

/// A contiguous interval in the logical iteration order of a mixed-radix walk.
fn interval(
    levels: &[(i64, i64)],
    first: i64,
    count: i64,
    source: i64,
    destination: i64,
    word: i64,
    out: &mut Vec<Piece>,
) {
    if count == 0 {
        return;
    }
    if levels.is_empty() {
        out.push(Piece {
            source,
            destination,
            levels: vec![(1, word)],
            words: 1,
        });
        return;
    }
    let inner: i64 = levels[1..].iter().map(|level| level.0).product();
    let mut at = first;
    let end = first + count;
    while at < end {
        let index = at / inner;
        let offset = at % inner;
        if offset == 0 && end - at >= inner {
            let n = (end - at) / inner;
            let mut walk = vec![(n, levels[0].1)];
            walk.extend_from_slice(&levels[1..]);
            out.push(Piece {
                source: source + index * levels[0].1,
                destination: destination + (at - first) * word,
                levels: walk,
                words: n * inner,
            });
            at += n * inner;
        } else {
            let n = (inner - offset).min(end - at);
            interval(
                &levels[1..],
                offset,
                n,
                source + index * levels[0].1,
                destination + (at - first) * word,
                word,
                out,
            );
            at += n;
        }
    }
}

/// Gather a rectangular window from a logical row-major word stream.
#[allow(clippy::too_many_arguments)]
pub(crate) fn rectangle(
    levels: &[(i64, i64)],
    at: i64,
    row: i64,
    rows: i64,
    pitch: i64,
    column: i64,
    columns: i64,
    word: i64,
) -> R<Vec<Piece>> {
    let total = levels.iter().try_fold(1i64, |n, &(count, _)| {
        if count <= 0 {
            None
        } else {
            n.checked_mul(count)
        }
    });
    let Some(total) = total else {
        return fail(
            "DSL.MOV.002",
            "a word walk needs positive, representable extents",
        );
    };
    if rows == 0 || columns == 0 {
        return Ok(vec![]);
    }
    let last = (row + rows - 1)
        .checked_mul(pitch)
        .and_then(|n| n.checked_add(column))
        .and_then(|n| n.checked_add(columns));
    if row < 0 || column < 0 || columns > pitch || last.map_or(true, |n| n > total) {
        return fail("DSL.MOV.002", "a gather window exceeds its word walk");
    }
    let mut pieces = Vec::new();
    if column == 0 && columns == pitch {
        interval(levels, row * pitch, rows * pitch, at, 0, word, &mut pieces);
    } else {
        for r in 0..rows {
            interval(
                levels,
                (row + r) * pitch + column,
                columns,
                at,
                r * columns * word,
                word,
                &mut pieces,
            );
        }
    }
    Ok(pieces)
}

/// Coalesce equal-shaped pieces into regular outer walks before tracing moves.
#[allow(clippy::too_many_arguments)]
pub(crate) fn emit(
    t: &mut Trace,
    dst: &Buf,
    src: &Buf,
    pieces: &[Piece],
    columns: i64,
    pitch: i64,
    word: i64,
) -> R<()> {
    let mut groups = BTreeMap::<(Vec<(i64, i64)>, i64), Vec<(i64, i64)>>::new();
    for piece in pieces {
        let mut slices = Vec::new();
        if columns == pitch {
            slices.push(piece.clone());
        } else {
            let mut first = 0;
            while first < piece.words {
                let logical = piece.destination / word + first;
                let count = (piece.words - first).min(columns - logical % columns);
                interval(
                    &piece.levels,
                    first,
                    count,
                    piece.source,
                    (logical / columns * pitch + logical % columns) * word,
                    word,
                    &mut slices,
                );
                first += count;
            }
        }
        for slice in slices {
            groups
                .entry((slice.levels, slice.words))
                .or_default()
                .push((slice.source, slice.destination));
        }
    }
    for ((levels, words), origins) in groups {
        let mut first = 0;
        while first < origins.len() {
            let mut end = first + 1;
            let (ss, ds) = if end < origins.len() {
                (
                    origins[end].0 - origins[first].0,
                    origins[end].1 - origins[first].1,
                )
            } else {
                (0, 0)
            };
            while end < origins.len()
                && origins[end].0 - origins[end - 1].0 == ss
                && origins[end].1 - origins[end - 1].1 == ds
            {
                end += 1;
            }
            let count = (end - first) as i64;
            let mut source = Vec::new();
            let mut destination = Vec::new();
            if count > 1 {
                source.push((count, ss));
                destination.push((count, ds));
            }
            source.extend_from_slice(&levels);
            destination.push((words, word));
            t.copy(
                &dst.whole(),
                &src.whole(),
                Some((&source, Some(origins[first].0))),
                Some((&destination, Some(origins[first].1))),
            )?;
            first = end;
        }
    }
    Ok(())
}
