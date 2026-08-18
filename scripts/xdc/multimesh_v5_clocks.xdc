# Asynchronous clock groups for multimesh v5 with V5_PER_MESH_CLK=1.

# FOR THE PER-MESH BUILD ONLY. At V5_PER_MESH_CLK=0 there is one generator named
# `clk_wiz_mesh` and every group below matches nothing; use v3's file there.

# NO proc / foreach / if. Vivado parses XDC in a restricted mode and rejects
# control flow as a CRITICAL WARNING, so the block is silently skipped and every
# crossing gets timed -- docs/workflow/tooling-traps.md.

# WHY THIS FILE EXISTS RATHER THAN AN EDIT TO v3's. That one globs
# *clk_wiz_mesh*clk_out1, which here matches all four generators and puts them in
# ONE group -- declaring them mutually synchronous, so every mesh-to-mesh path
# gets timed and route_design spends its life on crossings that do not exist.

# ONE GROUP PER GENERATOR, and clk_out* not clk_out1: a unit clock is VCO/k off
# the SAME VCO, so it is related to its own mesh and must share that group.

# -include_generated_clocks IS LOAD BEARING ON A PUMPED BUILD. There the mesh
# fabric 1x is not clk_out1 at all: ktpu_div2's BUFGCE_DIV halves clk_out2, and
# that derived clock is its own object which `-of_objects ... clk_out*` does not
# match. Ungrouped, it is treated as related to every other mesh and each
# crossing gets timed -- the very thing this file exists to stop.

# THE GATED UNIT CLOCKS NEED NOTHING. A BUFGCE removes edges and creates no
# clock object, so a gated compute unit is timed on its own mesh's clock.

# The six mag_link_cdc crossings live between the mesh groups below and are
# therefore untimed here; xpm_fifo_async carries its own scoped CDC constraints.

# BACKBONE, or the placer REFUSES: one reference feeds FIVE MMCMs (ctrl plus one
# per mesh) across all four SLRs, and rule_bufg_mmcm_3loads caps a BUFGCE at 3.
# Safe here precisely because the groups below declare the meshes mutually
# asynchronous -- each MMCM regenerates its own clock and no timing arc depends
# on reference skew between SLRs. Per-mesh clocks REQUIRE this.
# ANY_CMT_COLUMN, not BACKBONE: on UltraScale BACKBONE means SAME_CMT_COLUMN and
# pins every MMCM to one column; UG949 gives this one for DIFFERENT clock regions.
# NAMED BY A LOAD, NOT THE DRIVER. Three attempts at the driver matched nothing
# (12-507, 17-55) -- an auto-inserted BUFG has no name until opt_design. clk_in1
# is an IP boundary pin and always exists, and with the explicit util_ds_buf_bufg
# in front of it the net it names IS the buffer's output.
set_property CLOCK_DEDICATED_ROUTE ANY_CMT_COLUMN \
    [get_nets -of [get_pins multimesh_v5_i/clk_wiz_ctrl/clk_in1]]

set_clock_groups -asynchronous \
    -group [get_clocks -include_generated_clocks -of_objects [get_pins -hier -filter {NAME =~ *clk_wiz_ctrl*clk_out1}]] \
    -group [get_clocks -include_generated_clocks -of_objects [get_pins -hier -filter {NAME =~ *clk_wiz_mesh_0*clk_out*}]] \
    -group [get_clocks -include_generated_clocks -of_objects [get_pins -hier -filter {NAME =~ *clk_wiz_mesh_1*clk_out*}]] \
    -group [get_clocks -include_generated_clocks -of_objects [get_pins -hier -filter {NAME =~ *clk_wiz_mesh_2*clk_out*}]] \
    -group [get_clocks -include_generated_clocks -of_objects [get_pins -hier -filter {NAME =~ *clk_wiz_mesh_3*clk_out*}]] \
    -group [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *ddr4_0*c0_ddr4_ui_clk}]] \
    -group [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *ddr4_1*c0_ddr4_ui_clk}]] \
    -group [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *ddr4_2*c0_ddr4_ui_clk}]] \
    -group [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *ddr4_3*c0_ddr4_ui_clk}]] \
    -group [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *xdma_0*axi_aclk}]]
