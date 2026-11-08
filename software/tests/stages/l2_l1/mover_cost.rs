//! Selection and reporting price the same fragmented movement mechanism.
use ktpu_l1::text::read;
use ktpu_l2::cost::{self, resource};
use ktpu_l2::target::Target;

use super::common::card;

#[test]
fn pure_mover_prices_use_the_reporting_resource_clock() {
    let machine = card();
    let target = Target::of(&machine);
    let params = resource::Params::planning(&machine);
    for stride in [32, 512] {
        let source = format!(
            "target ktpu.v9\nfn priced.l1()\n    move local=1\n        copy w(x, (128,{stride})) -> w(y, (128,32))\n"
        );
        let module = read(&source, "mover price").expect("L1");
        let report = resource::kernel(&module, "priced", &machine, &params).expect("report");
        let expected = report["cycles"].as_f64().expect("completion").ceil() as i64
            + machine.vc.launch_front as i64;
        assert_eq!(
            cost::cycles(&module, "priced", &target).expect("selection price"),
            expected
        );
    }
}

#[test]
fn retained_walks_price_like_redeclared_walks_on_each_core() {
    let machine = card();
    let params = resource::Params::planning(&machine);
    let source = r#"target ktpu.v9
image seeded(n)
    desc a0 = 4096 walk=(32,n)
    vfill a0 -> l1[0]
    vsync fe on=fill
    halt

image held()
    seti s0 = 2
    loop s0
        vfill a0 -> l1[0]
        vsync fe on=fill
    halt

fn priced.l1()
    send vc[0]
        run seeded(4)
    wait vc[0]
    send vc[1]
        desc a0 = 8192 walk=(32,16)
    send vc[0] repeats=2
        run held() # four
    wait vc[0]
    send vc[1]
        run held() # sixteen
    wait vc[1]
    send vc[0]
        run seeded(8)
    wait vc[0]
    send vc[0] repeats=2
        run held() # eight
    wait vc[0]
    send vc[0]
        run seeded(4)
    wait vc[0]
    send vc[0] repeats=2
        run held() # four
    wait vc[0]
"#;
    let explicit = source
        .replace(
            "        run held() # four",
            "        desc a0 = 4096 walk=(32,4)\n        run held()",
        )
        .replace(
            "        run held() # eight",
            "        desc a0 = 4096 walk=(32,8)\n        run held()",
        )
        .replace(
            "        run held() # sixteen",
            "        desc a0 = 8192 walk=(32,16)\n        run held()",
        );
    let price = |text: &str| {
        let module = read(text, "descriptor lifetime").expect("L1");
        resource::kernel(&module, "priced", &machine, &params).expect("resource price")
    };
    let retained = price(source);
    let redeclared = price(&explicit);
    assert_eq!(
        retained["work"]["read_bytes"],
        redeclared["work"]["read_bytes"]
    );
    assert_eq!(retained["cycles"], redeclared["cycles"]);
}

#[test]
fn each_run_uses_the_walk_installed_before_it() {
    let machine = card();
    let params = resource::Params::planning(&machine);
    let source = r#"target ktpu.v9
image held()
    vfill a0 -> l1[0]
    vsync fe on=fill
    halt

fn priced.l1()
    send vc[0]
        desc a0 = 4096 walk=(32,4)
        run held()
        desc a0 = 8192 walk=(32,8)
        run held()
        desc a0 = 12288
        run held()
    wait vc[0]
"#;
    let separate = source
        .replace(
            "        desc a0 = 8192",
            "    wait vc[0]\n    send vc[0]\n        desc a0 = 8192",
        )
        .replace(
            "        desc a0 = 12288",
            "    wait vc[0]\n    send vc[0]\n        desc a0 = 12288 walk=(32,1)",
        );
    let reads = |text: &str| {
        let module = read(text, "ordered descriptors").expect("L1");
        let report =
            resource::kernel(&module, "priced", &machine, &params).expect("resource price");
        report["work"]["read_bytes"].clone()
    };
    assert_eq!(reads(source), reads(&separate));
}

#[test]
fn image_descriptors_are_launch_work_not_instruction_memory() {
    let machine = card();
    let params = resource::Params::planning(&machine);
    let source = r#"target ktpu.v9
image transfer()
    desc a0 = 4096 walk=(32,8)
    vfill a0 -> l1[0]
    vsync fe on=fill
    halt

fn priced.l1()
    send vc[0]
        run transfer()
    wait vc[0]
    send vc[0]
        run transfer()
    wait vc[0]
"#;
    let sent = source
        .replace("    desc a0 = 4096 walk=(32,8)\n", "")
        .replace(
            "        run transfer()",
            "        desc a0 = 4096 walk=(32,8)\n        run transfer()",
        );
    let price = |text: &str| {
        let module = read(text, "launch descriptors").expect("L1");
        resource::kernel(&module, "priced", &machine, &params).expect("resource price")
    };
    let owned = price(source);
    let explicit = price(&sent);
    assert_eq!(owned["work"], explicit["work"]);
    assert_eq!(owned["cycles"], explicit["cycles"]);
}

#[test]
fn repeated_sends_inherit_the_previous_iterations_final_walk() {
    let machine = card();
    let params = resource::Params::planning(&machine);
    let prefix = r#"target ktpu.v9
image held()
    vfill a0 -> l1[0]
    vsync fe on=fill
    halt

fn priced.l1()
    send vc[0]
        desc a0 = 4096 walk=(32,4)
"#;
    let body = "        run held()\n        desc a0 = 4096 walk=(32,8)\n        run held()\n";
    let suffix = "    wait vc[0]\n    send vc[0]\n        run held()\n    wait vc[0]\n";
    let repeated = format!("{prefix}    send vc[0] repeats=3\n{body}{suffix}");
    let mut explicit = format!("{prefix}        run held()\n    wait vc[0]\n");
    for _ in 0..6 {
        explicit.push_str(
            "    send vc[0]\n        desc a0 = 4096 walk=(32,8)\n        run held()\n    wait vc[0]\n",
        );
    }
    let report = |source: &str| {
        let module = read(source, "repeated descriptor state").expect("L1");
        resource::kernel(&module, "priced", &machine, &params).expect("resource price")
    };
    let repeated = report(&repeated);
    let explicit = report(&explicit);
    assert_eq!(
        repeated["work"]["read_bytes"],
        explicit["work"]["read_bytes"]
    );
}
