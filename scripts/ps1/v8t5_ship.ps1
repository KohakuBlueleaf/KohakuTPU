# multimesh v8t5, start to bitstream, one Vivado at a time: block design ->
# verify (stops on a FAIL line) -> synthesis with the analysis -> impl through
# write_bitstream. Meant to run DETACHED (Start-Process); progress in
# scripts\ps1\probe_status.ps1 and the stage log below.
#
#   Start-Process pwsh -ArgumentList '-NoProfile','-File','scripts/ps1/v8t5_ship.ps1' -WindowStyle Hidden
$root = "C:/Users/apoll/Desktop/code/Project/KohakuTPU"
$viv  = "D:/Xilinx/Vivado/2024.2/bin/vivado.bat"
$L    = "C:/Users/apoll/Desktop/vivado"
$stage = "$L/multimesh_v8t5_ship.log"
Set-Location $root

function Mark([string]$s) { "$(Get-Date -Format 'HH:mm:ss')  $s" | Out-File $stage -Append -Encoding utf8 }

Mark "build: block design (rebuild)"
& $viv -mode batch -log "$L/multimesh_v8t5_build.log" -nojournal -notrace `
    -source scripts/tcl/multimesh_v8t5_bd.tcl -tclargs rebuild jobs 4 | Out-Null
if ($LASTEXITCODE -ne 0) { Mark "BUILD FAILED ($LASTEXITCODE): $L/multimesh_v8t5_build.log"; exit 1 }

Mark "verify: 75_verify_bd"
$v = & $viv -mode batch -log "$L/multimesh_v8t5_verify.log" -nojournal -notrace `
    -source scripts/tcl/v8t5_verify.tcl 2>&1
$fails = @($v | Select-String -Pattern '@@@ FAIL')
if ($fails.Count -gt 0 -or $LASTEXITCODE -ne 0) {
  Mark "VERIFY FAILED: $($fails.Count) FAIL line(s), exit $LASTEXITCODE -- $L/multimesh_v8t5_verify.log"
  exit 1
}
Mark "verify: clean"

Mark "synth: OOC module runs, synth_1, analysis"
& $viv -mode batch -log "$L/multimesh_v8t5/build.log" -nojournal -notrace `
    -source scripts/tcl/multimesh_v8t5_bd.tcl -tclargs synth jobs 4 analyze | Out-Null
if ($LASTEXITCODE -ne 0) { Mark "SYNTH FAILED ($LASTEXITCODE): $L/multimesh_v8t5/build.log"; exit 1 }

Mark "impl: through write_bitstream"
& $viv -mode batch -log "$L/multimesh_v8t5/impl.log" -nojournal -notrace `
    -source scripts/tcl/v8t5_impl.tcl | Out-Null
if ($LASTEXITCODE -ne 0) { Mark "IMPL FAILED ($LASTEXITCODE): $L/multimesh_v8t5/impl.log"; exit 1 }
Mark "SHIP DONE"
