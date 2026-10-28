//! Machine-bound tier-0 runner adapters shared by the extension and Rust tests.

use ktpu_machine::Machine;
use serde_json::{json, Value};
use std::collections::BTreeMap;

mod generators;
pub mod hosted;
mod isa_stack;

/// Runners whose outputs are provided by the Rust stack.
pub fn runners() -> Vec<&'static str> {
    let mut out = ktpu_isa::spec::runners();
    out.extend(ktpu_l0::spec::runners());
    out.extend(ktpu_l1::spec::runners());
    out.extend(ktpu_l2::spec::runners());
    out.extend(ktpu_kernels::spec::runners());
    out.extend(generators::RUNNERS);
    out.extend(isa_stack::RUNNERS);
    out.extend([
        "isa.vector.image",
        "isa.verify",
        "isa.vector.links",
        "l0.named",
    ]);
    out.sort_unstable();
    out.dedup();
    out
}

/// Run an adapter, with the mutant active while reading the machine as well.
pub fn run(
    name: &str,
    input: &Value,
    machine: Option<&str>,
    mutant: Option<&str>,
    vector_id: Option<&str>,
    expect: &Value,
) -> Result<Value, String> {
    let stem = vector_id.unwrap_or("").rsplit('/').next().unwrap_or("");
    let (input, expect) = if name.starts_with("l0.") {
        ktpu_l0::spec::ruled(stem, input, expect)
    } else if name.starts_with("l1.") {
        ktpu_l1::spec::ruled(stem, input, expect)
    } else if name.starts_with("l2.") {
        ktpu_l2::spec::ruled(stem, input, expect)
    } else {
        (input.clone(), expect.clone())
    };
    let input = &input;
    let go = || {
        let m = machine
            .map(Machine::named)
            .transpose()
            .map_err(|e| e.to_string())?;
        match name {
            n if generators::RUNNERS.contains(&n) => generators::run(n, input, m.as_ref()),
            n if isa_stack::RUNNERS.contains(&n) => {
                isa_stack::run(n, input, m.as_ref().ok_or("ISA runner needs a machine")?)
            }
            "l2.lanes" | "isa.cluster.plan" | "num.stat.words" => {
                ktpu_kernels::spec::run(name, input, m.as_ref(), None)
            }
            n if n.starts_with("l0.") => hosted::l0(name, input, m.as_ref()),
            n if n.starts_with("l1.") => hosted::l1(name, input, m.as_ref()),
            n if n.starts_with("num.") && ktpu_l1::spec::runners().contains(&n) => {
                ktpu_l1::spec::run(name, input, m.as_ref(), None)
            }
            n if n.starts_with("l2.") => ktpu_l2::spec::run(name, input, m.as_ref(), mutant),
            "isa.vector.links" => {
                let sends = input["sends"]
                    .as_array()
                    .ok_or("no sends")?
                    .iter()
                    .map(|v| v.as_i64().ok_or("a send must be an integer"))
                    .collect::<Result<Vec<_>, _>>()?;
                ktpu_l2::spec::run_links(&sends, m.as_ref().ok_or("links need a machine")?, mutant)
            }
            "isa.vector.image" => image(input, m.as_ref().ok_or("image needs a machine")?),
            "isa.verify" => verify(input, m.as_ref().ok_or("verify needs a machine")?),
            "isa.vector.params" => params(input, m.as_ref().ok_or("params need a machine")?),
            _ => ktpu_isa::spec::run(name, input, m.as_ref(), None),
        }
    };
    let result = match mutant {
        Some(mu) => crate::mutant::with(mu, go)?,
        None => go(),
    }?;
    Ok(json!({"result": result, "expect": expect}))
}

fn params(inp: &Value, m: &Machine) -> Result<Value, String> {
    let names = inp["names"].as_array().ok_or("no parameter names")?;
    let mut out = serde_json::Map::new();
    for name in names {
        let name = name.as_str().ok_or("a parameter name")?;
        let mu = crate::mutant::active().filter(|mu| ktpu_l2::mutant::NAMES.contains(mu));
        if let Some(value) = ktpu_l2::spec::run_param(name, m, mu)? {
            out.insert(name.to_string(), value);
        } else {
            let value = ktpu_isa::spec::run(
                "isa.vector.params",
                &json!({"names": [name]}),
                Some(m),
                None,
            )?;
            out.extend(value.as_object().ok_or("parameter object")?.clone());
        }
    }
    Ok(Value::Object(out))
}

#[cfg(test)]
mod tests;

fn integer(value: &Value) -> Result<i128, String> {
    value
        .as_i64()
        .map(i128::from)
        .or_else(|| value.as_u64().map(i128::from))
        .ok_or_else(|| format!("expected a JSON integer, got {value}"))
}

fn image(inp: &Value, m: &Machine) -> Result<Value, String> {
    let text = inp["text"].as_str().ok_or("no text")?;
    let name = inp["name"].as_str().ok_or("no name")?;
    let entry = if inp["entry"] == "reset" {
        ktpu_l1::lower::image::reset(m.vc.vlmax)
    } else {
        ktpu_l1::lower::image::UNKNOWN
    };
    let args: Vec<i128> = inp["args"]
        .as_array()
        .into_iter()
        .flatten()
        .map(integer)
        .collect::<Result<_, _>>()?;
    let got = ktpu_l1::text::read(text, "<isa.vector.image>")
        .and_then(|module| ktpu_l1::lower::image::image(&module, m, name, &args, entry));
    let Ok((words, descs)) = got else {
        return Ok(json!({"error": "TextError"}));
    };
    let descs: BTreeMap<_, _> = descs
        .into_iter()
        .map(|((ad, field), v)| (format!("{ad},{field}"), format!("{v:#x}")))
        .collect();
    Ok(
        json!({"words": words.iter().map(|w| format!("{w:#x}")).collect::<Vec<_>>(), "descs": descs}),
    )
}

fn verify(inp: &Value, machine: &Machine) -> Result<Value, String> {
    let nodes = inp
        .get("nodes")
        .map(|v| {
            let n = integer(v)?;
            u32::try_from(n).map_err(|_| format!("node count {n} does not fit u32"))
        })
        .transpose()?;
    let m = match nodes {
        Some(n) => machine.with_nodes(n).map_err(|e| e.to_string())?,
        None => machine.clone(),
    };
    let binds: BTreeMap<String, i128> = inp["binds"]
        .as_object()
        .into_iter()
        .flatten()
        .map(|(k, v)| integer(v).map(|n| (k.clone(), n)))
        .collect::<Result<_, _>>()?;
    let text = inp["text"].as_str().ok_or("no text")?;
    let name = inp["fn"].as_str().ok_or("no fn")?;
    let programs = ktpu_l1::text::read(text, "<isa.verify>")
        .and_then(|module| ktpu_l1::build::programs(&module, name, &binds, &m, nodes, None));
    let Ok(programs) = programs else {
        return Ok(json!({"error": "TextError"}));
    };
    let np = programs.first().ok_or("no node program")?;
    let builder = ktpu_l1::build::Builder::new(&m).map_err(|e| e.to_string())?;
    let mut cores = ktpu_l1::lower::imem::Cores::new(&m).map_err(|e| e.to_string())?;
    let opts = ktpu_l1::build::Opts {
        fetch: None,
        cores: Some(&mut cores),
        relocate: false,
    };
    let Ok(built) = builder.build(np, opts) else {
        return Ok(json!({"error": "TextError"}));
    };
    let mut runs = ktpu_verify::Nodes::new();
    runs.insert(np.mesh, built.build(false, false));
    let target = ktpu_verify::Target::from_machine(&m, None)?;
    Ok(ktpu_verify::spec::verify_json(
        &[runs],
        &target,
        inp["l1"].as_bool().unwrap_or(false),
    ))
}
