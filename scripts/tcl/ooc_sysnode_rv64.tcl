# What the system node costs whole, with the RV64 control complex in place of
# the RV32 one. The mover and the transform slot are the SAME in both -- they
# are parts of the node, not of the processor -- so the difference this reports
# is the processor's and nothing else.
#
#   vivado -mode batch -source scripts/tcl/ooc_sysnode_rv64.tcl -tclargs <ports>
#
# THE GATE IS THIS RUN, not an arithmetic projection off the RV32 figure.
# Subtracting `rv_mag_pe` from the node removes the mover with it, and the mover
# does not disappear -- it stays whatever processor sits in the node.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set ports [lindex $argv 0]
if {$ports eq ""} { set ports 2 }
set tag "sn64_p${ports}"

set part xcvu13p-fhgb2104-2L-e
create_project -in_memory -part $part

read_verilog [list \
    [file join $root src kohakuaccel common sync_fifo.v] \
    [file join $root src kohakuaccel common async_fifo.v] \
    [file join $root src kohakuaccel common sb_skid.v] \
    [file join $root src kohakuaccel common kohaku_sdpram.v] \
    [file join $root src kohakuaccel common kohaku_sdpram_be.v] \
    [file join $root src kohakuaccel noc ctrl noc_orchestrator.v] \
    [file join $root src kohakuaccel noc endpoint noc_cu_base.v] \
    [file join $root src kohakutpu transform mx_quant.v] \
    [file join $root src kohakutpu transform xform_bank.v] \
    [file join $root src kohakuaccel sysnode core mag_xform.v] \
    [file join $root src kohakuaccel sysnode mover mx_tdesc.v] \
    [file join $root src kohakuaccel sysnode mover mm_prng.v] \
    [file join $root src kohakuaccel sysnode mover mm_mover.v] \
    [file join $root src kohakuaccel sysnode mover mv_exec.v] \
    [file join $root src kohakuaccel sysnode core mag_stage.v] \
    [file join $root src kohakuaccel sysnode core mag_stage_port.v] \
    [file join $root src kohakuaccel sysnode core mag_mem_port.v] \
    [file join $root src kohakuaccel sysnode core mag_dram_port.v] \
    [file join $root src kohakuaccel sysnode interlink il_pkt_arb.v] \
    [file join $root src kohakuaccel sysnode interlink mag_link.v] \
    [file join $root src kohakuaccel sysnode interlink mag_link_pipe.v] \
    [file join $root src kohakuaccel sysnode interlink mag_switch.v] \
    [file join $root src kohakuaccel sysnode interlink mag_ilink.v] \
    [file join $root src kohakuaccel sysnode core sn_hub.v] \
    [file join $root src kohakuaccel sysnode core mag.v] \
    [file join $root src kohakuaccel sysnode sysnode.v]]

# The RV32 processor's sources still come: `sysnode` names `rv_mag_pe` in the
# branch that is not generated, and the whole chain has to parse.
read_verilog [list \
        [file join $root src kohakuaccel pe rv32 mem rv_ram_be.v] \
        [file join $root src kohakuaccel pe rv32 mem rv_imem.v] \
        [file join $root src kohakuaccel pe rv32 mem rv_spad.v] \
        [file join $root src kohakuaccel pe rv32 mem rv_l1.v] \
        [file join $root src kohakuaccel pe rv32 core rv_regfile.v] \
        [file join $root src kohakuaccel pe rv32 core rv_bpred.v] \
        [file join $root src kohakuaccel pe rv32 core rv_if.v] \
        [file join $root src kohakuaccel pe rv32 core rv_id.v] \
        [file join $root src kohakuaccel pe rv32 core rv_ex.v] \
        [file join $root src kohakuaccel pe rv32 core rv_mem.v] \
        [file join $root src kohakuaccel pe rv32 core rv_wb.v] \
        [file join $root src kohakuaccel pe rv32 core rv_core.v] \
        [file join $root src kohakuaccel pe rv32 noc rv_noc_req.v] \
        [file join $root src kohakuaccel pe rv32 noc rv_mag_req.v] \
        [file join $root src kohakuaccel sysnode cpu rv_mag_pe.v]]

read_verilog [glob \
        [file join $root src kohakuaccel pe rv64-sys *.v] \
        [file join $root src kohakuaccel pe rv64-sys core *.v]]
read_verilog [list [file join $root src kohakuaccel sysnode cpu rv64_mag_pe.v]]

read_xdc [file join $root scripts xdc ooc_sysnode.xdc]

# STAGE_AT_PORT=1 IS NOT A TUNING KNOB HERE. At 0 the RTL puts a whole staging
# store inside EVERY memory port -- PORTS x 64 URAM, 4 MB to obtain 2 MB -- and
# its own comment says those copies are "unreachable by mover and interlink".
# SysCore's page tables, the cross-node mailbox and the allocator bitmap all
# live in staging and are reached over the converged path, so the per-port
# store is both twice the URAM and the wrong store.
synth_design -top sysnode -mode out_of_context -part $part -directive default \
    -generic STAGE=1 -generic ILINK=1 -generic PORTS=$ports \
    -generic STAGE_AT_PORT=1 \
    -generic CPU_RV64=1 -generic PE_IMEM=8192 -generic PE_SPAD=4096 \
    -generic PE_L1_LINES=64

if {[llength [get_clocks -quiet]] == 0} {
    error "no clock: ooc_sysnode.xdc did not apply, so every figure would be unconstrained"
}
report_utilization -quiet -file [file join $root build "node_${tag}_util.rpt"]
report_utilization -hierarchical -hierarchical_depth 4 -quiet \
    -file [file join $root build "node_${tag}_hier.rpt"]
report_timing_summary -quiet -file [file join $root build "node_${tag}_time.rpt"]

set nlut ?; set nff ?; set nbram ?; set ndsp ?; set nuram ?
set u [report_utilization -return_string]
regexp {CLB LUTs\D+(\d+)}          $u -> nlut
regexp {CLB Registers\D+(\d+)}     $u -> nff
regexp {Block RAM Tile\D+([\d.]+)} $u -> nbram
regexp {URAM\D+(\d+)}              $u -> nuram
regexp {DSPs\D+(\d+)}              $u -> ndsp
set slk [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]

puts "@@@ NODE64 PORTS=$ports LUT=$nlut FF=$nff BRAM=$nbram URAM=$nuram DSP=$ndsp WNS=$slk"
if {$nlut ne "?" && $nlut > 35000} {
    puts "@@@ OVER BUDGET: $nlut LUT against the 35,000 target"
} else {
    puts "@@@ WITHIN BUDGET: $nlut LUT against the 35,000 target"
}

# EVERY failing path, with its logic level: a level count above 11 is the thing
# to fix, not the slack.
set bad [get_timing_paths -max_paths 2000 -nworst 1 -setup -slack_lesser_than 0]
puts "@@@FAILN [llength $bad]"

proc base {pin} {
    set c [file dirname $pin]
    regsub {\[[0-9]+\]$} $c "" c
    return $c
}

array set grp {}
foreach p $bad {
    set k "[base [get_property STARTPOINT_PIN $p]] -> [base [get_property ENDPOINT_PIN $p]]"
    set s [get_property SLACK $p]
    set l [get_property LOGIC_LEVELS $p]
    if {[info exists grp($k)]} {
        lassign $grp($k) cnt worst lvl
        set grp($k) [list [expr {$cnt + 1}] [expr {$s < $worst ? $s : $worst}] \
                          [expr {$l > $lvl ? $l : $lvl}]]
    } else {
        set grp($k) [list 1 $s $l]
    }
}
set rows {}
foreach k [array names grp] {
    lassign $grp($k) cnt worst lvl
    lappend rows [list $worst $cnt $lvl $k]
}
foreach r [lsort -real -index 0 $rows] {
    lassign $r worst cnt lvl k
    puts [format "@@@GROUP %4d paths  worst %+7.3f  lvl %3s  %s" $cnt $worst $lvl $k]
}
