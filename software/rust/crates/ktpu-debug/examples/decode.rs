//! `cargo run -p ktpu-debug --example decode -- <region file> <machine name>`:
//! a dumped debug region decoded, as JSON on stdout.

use ktpu_machine::Machine;

fn main() {
    let a: Vec<String> = std::env::args().collect();
    if a.len() != 3 {
        eprintln!("usage: decode <region file> <machine name>");
        std::process::exit(2);
    }
    let region = std::fs::read(&a[1]).expect("region file");
    let m = Machine::named(&a[2]).expect("machine");
    match ktpu_debug::decode_json(&region, &m) {
        Ok(v) => println!("{}", serde_json::to_string_pretty(&v).unwrap()),
        Err(r) => {
            eprintln!("{r}");
            std::process::exit(1);
        }
    }
}
