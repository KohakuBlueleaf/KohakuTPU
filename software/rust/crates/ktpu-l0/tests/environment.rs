//! Coverage assumptions survive translated handles, not just numerical handles.
use ktpu_l0::format::bind::{environment, Translation};
use ktpu_l0::format::template::{ParameterTable, Template};
use ktpu_l0::{Buffer, Kind, Package};

fn buffers() -> Vec<Buffer> {
    ["input", "output"]
        .into_iter()
        .map(|name| Buffer {
            name: name.into(),
            size: 256,
            default: 0,
            kind: Kind::Temp,
            mesh: 0,
        })
        .collect()
}

#[test]
fn overlap_offsets_and_meshes_are_part_of_environment() {
    let mut b = buffers();
    assert!(environment(&b, &[4096, 4352]).unwrap().is_empty());
    assert_eq!(environment(&b, &[4096, 4096]).unwrap(), vec![[0, 1, 0]]);
    assert_eq!(environment(&b, &[4096, 4224]).unwrap(), vec![[0, 1, 128]]);
    assert_eq!(environment(&b, &[4224, 4096]).unwrap(), vec![[0, 1, -128]]);
    assert!(environment(&b, &[4096]).is_err());
    b[1].mesh = 1;
    assert!(environment(&b, &[4096, 4096]).unwrap().is_empty());
}

#[test]
fn translated_alias_guard_and_mutant() {
    let t = Template {
        package: Package {
            buffers: buffers(),
            ..Default::default()
        },
        table: ParameterTable {
            environment: Some(vec![]),
            ..Default::default()
        },
    };
    let handles = [0x1000, 0x2000];
    let translations: Vec<Translation> = vec![(0x1000, 0x10000, 256), (0x2000, 0x10000, 256)];
    let run = || t.bind_handles(&handles, &translations, &Default::default());
    assert_eq!(run().unwrap_err().clause, "L0.BIND.044");
    #[cfg(feature = "mutants")]
    assert!(ktpu_l0::mutant::with("l0.template/environment", run).is_ok());
    assert!(t.bind(&[0x10000, 0x10100], &Default::default()).is_ok());
    assert_eq!(
        t.bind(&[0x10000, 0x10080], &Default::default())
            .unwrap_err()
            .clause,
        "L0.BIND.044"
    );
}
