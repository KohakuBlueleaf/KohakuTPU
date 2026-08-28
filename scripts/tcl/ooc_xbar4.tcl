# Full-SAMD kaxi_xbar4 (per-home parallel write+read) vs xbar2 (naive full SAMD).
# Target: under xbar2 10,221 / SmartConnect 8,471, toward axi-ic perf 3,980.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set x4 [file join $root src kohakuaxi kaxi_xbar4.v]
set x2 [file join $root src kohakuaxi kaxi_xbar2.v]
set x3 [file join $root src kohakuaxi kaxi_xbar3.v]

proc syn {f part top M N} {
    catch {close_design}
    read_verilog $f
    synth_design -top $top -part $part -mode out_of_context \
        -generic M=$M -generic N_HOME=$N -generic DATA_W=512
    create_clock -name clk -period 2.0 [get_ports clk]
    ooc_record "$top-${M}x${N}" "M=$M N=$N dw=512" 2000 1
}
syn $x4 $part kaxi_xbar4 4 4
syn $x4 $part kaxi_xbar4 8 8
syn $x4 $part kaxi_xbar4 8 4
syn $x2 $part kaxi_xbar2 4 4
syn $x3 $part kaxi_xbar3 4 4
puts "XBAR4_DONE"
