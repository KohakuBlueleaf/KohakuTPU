# Does tying `op` to a constant actually prune vec_alu's transcendental half?
#
# `khs_float_lane` instantiates vec_alu with op tied to OP_FMA, so EXP2/LOG2/INV/
# RSQRT and the compare paths should constant-fold away. This measures whether
# they do: same module, once with `op` a free input and once tied.
#
#   vivado -mode batch -source ooc_valu.tcl -tclargs <top> <period>
#
# vec_alu appears 8x per SIMD PE and 8x per SIMT PE -- 96 instances in a mesh --
# so a LUT here is 96 LUT of mesh and the question is worth asking precisely.

set root [file normalize [file join [file dirname [info script]] .. ..]]

set top [lindex $argv 0]
set per [lindex $argv 1]
if {$top eq ""} { set top vec_alu }
if {$per eq ""} { set per 2.500 }

set_param general.maxThreads 4

read_verilog [list \
    [file join $root src kohakutpu vector vec_dsp.v] \
    [file join $root src kohakutpu vector vec_delay.v] \
    [file join $root src kohakutpu vector vec_cvt.v] \
    [file join $root src kohakutpu matmul mx_fpacc.v] \
    [file join $root src kohakutpu vector vec_tables.v] \
    [file join $root src kohakutpu vector vec_alu.v] \
    [file join $root src kohakuaccel pe rv32 simd khs_float_lane.v] \
]

synth_design -top $top -part xcvu13p-fhgb2104-2L-e -mode out_of_context \
             -flatten_hierarchy none

create_clock -period $per -name clk [get_ports clk]

set rpt [report_utilization -hierarchical -hierarchical_depth 3 -return_string]
foreach line [split $rpt "\n"] {
    if {[string match "*|*" $line]} { puts "@@@HIER $top | $line" }
}

set lut [llength [get_cells -hier -filter {PRIMITIVE_GROUP == LUT}]]
set dsp [llength [get_cells -hier -filter {PRIMITIVE_GROUP == ARITHMETIC}]]
set ff  [llength [get_cells -hier -filter {PRIMITIVE_GROUP == FLOP_LATCH}]]
puts "@@@VALU top=$top period=$per lut=$lut ff=$ff dsp=$dsp"
