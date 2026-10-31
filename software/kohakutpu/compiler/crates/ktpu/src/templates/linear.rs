//! Checked fraction-free elimination for declared launch coordinates.

use ktpu_l0::format::template::Params;

fn gcd(mut a: u128, mut b: u128) -> u128 {
    while b != 0 {
        (a, b) = (b, a % b);
    }
    a
}

pub(super) fn fit(points: &[(Params, i128)]) -> Result<(i64, Params), String> {
    let (origin, _) = points.first().ok_or("empty value witnesses")?;
    let names: Vec<_> = origin
        .keys()
        .filter(|n| points.iter().any(|(p, _)| p[*n] != origin[*n]))
        .cloned()
        .collect();
    let width = names.len() + 1;
    let mut matrix: Vec<Vec<i128>> = points
        .iter()
        .map(|(p, y)| {
            let mut row = vec![1];
            row.extend(names.iter().map(|n| p[n] as i128));
            row.push(*y);
            row
        })
        .collect();
    let mut pivots = Vec::new();
    for col in 0..width {
        let rank = pivots.len();
        let Some(pivot) = (rank..matrix.len()).find(|r| matrix[*r][col] != 0) else {
            continue;
        };
        matrix.swap(rank, pivot);
        for row in rank + 1..matrix.len() {
            if matrix[row][col] == 0 {
                continue;
            }
            let common = gcd(
                matrix[rank][col].unsigned_abs(),
                matrix[row][col].unsigned_abs(),
            );
            let common = i128::try_from(common).map_err(|_| "value fit overflow")?;
            let (a, b) = (matrix[rank][col] / common, matrix[row][col] / common);
            let (before, after) = matrix.split_at_mut(row);
            for (cell, pivot) in after[0][col..].iter_mut().zip(&before[rank][col..]) {
                *cell = cell
                    .checked_mul(a)
                    .and_then(|v| pivot.checked_mul(b).and_then(|w| v.checked_sub(w)))
                    .ok_or("value fit overflow")?;
            }
            let factor = matrix[row].iter().fold(0, |g, v| gcd(g, v.unsigned_abs()));
            if factor > 1 {
                let factor = i128::try_from(factor).map_err(|_| "value fit overflow")?;
                for v in &mut matrix[row] {
                    *v /= factor;
                }
            }
        }
        pivots.push(col);
    }
    if matrix[pivots.len()..].iter().any(|r| r[width] != 0) {
        return Err("non-affine in declared launch values".into());
    }
    let mut coefficients = vec![0i128; width];
    for (row, col) in pivots.iter().copied().enumerate().rev() {
        let mut y = matrix[row][width];
        for (j, c) in coefficients.iter().enumerate().skip(col + 1) {
            y = y
                .checked_sub(matrix[row][j].checked_mul(*c).ok_or("value fit overflow")?)
                .ok_or("value fit overflow")?;
        }
        if y % matrix[row][col] != 0 {
            return Err("launch values require noninteger coefficients".into());
        }
        coefficients[col] = y / matrix[row][col];
    }
    for (params, y) in points {
        let predicted = names
            .iter()
            .enumerate()
            .try_fold(coefficients[0], |acc, (i, n)| {
                acc.checked_add(coefficients[i + 1].checked_mul(params[n] as i128)?)
            })
            .ok_or("value fit overflow")?;
        if predicted != *y {
            return Err("non-affine launch witness".into());
        }
    }
    let constant = i64::try_from(coefficients[0]).map_err(|_| "constant exceeds i64")?;
    let result = names
        .into_iter()
        .zip(coefficients.into_iter().skip(1))
        .filter(|(_, c)| *c != 0)
        .map(|(n, c)| Ok((n, i64::try_from(c).map_err(|_| "coefficient exceeds i64")?)))
        .collect::<Result<Params, String>>()?;
    Ok((constant, result))
}
