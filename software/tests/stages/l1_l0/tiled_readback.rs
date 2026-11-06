//! Read independently constructed tiled bytes, without using the packer.

use std::collections::BTreeMap;

use ktpu_l1::layout::buffer::{Buffer, LayoutVal};

#[test]
fn tiled_readback_decodes_each_element_before_reordering() {
    for dtype in ["f32", "u8", "u32", "i32", "u64"] {
        let buffer = Buffer {
            dtype: dtype.into(),
            shape: vec![2, 8, 8],
            space: "dram".into(),
            layout: BTreeMap::from([
                ("tile".into(), LayoutVal::Dims(vec![2, 2])),
                ("t".into(), LayoutVal::Int(1)),
            ]),
        };
        let mut raw = Vec::new();
        for batch in 0..2 {
            for tile_row in 0..2 {
                for tile_col in 0..2 {
                    for row in 0..4 {
                        for col in 0..4 {
                            let i = tile_row * 4 + row;
                            let j = tile_col * 4 + col;
                            let value = batch * 64 + j * 8 + i;
                            match dtype {
                                "f32" => raw.extend_from_slice(&(value as f32).to_le_bytes()),
                                "u8" => raw.push(value as u8),
                                "u32" => raw.extend_from_slice(&(value as u32).to_le_bytes()),
                                "i32" => raw.extend_from_slice(&(value as i32).to_le_bytes()),
                                _ => raw.extend_from_slice(&(value as u64).to_le_bytes()),
                            }
                        }
                    }
                }
            }
        }
        let expected: Vec<f64> = (0..128).map(|n| n as f64).collect();
        assert_eq!(buffer.unpack(&raw).expect("valid tiled bytes"), expected);
    }
}
