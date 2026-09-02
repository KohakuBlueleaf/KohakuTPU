# multimesh v8t3: the ship shape of the v8 memory path -- four 2x2 meshes
# (2 clusters + 2 vector cores + system node each) on ONE sysnode clock, the
# interlink chain 0-1-2-3, ONE partition-aware Kohaku Xache, four MIGs named by
# SLR, four stations and JTAG on ONE fixed 200 MHz system clock.
#   vivado -mode batch -source scripts/tcl/multimesh_v8t3_bd.tcl \
#     ?-tclargs rebuild|synth ?jobs N? ?noanalyze??
# Every knob is in scripts/tcl/v8t3/00_config.tcl and nowhere else. Its own
# project, always. Implementation is scripts/tcl/v8t3_impl.tcl.

set here [file dirname [file normalize [info script]]]
set do_synth [expr {[lsearch $argv synth] >= 0}]
set do_build [expr {[lsearch $argv rebuild] >= 0 || !$do_synth}]
set njobs 4
if {[set i [lsearch $argv jobs]] >= 0} { set njobs [lindex $argv [expr {$i + 1}]] }

# A variant (multimesh_v8t4_bd.tcl) sets these before sourcing this file: the
# stages are shared, only the config differs.
if {![info exists V8_CONFIG]} { set V8_CONFIG $here/v8t3/00_config.tcl }
if {![info exists V8_STAGES]} { set V8_STAGES $here/v8t3 }
source $V8_CONFIG
set stages $V8_STAGES
set_param general.maxThreads 16

# bd/mref/<mod>/component.xml is packaged ONCE and the RTL never re-parsed:
# drop every cached module reference BEFORE the project opens -- but only on
# a rebuild. Dropping them before a plain reopen forces a repackage on open.
set v8_dropped {}
set g $proj_dir/${design_name}.gen
if {$do_build} {
    set mods [list $SB_WRAP kx_pbd_4x4 xcvu13p_rst_tree kts_pipe_bd ktpu_div2]
    foreach {mid mod} $MESHES { if {[lsearch $mods $mod] < 0} { lappend mods $mod } }
    foreach m $mods {
        if {[file isdirectory $g/sources_1/bd/mref/$m]} {
            file delete -force $g/sources_1/bd/mref/$m
            lappend v8_dropped $m
        }
    }
}
puts "@@@ ${design_name} mref: dropped [llength $v8_dropped] stale module-reference cache(s)"

if {!$do_build && [file exists $proj_dir/${design_name}.xpr]} {
    open_project $proj_dir/${design_name}.xpr
    if {[get_property top [current_fileset]] ne "${design_name}_wrapper"} {
        error "top is [get_property top [current_fileset]], not the BD wrapper"
    }
    puts "@@@ ${design_name}: reopened $proj_dir, top [get_property top [current_fileset]]"
    set v8_need_build 0
} else {
    # ONE SHOT: a rebuild starts from nothing, so no stale file survives.
    if {$do_build && [file isdirectory $proj_dir]} {
        file delete -force $proj_dir
        puts "@@@ ${design_name}: wiped $proj_dir"
    }
    create_project -force $design_name $proj_dir -part $part
    set_property target_language Verilog [current_project]
    set v8_need_build 1
}

if {$v8_need_build} {

source $stages/10_sources.tcl

create_bd_design $design_name
current_bd_design $design_name

source $stages/20_clocks.tcl
source $stages/30_meshes.tcl
source $stages/35_xache.tcl
source $stages/40_bus.tcl
source $stages/50_addr.tcl

regenerate_bd_layout
validate_bd_design
source $stages/55_addr_fill.tcl
validate_bd_design
save_bd_design

source $stages/60_constraints.tcl

set bdf [get_files ${design_name}.bd]
generate_target all $bdf
make_wrapper -files $bdf -top -import -force
update_compile_order -fileset sources_1
set_property top ${design_name}_wrapper [current_fileset]
set_property top_auto_set 0 [current_fileset]
if {[get_property top [current_fileset]] ne "${design_name}_wrapper"} {
    error "top is [get_property top [current_fileset]], not the BD wrapper"
}
puts "@@@ ${design_name} top: [get_property top [current_fileset]] in $proj_dir"

}

if {!$do_synth} {
    puts "@@@ ${design_name}: block design built. Verify with v8t3/75_verify_bd.tcl; -tclargs synth to synthesise."
    return
}

# A run whose launcher died keeps STATUS "Running" and refuses a relaunch
# (Common 17-69); reset anything unfinished before launching.
proc v8_pending {runs} {
    set todo {}
    foreach r $runs {
        set run [get_runs $r]
        if {[get_property PROGRESS $run] == "100%" && ![get_property NEEDS_REFRESH $run]} { continue }
        if {![string match "Not started" [get_property STATUS $run]]} { reset_run $r }
        lappend todo $r
    }
    return $todo
}
# A module reference's OOC run is NOT marked out of date when its RTL changes --
# Vivado tracks the packaged component, not the source mtime -- so an edited
# source silently re-links the previous netlist (a v8t4 "re-synthesis" that
# finished in 51 s and kept a 30-minute-old Xache). `resynth` resets them.
if {[lsearch $argv resynth] >= 0} {
    set v8_mref [list station_bus xache rst_tree rst_tree_bus]
    foreach {mid mod} $MESHES { lappend v8_mref mesh_$mid }
    foreach hop {{0 1} {1 2} {2 3}} {
        lassign $hop lo hi
        lappend v8_mref pipe_${lo}_to_${hi} pipe_${hi}_to_${lo}
    }
    set v8_reset {}
    foreach c $v8_mref {
        foreach r [get_runs -quiet ${design_name}_${c}_0_synth_1] { lappend v8_reset $r }
    }
    lappend v8_reset synth_1
    foreach r $v8_reset {
        if {![string match "Not started" [get_property STATUS [get_runs $r]]]} { reset_run $r }
    }
    puts "@@@ resynth: reset [llength $v8_reset] run(s) -- [join $v8_mref {, }]"
}

# OOC IP first and explicitly, at OOC_JOBS. IP runs do not carry IS_SYNTHESIS;
# match them by name.
set ooc [v8_pending [get_runs -quiet -filter {NAME =~ "*_synth_1" && NAME != "synth_1"}]]
if {[llength $ooc]} {
    puts "@@@ ${design_name} ooc: [llength $ooc] runs, -jobs $OOC_JOBS"
    launch_runs $ooc -jobs $OOC_JOBS
    foreach r $ooc { wait_on_run $r }
    foreach r $ooc {
        if {[get_property PROGRESS [get_runs $r]] != "100%"} {
            error "OOC run $r failed: [get_property DIRECTORY [get_runs $r]]/runme.log"
        }
    }
} else {
    puts "@@@ ${design_name} ooc: every IP run already complete"
}
if {[llength [v8_pending synth_1]]} {
    puts "@@@ ${design_name} synth_1: launched at [clock format [clock seconds] -format %H:%M:%S]"
    launch_runs synth_1 -jobs $njobs
    wait_on_run synth_1
} else {
    puts "@@@ ${design_name} synth_1: already complete"
}
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synthesis failed: $proj_dir/${design_name}.runs/synth_1/runme.log"
}
puts "@@@ ${design_name} synth_1: done at [clock format [clock seconds] -format %H:%M:%S]"
# The analysis reads the synth_1 checkpoint (v8t3_analyze.tcl) and runs beside
# v8t3_impl.tcl, not in front of it; `analyze` keeps the inline form.
if {[lsearch $argv analyze] >= 0} { source $stages/70_analyze.tcl }
puts "@@@ ${design_name}: synthesis done. Next: v8t3_impl.tcl (impl) and v8t3_analyze.tcl (checks), side by side."
