# multimesh v6: station-bus line, four meshes, per-module reconfigurable clocks.
#   vivado -mode batch -source scripts/tcl/multimesh_v6_bd.tcl ?-tclargs impl?
# Every knob is in scripts/tcl/v6/00_config.tcl and nowhere else.

set here [file dirname [file normalize [info script]]]
set do_impl  [expr {[lsearch $argv impl] >= 0}]
set do_synth [expr {$do_impl || [lsearch $argv synth] >= 0}]
set do_build [expr {[lsearch $argv rebuild] >= 0 || !$do_synth}]
set njobs 8
if {[set i [lsearch $argv jobs]] >= 0} { set njobs [lindex $argv [expr {$i + 1}]] }

source $here/v6/00_config.tcl
set_param general.maxThreads 16

# bd/mref/<mod>/component.xml is packaged ONCE and the RTL is never re-parsed --
# BEFORE open_project, because Vivado loads these definitions as it opens.
set v6_gens [list $BOARD_DIR/[file rootname [file tail $MAIN_XPR]].gen]
# A standalone/probe project keeps its own .gen across create_project -force.
if {$proj_dir ne $BOARD_DIR} { lappend v6_gens $proj_dir/${design_name}.gen }
set v6_dropped {}
foreach g $v6_gens {
    foreach m [concat [dict values $MESHES] {sb_bd_line4 ktpu_div2 mag_link_cdc}] {
        if {[file isdirectory $g/sources_1/bd/mref/$m]} {
            file delete -force $g/sources_1/bd/mref/$m
            lappend v6_dropped $m
        }
    }
}
puts "@@@ v6 mref: dropped [llength $v6_dropped] stale module-reference cache(s)"

# Building the BD costs ten minutes, so `synth` reopens what `bd` left behind.
# `rebuild` forces it from scratch.
if {!$do_build && [file exists $proj_dir/${design_name}.xpr]} {
    open_project $proj_dir/${design_name}.xpr
    if {[get_property top [current_fileset]] ne "${design_name}_wrapper"} {
        error "top is [get_property top [current_fileset]], not the BD wrapper"
    }
    puts "@@@ v6: reopened $proj_dir, top [get_property top [current_fileset]]"
    set v6_need_build 0
} elseif {[info exists ::env(V6_STANDALONE)]} {
    create_project -force $design_name $proj_dir -part $part
    set_property target_language Verilog [current_project]
    set v6_need_build 1
} else {
    # THE project, holding multimesh v2..v5 already. v6 is one more BD in it.
    if {![file exists $MAIN_XPR]} { error "no project at $MAIN_XPR" }
    open_project $MAIN_XPR
    set_property target_language Verilog [current_project]

    # A stale multimesh_v6.bd would be reused instead of rebuilt.
    set old [get_files -quiet ${design_name}.bd]
    if {[llength $old]} {
        set opened [get_bd_designs -quiet $design_name]
        if {[llength $opened]} { close_bd_design $opened }
        set olddir [file dirname $old]
        remove_files $old
        file delete -force $olddir
        puts "@@@ v6: removed the previous $design_name.bd"
    }
    set v6_need_build 1
}

# The body stays at global scope: 10_sources..70_analyze all read $root,
# $design_name, $MESHES and the rest as globals, and a proc would hide them.
if {$v6_need_build} {

source $here/v6/10_sources.tcl

create_bd_design $design_name
current_bd_design $design_name

source $here/v6/20_clocks.tcl
source $here/v6/30_meshes.tcl
source $here/v6/40_bus.tcl
source $here/v6/50_addr.tcl

regenerate_bd_layout
validate_bd_design
source $here/v6/55_addr_fill.tcl
validate_bd_design
save_bd_design

source $here/v6/60_constraints.tcl

# Without a wrapper AND a top, synth_design has nothing to elaborate.
# update_compile_order UNDOES make_wrapper -top, so the top is set after it.
set bdf [get_files ${design_name}.bd]
generate_target all $bdf
make_wrapper -files $bdf -top -import -force
update_compile_order -fileset sources_1
set_property top ${design_name}_wrapper [current_fileset]
# Auto-top re-runs and picks a bare mesh; place_design then dies on 3,610 I/O,
# one full synthesis later.
set_property top_auto_set 0 [current_fileset]
if {[get_property top [current_fileset]] ne "${design_name}_wrapper"} {
    error "top is [get_property top [current_fileset]], not the BD wrapper"
}
puts "@@@ v6 top: [get_property top [current_fileset]] in $proj_dir"

}

if {!$do_synth} {
    puts "@@@ v6: block design built. -tclargs synth to synthesise, impl to run."
    return
}

# OOC IP first and explicitly, so -jobs governs them: launch_runs synth_1 would
# pull them in as a dependency and the job count would not be ours to set.
set ooc [get_runs -quiet -filter {IS_SYNTHESIS && NAME != "synth_1"}]
if {[llength $ooc]} {
    puts "@@@ v6 ooc: [llength $ooc] runs, -jobs $njobs"
    launch_runs $ooc -jobs $njobs
    foreach r $ooc { wait_on_run $r }
    foreach r $ooc {
        if {[get_property PROGRESS [get_runs $r]] != "100%"} {
            error "OOC run $r failed: [get_property DIRECTORY [get_runs $r]]/runme.log"
        }
    }
}

launch_runs synth_1 -jobs $njobs
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synthesis failed: $proj_dir/${design_name}.runs/synth_1/runme.log"
}
source $here/v6/70_analyze.tcl

if {!$do_impl} {
    puts "@@@ v6: synthesis and analysis done. Run implementation in the GUI."
    return
}
launch_runs impl_1 -to_step route_design -jobs $njobs
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] != "100%"} {
    error "implementation failed: $proj_dir/${design_name}.runs/impl_1/runme.log"
}
puts "@@@ v6 done"
