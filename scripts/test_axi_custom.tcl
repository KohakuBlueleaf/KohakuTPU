create_hw_axi_txn wr_txn [get_hw_axis hw_axi_1] -address 00000000 -data {11111111_22222222_33333333_44444444_55555555_66666666_77777777_88888888} -len 4 -type write
create_hw_axi_txn rd_txn [get_hw_axis hw_axi_1] -address 00000000 -len 4 -type read

run_hw_axi wr_txn
run_hw_axi rd_txn

delete_hw_axi_txn wr_txn
delete_hw_axi_txn rd_txn
