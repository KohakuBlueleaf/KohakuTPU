# multimesh v8: the one-SLR probe -- four stations, four nodes on ONE sysnode
# clock, ONE Kohaku Xache, meshes 2+2 / 6+2 / 2+2 / 2+2.
#   vivado -mode batch -source scripts/tcl/multimesh_v8_bd.tcl \
#     ?-tclargs rebuild|synth|impl ?route? ?jobs N??
# Every knob is in scripts/tcl/v8/00_config.tcl and nowhere else. Its own
# project, always; the v6/v7 flows and their projects are untouched.

set here [file dirname [file normalize [info script]]]
set do_impl  [expr {[lsearch $argv impl] >= 0}]
set do_synth [expr {$do_impl || [lsearch $argv synth] >= 0}]
set do_build [expr {[lsearch $argv rebuild] >= 0 || !$do_synth}]
set njobs 4
if {[set i [lsearch $argv jobs]] >= 0} { set njobs [lindex $argv [expr {$i + 1}]] }
set impl_step [expr {[lsearch $argv route] >= 0 ? "route_design" : "write_bitstream"}]

source $here/v8/00_config.tcl
set_param general.maxThreads 16

# bd/mref/<mod>/component.xml is packaged ONCE and the RTL never re-parsed:
# drop every cached module reference BEFORE the project opens -- but only on
# a rebuild. Dropping them before a plain reopen forces a repackage on open,
# which is where the v7t 03:31 run died silently.
set v8_dropped {}
set g $proj_dir/${design_name}.gen
if {$do_build} {
    foreach m [concat [dict values $MESHES] {sb_bd_line4 ktpu_div2 kx_bd_4x4 mag_link_pipe_bd xcvu13p_rst_tree}] {
        if {[file isdirectory $g/sources_1/bd/mref/$m]} {
            file delete -force $g/sources_1/bd/mref/$m
            lappend v8_dropped $m
        }
    }
}
puts "@@@ v8 mref: dropped [llength $v8_dropped] stale module-reference cache(s)"

if {!$do_build && [file exists $proj_dir/${design_name}.xpr]} {
    open_project $proj_dir/${design_name}.xpr
    if {[get_property top [current_fileset]] ne "${design_name}_wrapper"} {
        error "top is [get_property top [current_fileset]], not the BD wrapper"
    }
    puts "@@@ v8: reopened $proj_dir, top [get_property top [current_fileset]]"
    set v8_need_build 0
} else {
    # ONE SHOT: a rebuild starts from nothing, so no stale file survives.
    if {$do_build && [file isdirectory $proj_dir]} {
        file delete -force $proj_dir
        puts "@@@ v8: wiped $proj_dir"
    }
    create_project -force $design_name $proj_dir -part $part
    set_property target_language Verilog [current_project]
    set v8_need_build 1
}

if {$v8_need_build} {

source $here/v8/10_sources.tcl

create_bd_design $design_name
current_bd_design $design_name

source $here/v8/20_clocks.tcl
source $here/v8/30_meshes.tcl
source $here/v8/35_xache.tcl
source $here/v8/40_bus.tcl
source $here/v8/50_addr.tcl

regenerate_bd_layout
validate_bd_design
source $here/v8/55_addr_fill.tcl
validate_bd_design
save_bd_design

source $here/v8/60_constraints.tcl

set bdf [get_files ${design_name}.bd]
generate_target all $bdf
make_wrapper -files $bdf -top -import -force
update_compile_order -fileset sources_1
set_property top ${design_name}_wrapper [current_fileset]
set_property top_auto_set 0 [current_fileset]
if {[get_property top [current_fileset]] ne "${design_name}_wrapper"} {
    error "top is [get_property top [current_fileset]], not the BD wrapper"
}
puts "@@@ v8 top: [get_property top [current_fileset]] in $proj_dir"

}

if {!$do_synth} {
    puts "@@@ v8: block design built. Verify with v8/75_verify_bd.tcl; -tclargs synth to synthesise."
    return
}

# OOC IP first and explicitly, at OOC_JOBS: four meshes synthesising at once
# took 141 GB of free memory to 2.8 GB.
# IP runs do not carry IS_SYNTHESIS; match them by name. (When this list is
# empty, launch_runs synth_1 -jobs N dispatches them itself, N at a time.)
set ooc [get_runs -quiet -filter {NAME =~ "*_synth_1" && NAME != "synth_1"}]
if {[llength $ooc]} {
    puts "@@@ v8 ooc: [llength $ooc] runs, -jobs $OOC_JOBS"
    launch_runs $ooc -jobs $OOC_JOBS
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
# Analysis BEFORE implementation is dispatched: one shot means nothing is
# placed until every check has passed.
source $here/v8/70_analyze.tcl

if {!$do_impl} {
    puts "@@@ v8: synthesis and analysis done. -tclargs impl to implement."
    return
}
puts "@@@ v8: dispatching impl_1 -to_step $impl_step, -jobs $njobs"
launch_runs impl_1 -to_step $impl_step -jobs $njobs
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] != "100%"} {
    error "impl failed: $proj_dir/${design_name}.runs/impl_1/runme.log"
}
puts "@@@ v8 impl done ($impl_step)"
