//! Explicit template policy: every rotation operand is a bind-time parameter.
use ktpu_l1::text::{Module, Stmt, Term};

pub fn rotations(module: &mut Module) {
    fn term(t: &mut Term, next: &mut usize) {
        if let Term::Call(name, args) = t {
            if name == "rot" && args.len() == 2 {
                let value = args[1].clone();
                args[1] = Term::Call(
                    "parameter".into(),
                    vec![Term::Str(format!("rotation_{}", *next)), value],
                );
                *next += 1;
            } else {
                for arg in args {
                    term(arg, next);
                }
            }
        }
    }
    fn body(stmts: &mut [Stmt], next: &mut usize) {
        for st in stmts {
            for arg in &mut st.args {
                term(arg, next);
            }
            body(&mut st.body, next);
        }
    }
    for image in module.images.values_mut() {
        body(&mut image.stmts, &mut 0);
    }
}
