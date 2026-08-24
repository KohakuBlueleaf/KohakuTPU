# Retired runners

Seven PowerShell runners that predate `scripts/py/xsim.py` and did the same job
per subsystem. **They are dead, not deprecated**: every one of them cites
`src/kohakumas/` and `src/synth_top/`, and neither directory has existed since
those trees became `src/kohakuaccel/sysnode/` and
`src/kohakutpu/top/generated/`. They fail before reaching a simulator.

They are kept rather than deleted because their per-subsystem groupings record
which modules were once thought to belong together, which is occasionally useful
history. Nothing should call them.

`docs/workflow/simulate.md` predicted exactly how they would die:

> They each keep their **own copy** of the source list, and that duplication is
> exactly how they break: a module gains a dependency, the shared table learns
> about it, one runner's private copy does not, and that runner fails
> elaboration on an unresolved module while everything else keeps working.

The rename finished the job the drift started. **One source list per bench, in
one place** — `scripts/py/xsim.py`.
