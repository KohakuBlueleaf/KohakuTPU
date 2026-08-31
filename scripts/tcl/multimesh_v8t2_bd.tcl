# multimesh v8t2: v8t with the banked Xache array (KX_BANKS 8). Same stages,
# own project.
#   vivado -mode batch -source scripts/tcl/multimesh_v8t2_bd.tcl \
#     ?-tclargs rebuild|synth ?jobs N??

set here [file dirname [file normalize [info script]]]
set do_synth [expr {[lsearch $argv synth] >= 0}]
set do_build [expr {[lsearch $argv rebuild] >= 0 || !$do_synth}]
set njobs 4
if {[set i [lsearch $argv jobs]] >= 0} { set njobs [lindex $argv [expr {$i + 1}]] }

source $here/v8t2/00_config.tcl
set stages $here/v8t
set_param general.maxThreads 16

# drop the cached module references only on a rebuild (see multimesh_v8t_bd.tcl)
set v8_dropped {}
set g $proj_dir/${design_name}.gen
if {$do_build} {
    foreach m {ktpu_node_v8t sb_bd_line4 kx_pbd_4x4 xcvu13p_rst_tree} {
        if {[file isdirectory $g/sources_1/bd/mref/$m]} {
            file delete -force $g/sources_1/bd/mref/$m
            lappend v8_dropped $m
        }
    }
}
puts "@@@ v8t2 mref: dropped [llength $v8_dropped] stale module-reference cache(s)"

if {!$do_build && [file exists $proj_dir/${design_name}.xpr]} {
    open_project $proj_dir/${design_name}.xpr
    if {[get_property top [current_fileset]] ne "${design_name}_wrapper"} {
        error "top is [get_property top [current_fileset]], not the BD wrapper"
    }
    puts "@@@ v8t2: reopened $proj_dir, top [get_property top [current_fileset]]"
    set v8_need_build 0
} else {
    if {$do_build && [file isdirectory $proj_dir]} {
        file delete -force $proj_dir
        puts "@@@ v8t2: wiped $proj_dir"
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
source $stages/30_nodes.tcl
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
puts "@@@ v8t2 top: [get_property top [current_fileset]] in $proj_dir"

}

if {!$do_synth} {
    puts "@@@ v8t2: block design built. Verify with v8t2/75_verify_bd.tcl; -tclargs synth to synthesise."
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
set ooc [v8_pending [get_runs -quiet -filter {NAME =~ "*_synth_1" && NAME != "synth_1"}]]
if {[llength $ooc]} {
    puts "@@@ v8t2 ooc: [llength $ooc] runs, -jobs $OOC_JOBS"
    launch_runs $ooc -jobs $OOC_JOBS
    foreach r $ooc { wait_on_run $r }
    foreach r $ooc {
        if {[get_property PROGRESS [get_runs $r]] != "100%"} {
            error "OOC run $r failed: [get_property DIRECTORY [get_runs $r]]/runme.log"
        }
    }
} else {
    puts "@@@ v8t2 ooc: every IP run already complete"
}
if {[llength [v8_pending synth_1]]} {
    puts "@@@ v8t2 synth_1: launched at [clock format [clock seconds] -format %H:%M:%S]"
    launch_runs synth_1 -jobs $njobs
    wait_on_run synth_1
} else {
    puts "@@@ v8t2 synth_1: already complete"
}
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synthesis failed: $proj_dir/${design_name}.runs/synth_1/runme.log"
}
puts "@@@ v8t2 synth_1: done at [clock format [clock seconds] -format %H:%M:%S]"
if {[lsearch $argv noanalyze] < 0} { source $stages/70_analyze.tcl }
puts "@@@ v8t2: synthesis and analysis done."
