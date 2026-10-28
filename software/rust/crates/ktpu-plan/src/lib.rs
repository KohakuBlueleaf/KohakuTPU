//! The tensor planner: a tinygrad kernel ast (`ktpu.uops`) planned (TG.PLAN), then run against
//! the bound buffers' records (`ktpu.state`) into a step program of kernel
//! graphs and form steps (TG.RUN, API.FORM); a `kp` op log likewise (API).
//! Planning is a pure function of (input, state, machine).

pub mod ast;
pub mod input;
pub mod plan;
pub mod run;
pub mod spec;
pub mod tensor;

pub use ast::{domain, fold, uop};
pub use input::hw;
pub use plan::{class, expr, lower, program};
pub use run::exec;
pub use spec::mutant;

pub use input::{Buffer, Slot, State, Uop, Uops};
pub use program::{
    Access, Addr, Array, Call, Case, Expr, ExprNode, Launch, Lowered, Plan, Range, RowsCall, Run,
    Step,
};
