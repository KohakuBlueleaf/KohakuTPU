//! The planner uses the shared host-write and IEEE-read numerics.

use ktpu_plan::fold::{f16_bits, f16_value, to_f16};

#[test]
fn rul_057_planner_writes_saturate() {
    for x in [70000.0, 65520.0, 1e6] {
        assert_eq!(f16_bits(x), 0x7BFF);
        assert_eq!(f16_bits(-x), 0xFBFF);
        assert_eq!(to_f16(x), 65504.0);
    }
    assert_eq!(f16_bits(f64::INFINITY), 0x7C00);
    assert_eq!(f16_value(0x7C00), f64::INFINITY);
    assert_eq!(f16_value(1), 2f64.powi(-24));
}
