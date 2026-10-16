# multimesh v9t5 -- v9t4's dies and floorplan with the system node's and the
# vector core's handshakes cut, register slices on station 1's managers,
# placement replication by net property, and the implementation directive variant
# named by IMPL_DIRS. v9t4 stays the hardware reference: every knob below is
# off in it.

set here9t5 [file dirname [file normalize [info script]]]
source [file dirname $here9t5]/v9t4/00_config.tcl

set design_name multimesh_v9t5
set proj_dir    C:/Users/apoll/Desktop/vivado/multimesh_v9t5

# mag.v's and v2_core.v's cuts (v9t4 routed: the port output, write-queue
# head, interlink tx, mover write and port AR handshakes and the math queue
# head, 6 to 16 logic levels each).
set RTL_DEFINES {
    KOHAKU_PORT_OSKID=1
    KOHAKU_PORT_LEAN=1
    KOHAKU_PORT_HCOPY=1
    KOHAKU_PORT_ARSKID=1
    KOHAKU_IL_TXSKID=1
    KOHAKU_IL_MVSKID=1
    V2_MQ_SKID=1
}

# dwc_xdma -> S01 measured -1.43 ns at 16 levels on v9t4; JTAG takes the same.
set SB_MGR_RS 1

# Die crossings (v8t3/00_config.tcl): Laguna flops on every Xache trunk, each
# trunk's landing rings beside its boundary, and the sysnode/DDR UI crossings
# held to a datapath and gray-bus skew bound instead of cut.
set TRUNK_SLL_REG  0
set TRUNK_RING_PIN 1
set TRUNK_HOLD_NS  0
set TRUNK_RING_SIDE tx
set CDC_DIE_SPLIT  1
set IL_MEM         distributed
set XING_TIMED     1

# default | timing | congestion (v8t3/00_config.tcl IMPL_VARIANTS).
set IMPL_DIRS default

# Registers whose output nets place_design replicates per clock region of
# their loads (v8t3/00_config.tcl REPL_NETS). v9t4 routed: the mover's
# write valid and the interlink's write FSM (576 loads each), the agent's and
# the interlink's destination row and valid (the hub's port decode, into the
# engines' e_w* at 1,024 loads), each TCU's skew-line last stage and each
# cluster core's valid line (into the DSP columns and the partial registers).
set REPL_NETS {
    {.*/u_pe/u_mover/wv_r_reg}
    {.*/g_ilink\.u_il/FSM_onehot_ws_reg\[\d+\]}
    {.*/u_mag/u_agent/noc_out_data_reg\[28[0-3]\]}
    {.*/u_mag/u_agent/noc_out_valid_reg}
    {.*/g_ilink\.u_il/inj_data_reg\[28[0-3]\]}
    {.*/g_ilink\.u_il/inj_valid_reg}
    {.*\.u_tcu/g_skew\[1\]\.g_delay\.sr_reg\[0\]\[\d+\]}
    {.*\.u_tcu/g_skew\[2\]\.g_delay\.sr_reg\[1\]\[\d+\]}
    {.*\.u_tcu/g_skew\[3\]\.g_delay\.sr_reg\[2\]\[\d+\]}
    {.*\.u_tcu/g_skew\[4\]\.g_delay\.sr_reg\[3\]\[\d+\]}
    {.*\.u_tcu/g_skew\[5\]\.g_delay\.sr_reg\[4\]\[\d+\]}
    {.*\.u_tcu/g_skew\[6\]\.g_delay\.sr_reg\[5\]\[\d+\]}
    {.*\.u_tcu/g_skew\[7\]\.g_delay\.sr_reg\[6\]\[\d+\]}
    {.*/u_cu\d+/g_p2\.u_node/u_core/vld_sr_reg\[\d+\]}
}

# ---- the per-item report (scripts/py/timing/items.py) ----------------------
# Each item: {name start-regexp end-regexp}, Python `re.search` over a census
# class's start and end register classes; "" matches any. A failing class
# counts in every item it matches. The report sets each item's TNS, failing
# endpoints, worst slack, its levels and route share, placed and routed, here
# against REPORT_BASE.
set REPORT_BASE v9t4
set REPORT_ITEMS {
    {B1_port_out   {}
                   {/g_port\[\d+\]\.(u_eng/(mem_out_data|e_w\d|p_w\d|u_rskid/)|g_oskid\.)}}
    {B2_wq_head    {/g_port\[\d+\]\.u_eng/(u_wrq|g_wq_hd\.u_wrq_hd)/} {}}
    {B2_ackq_head  {/g_port\[\d+\]\.u_eng/(g_ackq(_hd)?\.)?u_ackq/} {}}
    {B3_il_tx      {/g_ilink\.u_sw/} {/g_ilink\.(u_il|g_txskid)/}}
    {B4_mover_w    {/u_pe/u_mover/} {/u_mag/u_mag/(u_dram|g_ilink)/}}
    {B5_port_ar    {/u_mag/u_mag/u_dram/} {/g_port\[\d+\]\.(u_eng|g_arskid)/}}
    {B5_ar_issue   {/g_port\[\d+\]\.u_eng/ar_} {/g_port\[\d+\]\.u_eng/(ar_|m_ar)}}
    {V_mq_head     {/u_vec\w*/u_core/(u_mq|g_mq_skid)} {/u_vec\w*/u_core/}}
    {P2_wv_r       {/u_pe/u_mover/wv_r} {}}
    {P2_il_ws      {/g_ilink\.u_il/FSM_onehot_ws} {}}
    {P2_hub_dest   {/(u_agent/noc_out_(data|valid)|g_ilink\.u_il/inj_(data|valid))} {}}
    {P2_skew_last  {\.u_tcu/g_skew\[\d+\]\.g_delay\.sr} {}}
    {P2_vld_sr     {/g_p2\.u_node/u_core/vld_sr} {}}
    {P5_xdma_s01   {/(dwc_xdma|rs_xdma|xdma_0)/} {/station_bus/}}
    {P5_jtag_s00   {/(jtag_ctrl|rs_jtag)/} {/station_bus/}}
}
