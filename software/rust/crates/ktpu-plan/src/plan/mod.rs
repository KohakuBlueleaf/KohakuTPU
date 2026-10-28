//! A kernel ast as a plan (TG.PLAN): the lowering, the expression it
//! computes, the step program it becomes, and each refusal's class.

pub mod class;
pub mod expr;
pub mod lower;
pub mod program;
