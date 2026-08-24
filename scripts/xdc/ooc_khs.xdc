# OOC constraints for the KohakuSIMD vector unit.
#
# ONE CLOCK. The unit lives on the PE's clock, which lives on noc_clk, and a
# second clock here would mean something had gone wrong.
#
# THE PERIOD IS THE CALLER'S AND THE CALLER MUST SET IT. The base core's
# measurement settled on 3.333 ns -- where the PE reaches its ceiling, asking for
# less buys LUT rather than megahertz -- but that number lives in the callers now.
#
# NO `if` HERE. Vivado's XDC reader rejects it outright ("Command 'if' is not
# supported in the xdc constraint file", Designutils 20-1307) and raises a
# CRITICAL WARNING on every run, so the default that used to stand here never
# supplied anything: it worked only because all seven callers set ::ooc_period
# unconditionally. A caller that forgets now fails loudly on the next line
# instead of being silently constrained at some other period. ooc_khg.xdc and
# ooc_rv_pe.xdc carry the same note for the same reason.

create_clock -name noc_clk -period $::ooc_period [get_ports clk]
