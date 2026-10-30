//! Measured, per-recipe alternatives (`runtime/costs.py`): a deployment's
//! cost profile (`KTPU_COST_PROFILE`) admits an alternative where it was
//! measured cheaper and verified, under the schedule the environment runs
//! now; with no profile every recipe keeps its original path.

use std::fmt;

use serde::de::{Deserializer, MapAccess, Visitor};
use serde::Deserialize;
use serde_json::{json, Value};

use super::pending::Step;

/// The admitted records, in the profile's order: encoded key, recipe, the
/// alternative's cost.
type Admitted = Vec<(String, Value, f64)>;

/// A JSON object's entries in file order.
struct Ordered(Vec<(String, Value)>);

impl<'de> Deserialize<'de> for Ordered {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Ordered;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("an object")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut m: A) -> Result<Ordered, A::Error> {
                let mut out = Vec::new();
                while let Some(e) = m.next_entry()? {
                    out.push(e);
                }
                Ok(Ordered(out))
            }
        }
        d.deserialize_map(V)
    }
}

#[derive(Deserialize)]
struct Profile {
    schedule: Value,
    recipes: Ordered,
}

fn flag(name: &str, default: &str) -> bool {
    std::env::var(name).unwrap_or_else(|_| default.into()) == "1"
}

/// The schedule the environment runs (`costs._records`' `schedule`).
fn schedule() -> Value {
    let mut s = serde_json::Map::new();
    let batch = std::env::var("KTPU_PIPE_FINITE_BATCH").unwrap_or_else(|_| "4".into());
    s.insert("finite_batch".into(), json!(batch.parse::<i64>().unwrap()));
    s.insert("overlap".into(), json!(flag("KTPU_PIPE_OVERLAP", "1")));
    for (key, env, default) in [
        ("classifier_overlap", "KTPU_QUANT_CLASSIFIER_OVERLAP", "0"),
        ("pipeline", "KTPU_QUANT_PIPELINE", "1"),
        ("zero_classifier", "KTPU_QUANT_ZERO_CLASSIFIER", "1"),
        ("stream_axes", "KTPU_QUANT_STREAM_AXES", "1"),
        ("fill_overlap", "KTPU_QUANT_FILL_OVERLAP", "1"),
        ("pull_tree", "KTPU_PULL_TREE", "1"),
        ("zero_reuse", "KTPU_QUANT_ZERO_REUSE", "1"),
        ("class_rows_direct", "KTPU_CLASS_ROWS_DIRECT", "0"),
        ("class_rows_route", "KTPU_CLASS_ROWS_ROUTE", "0"),
    ] {
        if flag(env, default) {
            s.insert(key.into(), json!(true));
        }
    }
    let band = std::env::var("KTPU_CLASS_BAND").unwrap_or_else(|_| "4".into());
    let band: i64 = band.parse().unwrap();
    if band != 1 {
        s.insert("class_band".into(), json!(band));
    }
    Value::Object(s)
}

/// The admitted records of `kind` on `board`: none without a profile or
/// when the profile was measured under another schedule.
fn records(kind: &str, board: &str) -> Admitted {
    let Ok(path) = std::env::var("KTPU_COST_PROFILE") else {
        return Admitted::new();
    };
    let text = std::fs::read_to_string(&path).expect("a readable cost profile");
    let doc: Profile = serde_json::from_str(&text).expect("a cost profile");
    if doc.schedule != schedule() {
        return Admitted::new();
    }
    let mut out = Admitted::new();
    for (encoded, record) in doc.recipes.0 {
        let (alt, orig) = (record["alternative"].as_f64(), record["original"].as_f64());
        if record["verified"] != json!(true) || alt.unwrap() >= orig.unwrap() {
            continue;
        }
        let pair: Value = serde_json::from_str(&encoded).expect("an encoded recipe");
        if pair[0] == kind && pair[1]["board"] == board {
            out.push((encoded, pair[1]["recipe"].clone(), alt.unwrap()));
        }
    }
    out
}

/// `costs.key`: `[kind, recipe]` as compact JSON, keys sorted.
pub fn key(kind: &str, recipe: &Value) -> String {
    serde_json::to_string(&json!([kind, recipe])).unwrap()
}

/// `costs.choose`: whether `recipe`'s alternative is admitted.
pub fn choose(kind: &str, recipe: &Value, board: &str) -> bool {
    let k = key(kind, &json!({"board": board, "recipe": recipe}));
    records(kind, board).iter().any(|(e, _, _)| *e == k)
}

/// `costs.steps_recipe`.
pub fn steps_recipe(steps: &[Step]) -> Value {
    Value::Array(
        steps
            .iter()
            .map(|s| {
                json!({"n": s.n, "walk": s.walk, "at": s.at, "dwalk": s.dwalk, "dat": s.dat, "m": s.m})
            })
            .collect(),
    )
}

/// `costs.packing_map`: a step's logical (row, column) map into a packing
/// source of `pitch` from `offset`; None when a level changes length or the
/// columns pass the row.
fn packing_map(step: &Value, src: &Value, cols: i64) -> Option<Value> {
    let pitch = src["pitch"].as_i64().unwrap();
    let dat = step["dat"].as_i64().unwrap() - src["offset"].as_i64().unwrap();
    let (row0, col0) = (dat.div_euclid(pitch), dat.rem_euclid(pitch));
    let mut axes = Vec::new();
    let mut reach = col0;
    let levels = step["walk"].as_array().unwrap();
    for (w, d) in levels.iter().zip(step["dwalk"].as_array().unwrap()) {
        let (n, s, nd, dd) = (
            w[0].as_i64()?,
            w[1].as_i64()?,
            d[0].as_i64()?,
            d[1].as_i64()?,
        );
        if n != nd {
            return None;
        }
        let (r, c) = (dd.div_euclid(pitch), dd.rem_euclid(pitch));
        reach += (n - 1) * c;
        axes.push(json!([n, s, r, c]));
    }
    if reach >= cols.min(pitch) {
        return None;
    }
    Some(json!([step["n"], step["at"], row0, col0, axes]))
}

/// `costs.prepared_quant`: the cheapest measured layout-equivalent packing
/// read of `steps` (its last step's destination pitch an intermediate
/// choice: the logical map compared), None when none is.
#[allow(clippy::too_many_arguments)]
pub fn prepared_quant(
    steps: &[Step],
    (pitch, offset): (i64, i64),
    (rows, cols, rp, cp): (i64, i64, i64, i64),
    side: &str,
    (g, nk, batches): (i64, i64, i64),
    board: &str,
) -> Option<Value> {
    let requested = steps_recipe(steps);
    let req = requested.as_array().unwrap();
    let wanted = packing_map(
        req.last().unwrap(),
        &json!({"pitch": pitch, "offset": offset}),
        cols,
    );
    let matched = json!({"rows": rows, "rp": rp, "cp": cp, "side": side, "g": g, "nk": nk, "batches": batches});
    let mut best: Option<(f64, Value)> = None;
    for (_, recipe, cost) in records("prepared_quant", board) {
        let q = &recipe["quant"];
        let got = recipe["steps"].as_array().unwrap();
        if got[..got.len() - 1] == req[..req.len() - 1]
            && matched.as_object().unwrap().iter().all(|(k, v)| q[k] == *v)
            && wanted.is_some()
            && packing_map(got.last().unwrap(), &q["src"], q["cols"].as_i64().unwrap()) == wanted
            && best.as_ref().is_none_or(|(c, _)| cost < *c)
        {
            best = Some((cost, recipe));
        }
    }
    best.map(|(_, r)| r)
}

/// `costs.has_steps`: whether a measured winning consumer takes `steps`.
pub fn has_steps(steps: &[Step], board: &str) -> bool {
    let wanted = steps_recipe(steps);
    records("prepared_quant", board)
        .iter()
        .any(|(_, recipe, _)| recipe["steps"] == wanted)
}
