//! Packing policy is versioned, board-specific and part of canonical identity.
use ktpu_machine::{Machine, PackingBackend};
use serde_json::json;

#[test]
fn board_policy_and_model_defaults_are_distinct() {
    let silicon = Machine::named("multimesh_v9t6").unwrap();
    let model = Machine::named("vlt_v9t6s_1n").unwrap();
    assert_eq!(
        silicon.conversion.fp16_mx7.allowed_backends,
        vec![PackingBackend::VectorMx7]
    );
    assert_eq!(
        model.conversion.fp16_mx7.preferred_backend,
        PackingBackend::MoverQuant
    );
    let vector = model
        .with_fact("conversion.fp16_mx7.preferred_backend", json!("vector_mx7"))
        .unwrap();
    assert_ne!(vector.digest(), model.digest());
    assert!(silicon
        .with_fact(
            "conversion.fp16_mx7.preferred_backend",
            json!("mover_quant")
        )
        .is_err());
    assert!(model
        .with_fact("conversion.fp16_mx7.version", json!(3))
        .is_err());
    assert!(model
        .with_fact("conversion.fp16_mx7.allowed_backends", json!([]))
        .is_err());
}
