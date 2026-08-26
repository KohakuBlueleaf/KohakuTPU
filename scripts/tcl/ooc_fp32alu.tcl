# OOC synthesis of KohakuMPE's FP32 vector ALU at one width.
#   vivado -mode batch -source scripts/tcl/ooc_fp32alu.tcl \
#          -tclargs <flanes> <period> <fsfu>
#
# The MPE float path only. KohakuTPU's vector core is not read here and not
# modified anywhere -- it keeps computing in E8M15.
#
# `fsfu` IS THE SEED COUNT, so the seed unit's marginal cost is a difference
# between two rows of this table rather than an estimate.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set fln [lindex $argv 0]
set per [lindex $argv 1]
set sfu [lindex $argv 2]
if {$fln eq ""} { set fln 8 }
if {$per eq ""} { set per 3.333 }
if {$sfu eq ""} { set sfu 0 }
set ::ooc_period $per

# The array checks the caller's depth against its own, so the harness has to
# resolve it the same way khs_unit does: 9 with seeds, 6 without.
set alat [expr {$sfu != 0 ? 9 : 6}]

# `pass` carries the LONGER walk; the default sizes the FMA walk alone, giving
# fsfu=2 flanes=8 a 1-bit index where khs_unit builds 2.
set elems 8
set pw_n [expr {$elems / $fln}]
set seed_u [expr {$sfu > 0 ? $sfu : $fln}]
set ps_n [expr {$elems / $seed_u}]
set pmax [expr {$ps_n > $pw_n ? $ps_n : $pw_n}]
set psw [expr {$pmax > 1 ? int(ceil(log($pmax) / log(2))) : 1}]

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

read_verilog [list \
    [file join $root src kohakuaccel pe rv32 core rv_fpu.v] \
    [file join $root src kohakumpe simd khs_lead1.v] \
    [file join $root src kohakumpe simd generated khs_seed_tab.v] \
    [file join $root src kohakumpe simd khs_fp32_sfu.v] \
    [file join $root src kohakumpe simd khs_fp32_alu.v]]

read_xdc [file join $root scripts xdc ooc_rv_pe.xdc]

synth_design -top khs_fp32_alu -part $part -mode out_of_context \
             -flatten_hierarchy rebuilt -directive default \
             -generic VW=256 -generic FLANES=$fln \
             -generic FSFU_UNITS=$sfu -generic PSW=$psw -generic ALAT=$alat

ooc_record "fp32alu-fl$fln-sfu$sfu-t$per" \
    "flanes=$fln fsfu=$sfu vw=256 period=$per" 1 2

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ooc_fp32alu done fl$fln sfu$sfu"
