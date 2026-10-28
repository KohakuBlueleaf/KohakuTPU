//! Concatenate bound straight-line bodies, retaining completion barriers.
use crate::{Op, Package, Step};

pub fn bound(parts: &[Package]) -> Result<Package, String> {
    let first = parts.first().ok_or("empty link")?;
    let mut out = first.clone();
    out.steps.clear();
    out.payloads.clear();
    for part in parts {
        if !part.buffers.is_empty() || !part.relocs.is_empty() {
            return Err("link requires bound packages".into());
        }
        if part.units != first.units
            || part.signature != first.signature
            || part.ack_reserve != first.ack_reserve
        {
            return Err("link unit topology or completion reserve differs".into());
        }
        let offset = out.payloads.len() as u64;
        for step in &part.steps {
            let mut step = *step;
            match step.op {
                Op::End => break,
                Op::Dispatch | Op::Mover => {
                    step.arg = step
                        .arg
                        .checked_add(offset)
                        .ok_or("payload offset overflow")?
                }
                Op::Repeat => {
                    let start = (step.arg & 0xffff_ffff)
                        .checked_add(offset)
                        .ok_or("repeat offset overflow")?;
                    if start > u32::MAX as u64 {
                        return Err("repeat offset exceeds field".into());
                    }
                    step.arg = (step.arg & !0xffff_ffff) | start;
                }
                Op::Await | Op::Barrier | Op::Mwait => (),
                _ => return Err(format!("unsupported linked step {:?}", step.op)),
            }
            out.steps.push(step);
        }
        out.steps.push(Step::new(Op::Barrier, 0, 0, 0, 0));
        out.payloads.extend_from_slice(&part.payloads);
    }
    out.steps.push(Step::new(Op::End, 0, 0, 0, 0));
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Word;

    #[test]
    fn offsets_and_boundaries_survive_linking() {
        let p = Package {
            payloads: vec![Word::default(); 3],
            steps: vec![
                Step::new(Op::Mover, 0, 2, 1, 0),
                Step::new(Op::End, 0, 0, 0, 0),
            ],
            ..Default::default()
        };
        let linked = bound(&[p.clone(), p]).unwrap();
        assert_eq!(linked.steps[0].arg, 1);
        assert_eq!(linked.steps[2].arg, 4);
        assert_eq!(linked.steps[1].op, Op::Barrier);
        assert_eq!(linked.payloads.len(), 6);
        let bad = Package {
            steps: vec![Step::new(Op::Jump, 0, 0, 0, 0)],
            ..Default::default()
        };
        assert!(bound(&[bad]).is_err());
    }
}
