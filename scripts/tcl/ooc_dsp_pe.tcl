# OOC synthesis of the ASSEMBLED DSP PE -- the whole rv_pe with the extension
# enabled. SYNTH ONLY. Results are the @@@ lines.
#   vivado -mode batch -source scripts/tcl/ooc_dsp_pe.tcl -tclargs \
#     <dsp_en> <simd> <muls> <has_shift> <has_perm> <wb> <period_ns>
#
# The unit measured alone says what the extension costs; THIS says what the PE
# runs at, which is the number that decides whether a DSP PE can sit on the
# mesh clock. They are not the same: integration adds the vector stall into the
# MEM stage's own and the reduction's word into the writeback mux, and either
# could be the limit even when the unit alone is not.
#
# At DSP_EN = 0 it is the shipped controller PE, so the same script measures the
# baseline the extension is charged against.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set den  [lindex $argv 0]
set simd [lindex $argv 1]
set muls [lindex $argv 2]
set hsh  [lindex $argv 3]
set hpm  [lindex $argv 4]
set wbs  [lindex $argv 5]
set per  [lindex $argv 6]
set f16  [lindex $argv 7]
set npt  [lindex $argv 8]
if {$f16 eq ""} { set f16 0 }
if {$npt eq ""} { set npt 16 }

if {$den  eq ""} { set den  1 }
if {$simd eq ""} { set simd 8 }
if {$muls eq ""} { set muls 4 }
if {$hsh  eq ""} { set hsh  1 }
if {$hpm  eq ""} { set hpm  1 }
if {$wbs  eq ""} { set wbs  0 }
if {$per  eq ""} { set per  3.333 }
set ::ooc_period $per

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

read_verilog [list \
    [file join $root src kohakuaccel common sync_fifo.v] \
    [file join $root src kohakuaccel common kohaku_sdpram.v] \
    [file join $root src kohakuaccel noc endpoint noc_cu_base.v] \
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
    [file join $root src kohakutpu matmul mx_fpacc.v] \
    [file join $root src kohakutpu vector vec_dsp.v] \
    [file join $root src kohakutpu vector vec_delay.v] \
    [file join $root src kohakutpu vector vec_tables.v] \
    [file join $root src kohakutpu vector vec_cvt.v] \
    [file join $root src kohakutpu vector vec_alu.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_f16_lane.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_facc.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_ffold.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_scalar_decode.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_mul.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_padd32.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_pshift32.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_lane.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_perm.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_reduce.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_vregfile.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_vspad.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_unit.v] \
    [file join $root src kohakuaccel pe rv32 rv_pe.v]]

read_xdc [file join $root scripts xdc ooc_rv_pe.xdc]

set tag "dsppe-en$den-s$simd-m$muls-sh$hsh-pm$hpm-wb$wbs-f$f16-t$per"
puts "@@@ top rv_pe dsp_en $den simd $simd muls $muls shift $hsh perm $hpm wb $wbs f16 $f16 npart $npt period $per"

synth_design -top rv_pe -part $part -mode out_of_context \
             -flatten_hierarchy none -directive default \
             -include_dirs [file join $root src kohakuaccel pe rv32 dsp generated] \
             -generic DSP_EN=$den -generic DSP_SIMD=$simd \
             -generic DSP_MULS=$muls -generic DSP_SHIFT=$hsh \
             -generic DSP_PERM=$hpm -generic DSP_WB=$wbs \
             -generic DSP_F16=$f16 -generic DSP_NPART=$npt

ooc_record $tag \
    "dsp_en=$den simd=$simd muls=$muls shift=$hsh perm=$hpm wb=$wbs f16=$f16 period=$per" \
    2000 2

puts "@@@ ============================ device totals"
ooc_count TOTAL

puts "@@@ ============================ vivado utilization"
ooc_util

puts "@@@ ============================ per unit"
foreach inst {u_core u_core/u_if u_core/u_id u_core/u_ex u_core/u_mem \
              u_core/u_rf u_core/g_dsp.u_khd u_imem u_spad u_l1 u_req u_base} {
    if {[llength [get_cells -quiet $inst]] == 0} {
        puts "@@@ $inst MISSING"
        continue
    }
    ooc_count $inst $inst
}

puts "@@@ ============================ control sets"
ooc_ctrlsets

puts "@@@ ============================ Fmax per clock"
ooc_classify 2000

puts "@@@ ============================ the binding path"
foreach l [split [report_timing -max_paths 1 -nworst 1 -setup -input_pins \
                      -return_string] "\n"] {
    puts "@@@P $l"
}

puts "@@@ ooc_dsp_pe done $tag"
