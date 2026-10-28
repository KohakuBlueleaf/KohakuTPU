//! A `kp` op log replayed, one entry at a time.

use super::*;

// ------------------------------------------------------------- replay
/// The outcome of one `kp` entry replayed: the steps it took, the tensor
/// it made.
pub struct Replayed {
    pub steps: Vec<Step>,
    pub out: Option<usize>,
}

impl Session<'_> {
    /// One `kp` log entry (`plan_dump` `_api`): `names` maps the log's
    /// tensor names to this session's tensors.
    pub fn replay(
        &mut self,
        op: &str,
        args: &[Value],
        names: &HashMap<String, usize>,
    ) -> Result<Replayed, (Refusal, Vec<Step>)> {
        let start = self.r.x.steps.len();
        let t = |v: &Value| -> usize { names[v.as_str().expect("a tensor name")] };
        let num = |v: &Value| -> Arg {
            match v {
                Value::String(s) => Arg::T(names[s]),
                Value::Bool(b) => Arg::N(if *b { 1.0 } else { 0.0 }),
                _ => Arg::N(v.as_f64().expect("a number")),
            }
        };
        let got: Result<Option<usize>, Refusal> = match op {
            "set_fusion" => {
                self.set_fusion(args[0].as_bool().unwrap());
                Ok(None)
            }
            "tensor" => {
                let a = &args[0]["array"];
                let shape: Vec<i64> = a["shape"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|d| d.as_i64().unwrap())
                    .collect();
                let h = &args[0]["host"];
                let host = Digest {
                    bytes: h["bytes"].as_i64().unwrap(),
                    sha256: h["sha256"].as_str().unwrap().into(),
                };
                self.tensor(&shape, a["dtype"].as_str().unwrap(), host)
                    .map(Some)
            }
            "ew" => {
                let xs: Vec<Arg> = args[1..].iter().map(num).collect();
                self.ew(args[0].as_str().unwrap(), &xs).map(Some)
            }
            "reduce" => self
                .reduce(
                    args[0].as_str().unwrap(),
                    t(&args[1]),
                    args[2].as_i64(),
                    args[3].as_bool().unwrap(),
                )
                .map(Some),
            "matmul" => self.matmul(t(&args[0]), t(&args[1])).map(Some),
            "realize" => self.realize(t(&args[0])).map(|_| Some(t(&args[0]))),
            "numpy" => self.numpy(t(&args[0])).map(|_| None),
            "reshape" => {
                let shape: Vec<i64> = match args.get(1) {
                    Some(Value::Array(a)) => a.iter().map(|d| d.as_i64().unwrap()).collect(),
                    _ => args[1..].iter().map(|d| d.as_i64().unwrap()).collect(),
                };
                self.reshape(t(&args[0]), &shape).map(Some)
            }
            "T" => self.transpose(t(&args[0])).map(Some),
            other => unsupported("API.TEN.002", format!("no kp op {other}")),
        };
        let compiled = crate::program::compile(&mut self.r.x.steps[start..], self.r.m);
        let got = got.and_then(|out| compiled.map(|_| out));
        let steps = self.r.x.steps[start..].to_vec();
        match got {
            Ok(out) => Ok(Replayed { steps, out }),
            Err(e) => Err((e, steps)),
        }
    }
}
