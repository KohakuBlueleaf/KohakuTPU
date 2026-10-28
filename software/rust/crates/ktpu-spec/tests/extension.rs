//! Every compiler port advertised to Python, including its mutation witnesses.

#![cfg(feature = "stack")]

#[test]
fn extension_routes_every_compiler_suite_and_kills_its_mutants() {
    let advertised = ktpu::spec::runners();
    let mut unique = advertised.clone();
    unique.sort_unstable();
    unique.dedup();
    assert_eq!(unique.len(), advertised.len());
    for suite in ktpu_spec::runners::suites() {
        for name in (suite.runners)() {
            if !name.starts_with("tg.") {
                assert!(
                    advertised.contains(&name),
                    "unadvertised compiler runner {name}"
                );
            }
        }
    }
    let vectors = ktpu_spec::vectors::load(&ktpu_spec::vectors::root()).unwrap();
    let mut count = 0;
    let mut kills = 0;
    let mut pending = 0;
    for v in vectors
        .iter()
        .filter(|v| v.tier == 0 && advertised.contains(&v.runner.as_str()))
    {
        let run = |mu| {
            ktpu::spec::run(
                &v.runner,
                &v.input,
                v.machine.as_deref(),
                mu,
                Some(&v.id),
                &v.expect,
            )
        };
        let got = run(None).unwrap_or_else(|e| panic!("{}: {e}", v.id));
        if v.status == "ruling" {
            assert_ne!(got["result"], got["expect"], "{}: mark it frozen", v.id);
            pending += 1;
            continue;
        }
        assert_eq!(got["result"], got["expect"], "{}", v.id);
        count += 1;
        if v.status == "frozen" {
            for mu in &v.mutants {
                assert!(
                    ktpu::mutant::names().contains(&mu.as_str()),
                    "{}: missing {mu}",
                    v.id
                );
                if let Ok(broken) = run(Some(mu)) {
                    assert_ne!(
                        broken["result"], broken["expect"],
                        "{}: survived {mu}",
                        v.id
                    );
                }
                kills += 1;
            }
        }
    }
    println!(
        "extension: {count} vectors passed; {pending} pending rulings; {kills} mutants killed"
    );
}
