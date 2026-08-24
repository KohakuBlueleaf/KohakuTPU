# GATE 0: what the control processor costs the system node, in LUT and slack.
#
#   vivado -mode batch -source scripts/tcl/ooc_sysnode.tcl -tclargs <CTRL_PE> <period_ns>
#
# Two runs, one number each. CTRL_PE=0 is the shipping node; CTRL_PE=1 adds the
# processor AND widens mag's converged-path arbiters by one requester, which is
# the half that can cost slack rather than area.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set pe    [lindex $argv 0]
set ports [lindex $argv 1]
if {$pe    eq ""} { set pe 0 }
# PRODUCTION SHIPS AT LEAST TWO. A one-port figure understates the node, and the
# per-port cost has to come from adjacent rows, never a tier divided by a count.
if {$ports eq ""} { set ports 2 }
set tag "pe${pe}_p${ports}_nx"

set part xcvu13p-fhgb2104-2L-e
create_project -in_memory -part $part

read_verilog [list \
    [file join $root src kohakuaccel common sync_fifo.v] \
    [file join $root src kohakuaccel common async_fifo.v] \
    [file join $root src kohakuaccel common sb_skid.v] \
    [file join $root src kohakuaccel common kohaku_sdpram.v] \
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
    [file join $root src kohakuaccel sysnode core mag.v] \
    [file join $root src kohakuaccel sysnode sysnode.v]]

# The processor's own sources, only when it is generated.
if {$pe ne "0"} {
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
}

read_xdc [file join $root scripts xdc ooc_sysnode.xdc]

synth_design -top sysnode -mode out_of_context -part $part -directive default \
    -generic CTRL_PE=$pe -generic STAGE=1 -generic ILINK=1 \
    -generic MEM_PORTS=$ports

if {[llength [get_clocks -quiet]] == 0} {
    error "no clock: ooc_sysnode.xdc did not apply, so every figure would be unconstrained"
}
report_utilization -quiet -file [file join $root build "node_${tag}_util.rpt"]
# -flatten_hierarchy none, or a leaf row is charged to whoever it re-parented to
# and the attribution is wrong in exactly the direction that hides the cost.
report_utilization -hierarchical -hierarchical_depth 4 -quiet \
    -file [file join $root build "node_${tag}_hier.rpt"]
write_checkpoint -force [file join $root build "node_${tag}.dcp"]
report_timing_summary -quiet -file [file join $root build "node_${tag}_time.rpt"]

# Defaults, because an unmatched regexp leaves the variable UNSET and the puts
# below then errors out AFTER a clean synthesis -- which reads as a failed run.
set nlut ?; set nff ?; set nbram ?; set ndsp ?
set u [report_utilization -return_string]
regexp {CLB LUTs\D+(\d+)}        $u -> nlut
regexp {CLB Registers\D+(\d+)}   $u -> nff
regexp {Block RAM Tile\D+([\d.]+)} $u -> nbram
regexp {DSPs\D+(\d+)}            $u -> ndsp
set slk [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]

# ONE transform bank (32 DSP) plus the mover's 3, whatever the port count. A
# figure that scales with $ports means an occupant is back inside a port.
if {$ndsp ne "?" && $ndsp > 48} {
    error "$ndsp DSP: a transform instance is being generated per port again"
}
puts "@@@ NODE CTRL_PE=$pe PORTS=$ports LUT=$nlut FF=$nff BRAM=$nbram DSP=$ndsp WNS=$slk"
