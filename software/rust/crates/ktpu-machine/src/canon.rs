//! Canonical JSON: keys sorted, no whitespace, non-ASCII escaped; the bytes
//! Python's `json.dumps(doc, sort_keys=True, separators=(",", ":"))` writes.

use serde_json::Value;
use std::fmt::Write;

/// `value` as canonical JSON text.
pub fn canonical(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value);
    out
}

fn write_value(out: &mut String, value: &Value) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => write_str(out, s),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(out, item);
            }
            out.push(']');
        }
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            out.push('{');
            for (i, key) in keys.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_str(out, key);
                out.push(':');
                write_value(out, &map[key]);
            }
            out.push('}');
        }
    }
}

fn write_str(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7f => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    let _ = write!(out, "\\u{:04x}", unit);
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_sorts_keys_and_escapes_like_python() {
        let text = format!(
            "x{}{}\n\u{7f}",
            char::from_u32(0xE9).unwrap(),
            char::from_u32(0x1F600).unwrap()
        );
        let v = serde_json::json!({"b": [1, true, null], "a": text});
        let bs = '\\';
        let want =
            format!("{{\"a\":\"x{bs}u00e9{bs}ud83d{bs}ude00{bs}n\u{7f}\",\"b\":[1,true,null]}}");
        assert_eq!(canonical(&v), want);
    }
}
