//! The leaves of a machine file's facts, each with the next value that keeps
//! the file readable.

use ktpu_machine::Machine;
use serde_json::Value;

/// One leaf: its dotted path and the keys and indices down to it.
pub type Leaf = (String, Vec<String>);

fn walk(node: &Value, path: &str, at: &[String], out: &mut Vec<Leaf>) {
    match node {
        Value::Object(o) => {
            for (k, v) in o {
                let mut at = at.to_vec();
                at.push(k.clone());
                let dotted = if path.is_empty() {
                    k.clone()
                } else {
                    format!("{path}.{k}")
                };
                walk(v, &dotted, &at, out);
            }
        }
        Value::Array(a) if a.first().is_none_or(|v| !v.is_string()) => {
            for (i, v) in a.iter().enumerate() {
                let mut at = at.to_vec();
                at.push(i.to_string());
                walk(v, path, &at, out);
            }
        }
        _ => out.push((path.to_string(), at.to_vec())),
    }
}

/// Every leaf of `doc`'s facts; an array is walked by its index, and a list
/// of names is one leaf.
pub fn leaves(doc: &Value) -> Vec<Leaf> {
    let mut out = Vec::new();
    walk(&doc["machine"], "", &[], &mut out);
    out
}

fn step<'a>(node: &'a Value, key: &str) -> &'a Value {
    match key.parse::<usize>() {
        Ok(i) if node.is_array() => &node[i],
        _ => &node[key],
    }
}

/// The value next to `doc`'s leaf `at`, or None: the checks hold a version and
/// a slot count to 1 or 2 and a backend to its enum; the non-finite policy has
/// one value.
pub fn moved(doc: &Value, at: &[String]) -> Option<Value> {
    let leaf = at.iter().fold(&doc["machine"], |node, key| step(node, key));
    Some(match leaf {
        Value::String(s) if s == "reject_vector" => return None,
        Value::Array(names) if names.is_empty() => return None,
        Value::Array(names) => {
            let mut names = names.clone();
            names.push(names[0].clone());
            Value::from(names)
        }
        Value::Bool(b) => Value::from(!b),
        Value::Number(n) => match n.as_i64()? {
            1 => Value::from(2),
            n => Value::from(if n == 2 { 1 } else { n + 1 }),
        },
        Value::String(s) if s == "vector_mx7" => Value::from("mover_quant"),
        Value::String(s) if s == "mover_quant" => Value::from("vector_mx7"),
        Value::String(s) if s.starts_with("0x") => Value::from(format!("{s}0")),
        Value::String(s) => Value::from(format!("{s}x")),
        _ => return None,
    })
}

pub fn set(doc: &mut Value, at: &[String], to: Value) {
    let mut node = &mut doc["machine"];
    for key in at {
        node = match key.parse::<usize>() {
            Ok(i) if node.is_array() => &mut node[i],
            _ => &mut node[key.as_str()],
        };
    }
    *node = to;
}

/// `doc` with every runtime fact moved.
pub fn runtime_moved(doc: &Value) -> Value {
    let m = Machine::from_json(&doc.to_string()).unwrap();
    let mut next = doc.clone();
    for (path, at) in leaves(doc) {
        if m.get(&path).is_none() {
            if let Some(to) = moved(doc, &at) {
                set(&mut next, &at, to);
            }
        }
    }
    next
}
