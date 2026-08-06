// Compile this ahead of a matmul bench to run against the real DSP48E2 instead
// of the behavioural model. Requires -L unisims_ver at elaboration.
//
// A file rather than `xvlog -d MX_MODEL=0`, because Vivado's .bat wrappers
// split arguments on '=' and the define never arrives intact. Same reason
// run_noc_sim.ps1 generates its MESH_N define -- see docs/simulation.md s3.

`define MX_MODEL 0
