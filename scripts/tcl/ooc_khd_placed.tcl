# The KohakuDSP vector unit, SYNTHESISED AND PLACED.
#   vivado -mode batch -source scripts/tcl/ooc_khd_placed.tcl -tclargs \
#     <simd> <vregs> <nacc> <vspad_entries> <muls> <has_shift> <has_perm> \
#     <use_dsp> <vreg_prim> <period_ns> <wb>
#
# The matrix runs synth only, which is right for comparing SIZE across fourteen
# configurations. It is NOT right for judging a change aimed at ROUTING: before
# placement Vivado estimates net delay from fanout alone, so any change that
# lowers fanout improves the estimate by construction, whether or not a placer
# would ever have had trouble with it.
#
# 77% of this unit's binding path is interconnect estimate. A fanout fix
# therefore has to be judged here, not there.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set simd [lindex $argv 0]
set vreg [lindex $argv 1]
set nacc [lindex $argv 2]
set vspe [lindex $argv 3]
set muls [lindex $argv 4]
set hsh  [lindex $argv 5]
set hpm  [lindex $argv 6]
set udsp [lindex $argv 7]
set vprm [lindex $argv 8]
set per  [lindex $argv 9]
set wbs  [lindex $argv 10]

if {$simd eq ""} { set simd 8 }
if {$vreg eq ""} { set vreg 8 }
if {$nacc eq ""} { set nacc 2 }
if {$vspe eq ""} { set vspe 1024 }
if {$muls eq ""} { set muls 4 }
if {$hsh  eq ""} { set hsh  1 }
if {$hpm  eq ""} { set hpm  1 }
if {$udsp eq ""} { set udsp yes }
if {$vprm eq ""} { set vprm distributed }
if {$per  eq ""} { set per  3.333 }
if {$wbs  eq ""} { set wbs  0 }
set ::ooc_period $per

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

read_verilog [list \
    [file join $root src kohakuaccel common kohaku_sdpram.v] \
    [file join $root src kohakuaccel pe rv32 mem rv_ram_be.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_mul.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_padd32.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_pshift32.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_lane.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_perm.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_reduce.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_vregfile.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_vspad.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_unit.v]]

read_xdc [file join $root scripts xdc ooc_khd.xdc]

set tag "khdP-s$simd-v$vreg-a$nacc-m$muls-sh$hsh-pm$hpm-wb$wbs-t$per"
puts "@@@ top khd_unit PLACED simd $simd muls $muls shift $hsh perm $hpm wb $wbs period $per"

synth_design -top khd_unit -part $part -mode out_of_context \
             -flatten_hierarchy none -directive default \
             -include_dirs [file join $root src kohakuaccel pe rv32 dsp generated] \
             -generic SIMD=$simd -generic VREGS=$vreg -generic NACC=$nacc \
             -generic VSPAD_ENTRIES=$vspe -generic MULS=$muls \
             -generic HAS_SHIFT=$hsh -generic HAS_PERM=$hpm \
             -generic WB_STAGE=$wbs \
             -generic "USE_DSP=\"$udsp\"" -generic "MEM_PRIM=\"block\"" \
             -generic "VREG_PRIM=\"$vprm\""

puts "@@@ ---- post-synth ----"
ooc_classify 2000

opt_design
place_design
phys_opt_design

puts "@@@ ---- post-place ----"
ooc_record "$tag-placed" "simd=$simd muls=$muls wb=$wbs period=$per placed=1" 2000 2
ooc_classify 2000

puts "@@@ ============================ the binding path, placed"
foreach l [split [report_timing -max_paths 1 -nworst 1 -setup -input_pins \
                      -return_string] "\n"] {
    puts "@@@P $l"
}

puts "@@@ ooc_khd_placed done $tag"
