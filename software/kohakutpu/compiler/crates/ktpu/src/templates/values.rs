//! Host arithmetic for launch values, separate from the affine patch sites.

use std::collections::BTreeMap;

use ktpu_l0::format::template::Params;
use serde::{Deserialize, Serialize};

/// A checked, compiler-free expression over the original shape parameters.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum Expr {
    Param { name: String },
    Const { value: i64 },
    Add { a: Box<Expr>, b: Box<Expr> },
    Mul { a: Box<Expr>, b: Box<Expr> },
    Min { a: Box<Expr>, b: Box<Expr> },
    Div { a: Box<Expr>, divisor: i64 },
    Quotient { a: Box<Expr>, b: Box<Expr> },
}

pub type Recipes = BTreeMap<String, Expr>;

impl Expr {
    pub fn dependencies(&self) -> std::collections::BTreeSet<String> {
        match self {
            Self::Param { name } => [name.clone()].into(),
            Self::Const { .. } => Default::default(),
            Self::Div { a, .. } => a.dependencies(),
            Self::Add { a, b }
            | Self::Mul { a, b }
            | Self::Min { a, b }
            | Self::Quotient { a, b } => {
                a.dependencies().union(&b.dependencies()).cloned().collect()
            }
        }
    }
    fn eval(&self, params: &Params, depth: usize) -> Result<i64, String> {
        if depth > 32 {
            return Err("launch value expression exceeds 32 levels".into());
        }
        let eval = |x: &Expr| x.eval(params, depth + 1);
        let value = match self {
            Self::Param { name } => {
                return params
                    .get(name)
                    .copied()
                    .ok_or_else(|| format!("missing shape parameter {name}"))
            }
            Self::Const { value } => Some(*value),
            Self::Add { a, b } => eval(a)?.checked_add(eval(b)?),
            Self::Mul { a, b } => eval(a)?.checked_mul(eval(b)?),
            Self::Min { a, b } => Some(eval(a)?.min(eval(b)?)),
            Self::Quotient { a, b } => {
                let divisor = eval(b)?;
                if divisor <= 0 {
                    return Err("launch value divisor must be positive".into());
                }
                Some(eval(a)?.div_euclid(divisor))
            }
            Self::Div { a, divisor } if *divisor > 0 => Some(eval(a)?.div_euclid(*divisor)),
            Self::Div { .. } => return Err("launch value divisor must be positive".into()),
        };
        value.ok_or_else(|| "launch value arithmetic overflow".into())
    }
}

/// Derivations only reference shape parameters, never other derived names.
pub fn bind(recipes: &Recipes, shape: &Params) -> Result<Params, String> {
    let mut result = shape.clone();
    for (name, expression) in recipes {
        if shape.contains_key(name) {
            return Err(format!("derived name shadows shape parameter {name}"));
        }
        result.insert(name.clone(), expression.eval(shape, 0)?);
    }
    Ok(result)
}

/// Fit one varying coordinate plus a constant, checked against every witness.
/// Correlated coordinates are accepted only when their predicted field agrees
/// at every supplied shape witness; callers still prove all real launches.
pub(super) fn fit(points: &[(Params, i128)]) -> Result<(i64, Params), String> {
    let (origin, y0) = points.first().ok_or("empty value witnesses")?;
    if points.iter().all(|(_, y)| y == y0) {
        return Ok((
            i64::try_from(*y0).map_err(|_| "constant exceeds i64")?,
            Params::new(),
        ));
    }
    for (name, x0) in origin {
        let Some((other, y1)) = points.iter().find(|(p, _)| p[name] != *x0) else {
            continue;
        };
        let dx = other[name] as i128 - *x0 as i128;
        let dy = y1.checked_sub(*y0).ok_or("value fit overflow")?;
        if dy % dx != 0 {
            continue;
        }
        let coefficient = dy / dx;
        let constant = y0
            .checked_sub(
                coefficient
                    .checked_mul(*x0 as i128)
                    .ok_or("value fit overflow")?,
            )
            .ok_or("value fit overflow")?;
        if points.iter().all(|(p, y)| {
            coefficient
                .checked_mul(p[name] as i128)
                .and_then(|v| v.checked_add(constant))
                == Some(*y)
        }) {
            return Ok((
                i64::try_from(constant).map_err(|_| "constant exceeds i64")?,
                [(
                    name.clone(),
                    i64::try_from(coefficient).map_err(|_| "coefficient exceeds i64")?,
                )]
                .into(),
            ));
        }
    }
    super::linear::fit(points)
}
