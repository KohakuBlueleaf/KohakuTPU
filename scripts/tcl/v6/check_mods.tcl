# tclsh scripts/tcl/v6/check_mods.tcl -- every cell/pin and CONFIG.x the v6 Tcl
# names, against the RTL that declares it. A wrong name is a failed BD build.

set here [file dirname [file normalize [info script]]]
source $here/00_config.tcl

source $here/rtl_index.tcl

# The four `create_bd_cell -type module` references. Everything else is Xilinx
# IP, whose pins only Vivado can confirm.
# cdc_?_to_? only: cdc_ready_cat and cdc_ready_all are Xilinx IP, not link CDCs.
set OWN [list mesh_? [lindex $MESHES 1] div2_* ktpu_div2 cdc_?_to_? mag_link_cdc \
              station_bus sb_bd_line4 {$name} mag_link_cdc]
proc owner {cell} {
    global OWN
    foreach {pat mod} $OWN { if {[string match $pat $cell]} { return $mod } }
    return ""
}

proc slurp {path} {
    set fh [open $path r] ; set t [read $fh] ; close $fh
    return [regsub -all {\\\n\s*} $t " "]
}

set bad 0 ; set seen [dict create] ; set nchk 0
foreach base {30_meshes.tcl 40_bus.tcl 50_addr.tcl} {
    set txt [slurp $here/$base]

    # cell/pin, die-index variables resolved to 0: the index picks the cell,
    # never the module, so one value settles the pin name for all four.
    foreach {all cell pin} [regexp -all -inline \
              {([A-Za-z_][A-Za-z0-9_$\{\}]*)/([A-Za-z_][A-Za-z0-9_$\{\}]*)} $txt] {
        foreach v {mid lo hi ddr s i} {
            regsub -all "\\\$$v\\M" $cell 0 cell ; regsub -all "\\\$\{$v\}" $cell 0 cell
            regsub -all "\\\$$v\\M" $pin  0 pin  ; regsub -all "\\\$\{$v\}" $pin  0 pin
        }
        set mod [owner $cell]
        if {$mod eq "" || [string first {$} $pin] >= 0} { continue }
        if {![dict exists $POR $mod]} { puts "  no RTL for module $mod" ; incr bad ; continue }
        if {[dict exists $seen $mod/$pin]} { continue }
        dict set seen $mod/$pin 1 ; incr nchk
        set ports [dict get $POR $mod]
        set ok [expr {[lsearch -exact $ports $pin] >= 0}]
        if {!$ok} {
            foreach p $ports { if {[string match ${pin}_* $p]} { set ok 1 ; break } }
        }
        if {!$ok} {
            puts "  $base: $cell/$pin -- $mod has no such port or interface"
            incr bad
        }
    }

    # CONFIG.x, attributed by the cell named in the same statement.
    foreach line [split $txt "\n"] {
        if {![string match *CONFIG.* $line]} { continue }
        set cell ""
        if {[string first {$bus} $line] >= 0 || [string first station_bus $line] >= 0} {
            set cell station_bus
        } elseif {[regexp {get_bd_cells\s+mesh_} $line]} {
            set cell mesh_0
        }
        if {$cell eq ""} { continue }
        set mod [owner $cell]
        foreach {all p} [regexp -all -inline {CONFIG\.([A-Za-z_]\w*)} $line] {
            incr nchk
            if {[lsearch -exact [dict get $PAR $mod] $p] < 0} {
                puts "  $base: CONFIG.$p on $cell -- $mod declares no such parameter"
                incr bad
            }
        }
    }

    # A guarded knob that names nothing is SKIPPED, not applied: silent default.
    foreach {all cell p} [regexp -all -inline {v6_set_if\s+(\S+)\s+(\w+)} $txt] {
        set mod [owner [regsub {\$\w+} $cell 0]]
        if {$mod eq ""} { continue }
        incr nchk
        if {[lsearch -exact [dict get $PAR $mod] $p] < 0} {
            puts "  $base: v6_set_if $p -- $mod declares no such parameter"
            incr bad
        }
    }
}

puts "[llength $rtl_files] RTL files, [dict size $POR] modules, $nchk names checked"
if {$bad} { error "$bad name(s) the RTL does not declare -- fix before building the BD" }
puts "@@@ mods ok: every pin and parameter the v6 Tcl names exists in the RTL"
