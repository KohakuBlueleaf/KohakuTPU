# Verilog index over $root/src, shared by check_*.tcl. PAR/POR/SRC/INS map module
# -> parameters, ports, defining files (poc/ shadows three, so a list), instances.

set PAR [dict create] ; set POR [dict create]
set SRC [dict create] ; set INS [dict create]

# Names that open a paren but instantiate nothing.
set RTL_KW {module endmodule always assign initial generate begin end if else
            case casez casex endcase for while repeat forever function task
            parameter localparam wire reg input output inout integer genvar
            defparam and or not nand nor xor xnor buf bufif0 bufif1 posedge
            negedge return disable fork join specify endspecify}

proc rtl_scan {path} {
    global PAR POR SRC INS RTL_KW
    set fh [open $path r] ; set txt [read $fh] ; close $fh
    regsub -all {/\*.*?\*/} $txt "" txt
    set mod "" ; set dup 0
    foreach line [split $txt "\n"] {
        regsub {//.*$} $line "" line
        if {[regexp {^\s*module\s+([A-Za-z_]\w*)} $line -> nm]} {
            set mod $nm
            set dup [dict exists $PAR $mod]
            if {!$dup} { dict set PAR $mod {} ; dict set POR $mod {} ; dict set INS $mod {} }
            dict lappend SRC $mod $path
        }
        if {$mod eq "" || $dup} { continue }
        if {[regexp {parameter\s+(?:integer\s+|real\s+|signed\s+)?(?:\[[^\]]*\]\s*)?([A-Za-z_]\w*)\s*=} \
                    $line -> p]} {
            dict lappend PAR $mod $p
        }
        if {[regexp {^\s*(?:input|output|inout)\s+(?:wire\s+|reg\s+)?(?:signed\s+)?(?:\[[^\]]*\]\s*)?([A-Za-z_]\w*)\s*,?\s*$} \
                    $line -> s]} {
            dict lappend POR $mod $s
        }
        # `Name inst ();` with no ports is the elaboration-abort idiom
        # (sb_nsu.v:105), not a module anyone has to provide.
        if {[regexp {\(\s*\)\s*;} $line]} { continue }
        if {[regexp {^\s*([A-Za-z_]\w*)\s+(?:#\s*\(|[A-Za-z_]\w*\s*\()} $line -> r]} {
            if {[lsearch -exact $RTL_KW $r] < 0 && $r ne $mod} {
                if {[lsearch -exact [dict get $INS $mod] $r] < 0} { dict lappend INS $mod $r }
            }
        }
    }
}

proc rtl_walk {dir} {
    global rtl_files
    foreach f [glob -nocomplain -directory $dir -types f *.v] { lappend rtl_files $f }
    foreach d [glob -nocomplain -directory $dir -types d *] { rtl_walk $d }
}

set rtl_files {} ; rtl_walk $root/src
foreach f $rtl_files { rtl_scan $f }
