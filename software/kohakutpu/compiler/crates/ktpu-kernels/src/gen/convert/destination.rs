//! Complete physical words for linear destination mover walks.

use super::*;
use crate::gen::classes::{moves, ShapeClass};
use crate::split::{PlannedKernel, Region, SplitAxis, SplitRequest};

/// Number of words when every logical step has the same physical stride.
fn linear(levels: &Walk, word: i64) -> Option<i64> {
    let mut count = 1i64;
    for &(n, stride) in levels.iter().rev() {
        if n <= 0 || n > 1 && stride != count.checked_mul(word)? {
            return None;
        }
        count = count.checked_mul(n)?;
    }
    Some(count)
}

/// Normalize paired axes when destination words have one physical stride.
fn dense_pair(
    levels: &Walk,
    at: i64,
    dlevels: &Walk,
    dat: i64,
    word: i64,
) -> Option<(i64, i64, i64, Vec<(i64, i64)>, i64)> {
    let step = dlevels
        .iter()
        .rev()
        .find(|level| level.0 > 1)
        .map_or(Some(word), |level| level.1.checked_abs())?;
    if step < word || step % word != 0 {
        return None;
    }
    if let Some(count) = linear(dlevels, step) {
        return Some((count, dat, at, levels.to_vec(), step));
    }
    if let Some(count) = linear(dlevels, -step) {
        let source = levels.iter().try_fold(at, |origin, &(n, stride)| {
            origin.checked_add(n.checked_sub(1)?.checked_mul(stride)?)
        })?;
        let levels = levels
            .iter()
            .map(|&(n, stride)| Some((n, stride.checked_neg()?)))
            .collect::<Option<Vec<_>>>()?;
        let destination = dat.checked_sub((count - 1).checked_mul(step)?)?;
        return Some((count, destination, source, levels, step));
    }
    if levels.len() != dlevels.len() || levels.iter().zip(dlevels).any(|(s, d)| s.0 != d.0) {
        return None;
    }
    let mut source = at;
    let mut destination = dat;
    let mut axes = Vec::new();
    for (&(n, mut ss), &(_, mut ds)) in levels.iter().zip(dlevels) {
        if n <= 0 {
            return None;
        }
        if ds < 0 {
            source = source.checked_add((n - 1).checked_mul(ss)?)?;
            destination = destination.checked_add((n - 1).checked_mul(ds)?)?;
            ss = ss.checked_neg()?;
            ds = ds.checked_neg()?;
        }
        axes.push((n, ss, ds));
    }
    axes.sort_by_key(|axis| std::cmp::Reverse(axis.2));
    let destination_levels: Vec<_> = axes.iter().map(|&(n, _, ds)| (n, ds)).collect();
    let step = destination_levels
        .iter()
        .rev()
        .find(|level| level.0 > 1)
        .map_or(word, |level| level.1);
    if step < word || step % word != 0 {
        return None;
    }
    let count = linear(&destination_levels, step)?;
    Some((
        count,
        destination,
        source,
        axes.iter().map(|&(n, ss, _)| (n, ss)).collect(),
        step,
    ))
}

fn options(
    count: i64,
    dat: i64,
    stride: i64,
    source: Option<i64>,
    request: &SplitRequest,
    m: &Machine,
) -> R<Vec<crate::split::SplitOption>> {
    let hw = Hw::of(m);
    let shape = vector::split::Shape {
        rows: count,
        quantum: 1,
        reads: vec![],
        writes: vec![vector::split::Tensor {
            name: "dst".into(),
            operand: vector::Operand {
                kind: vector::Kind::Full,
                dtype: g::Dtype::F16,
                offset: dat / 2,
                pitch: stride / 2,
                rows: count,
            },
            width: hw.lanes,
            planes: vec![0],
        }],
        tokens: false,
    };
    let mut options = vector::split::split_options(&shape, request, m)?;
    for option in &mut options {
        option.id = "destination-tiles".into();
        option.axis = SplitAxis::DestinationTiles;
        option.legality.clauses = vec![
            "complete physical destination words; line-safe cuts; global walked source".into(),
        ];
        if let Some(bytes) = source {
            for share in &mut option.shards {
                if share.valid_extent[0] != 0 {
                    share.read_regions.push(Region {
                        allocation: "src".into(),
                        bytes: 0..bytes as u64,
                    });
                }
            }
        }
    }
    Ok(options)
}

#[allow(clippy::too_many_arguments)]
pub fn walk(
    src_bytes: i64,
    levels: &Walk,
    at: i64,
    dst_bytes: i64,
    dlevels: &Walk,
    dat: i64,
    class: Option<&ShapeClass>,
    request: &SplitRequest,
    m: &Machine,
) -> R<Option<PlannedKernel>> {
    let hw = Hw::of(m);
    let Some((count, destination_origin, source_origin, source_levels, stride)) =
        dense_pair(levels, at, dlevels, dat, hw.word_bytes)
    else {
        return Ok(None);
    };
    let signature = match class {
        Some(class) => {
            moves::walk_kernel(src_bytes, levels, at, dst_bytes, dlevels, dat, class, m)?.kernel
        }
        None => walk_kernel(src_bytes, levels, at, dst_bytes, dlevels, dat, m)?,
    };
    let identity = signature.graph.name.clone();
    let options = options(
        count,
        destination_origin,
        stride,
        Some(words(src_bytes, &hw).0 * hw.word_bytes),
        request,
        m,
    )?;
    let (sw, dw) = (words(src_bytes, &hw).0, words(dst_bytes, &hw).0);
    let (ports, given) = word_ports(&[("src", g::Role::In, sw), ("dst", g::Role::Out, dw)], &hw)?;
    vector::split::kernel_options(options, signature, request, m, |first, size| {
        if first == 0 && size == count {
            return match class {
                Some(class) => Ok(moves::walk_kernel(
                    src_bytes, levels, at, dst_bytes, dlevels, dat, class, m,
                )?
                .kernel),
                None => walk_kernel(src_bytes, levels, at, dst_bytes, dlevels, dat, m),
            };
        }
        let key = name(
            "walk_share",
            vec![Py::str(&identity), Py::Int(first), Py::Int(size)],
        );
        build(&key, m, ports.clone(), given.clone(), |t, b| {
            let pieces = crate::gen::matmul::destination_walk::rectangle(
                &source_levels,
                source_origin,
                0,
                1,
                count,
                first,
                size,
                hw.word_bytes,
            )?;
            for piece in pieces {
                let destination = [(piece.words, stride)];
                let source = (&piece.levels[..], Some(piece.source));
                let target = (
                    &destination[..],
                    Some(destination_origin + (first + piece.destination / hw.word_bytes) * stride),
                );
                if class.is_some() {
                    t.copy_classed(&b[1].whole(), &b[0].whole(), source, target, Some(2))?;
                } else {
                    t.copy(&b[1].whole(), &b[0].whole(), Some(source), Some(target))?;
                }
            }
            Ok(())
        })
    })
    .map(Some)
}

#[allow(clippy::too_many_arguments)]
pub fn fill(
    dst_bytes: i64,
    dlevels: &Walk,
    dat: i64,
    value: i64,
    ew: i64,
    request: &SplitRequest,
    m: &Machine,
) -> R<Option<PlannedKernel>> {
    let hw = Hw::of(m);
    let Some((count, destination_origin, _, _, stride)) =
        dense_pair(dlevels, dat, dlevels, dat, hw.word_bytes)
    else {
        return Ok(None);
    };
    let signature = fill_kernel(dst_bytes, dlevels, dat, value, ew, m)?;
    let identity = signature.graph.name.clone();
    let options = options(count, destination_origin, stride, None, request, m)?;
    let (ports, given) = word_ports(&[("dst", g::Role::Out, words(dst_bytes, &hw).0)], &hw)?;
    vector::split::kernel_options(options, signature, request, m, |first, size| {
        if first == 0 && size == count {
            return fill_kernel(dst_bytes, dlevels, dat, value, ew, m);
        }
        let key = name(
            "fill_share",
            vec![Py::str(&identity), Py::Int(first), Py::Int(size)],
        );
        build(&key, m, ports.clone(), given.clone(), |t, b| {
            t.fill_value(
                &b[0].whole(),
                value,
                Some(ew),
                Some((&[(size, stride)], Some(destination_origin + first * stride))),
            )
        })
    })
    .map(Some)
}
