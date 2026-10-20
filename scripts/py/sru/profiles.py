"""The SRU's predefined profiles: one RTL (sru_core / sru_cu parameters), the
knob values of each. The benches and OOC runs read this table.

HAS_MANT: MANT / HALF function modes on the RAM table (elementwise 1/x,
rsqrt, sqrt). FIN_RCP: lane 0's mantissa ROM, the finalise's RCP / RSQRT.
F16_IEEE: 1 IEEE FP16 in and out, 0 the clusters' accumulator form."""

PROFILES = {
    # the strongest: 16 lanes, function front ends and the RAM table on all
    "strong": dict(F_LANES=16, HAS_FN=1, TAB="ram", H_TAPS=2, HAS_R1=1, HAS_R2=1,
                   HAS_MX7=1, HAS_MASK=1, HAS_B=1, BUF_WORDS=512, HAS_MANT=1,
                   FIN_RCP=1, F16_IEEE=0),
    # 16 lanes, the exp2 ROM in place of the table: no table BRAM
    "rom": dict(F_LANES=16, HAS_FN=1, TAB="rom", H_TAPS=2, HAS_R1=1, HAS_R2=1,
                HAS_MX7=1, HAS_MASK=1, HAS_B=1, BUF_WORDS=512, HAS_MANT=0,
                FIN_RCP=1, F16_IEEE=0),
    # half the lanes, one result tap: every program at half rate
    "mid": dict(F_LANES=8, HAS_FN=1, TAB="ram", H_TAPS=1, HAS_R1=1, HAS_R2=1,
                HAS_MX7=1, HAS_MASK=1, HAS_B=1, BUF_WORDS=512, HAS_MANT=1,
                FIN_RCP=1, F16_IEEE=0),
    # FMA programs only
    "spu": dict(F_LANES=16, HAS_FN=0, TAB="ram", H_TAPS=2, HAS_R1=1, HAS_R2=1,
                HAS_MX7=1, HAS_MASK=1, HAS_B=1, BUF_WORDS=512, HAS_MANT=0,
                FIN_RCP=1, F16_IEEE=0),
    # the format unit alone: no pool
    "pack": dict(F_LANES=0, HAS_FN=0, TAB="ram", H_TAPS=1, HAS_R1=0, HAS_R2=0,
                 HAS_MX7=1, HAS_MASK=0, HAS_B=0, BUF_WORDS=0, HAS_MANT=0,
                 FIN_RCP=0, F16_IEEE=0),
}

#: Variants measured beside the profiles: one knob off at a time (OOC deltas).
VARIANTS = {
    "strong_nomant": dict(PROFILES["strong"], HAS_MANT=0),
    "strong_norcp": dict(PROFILES["strong"], FIN_RCP=0),
    "strong_ieee": dict(PROFILES["strong"], F16_IEEE=1),
}


def knobs(p):
    return PROFILES[p] if p in PROFILES else VARIANTS[p]


def features(p):
    """What a profile can run, in the job `needs` vocabulary of core_vectors."""
    k = knobs(p)
    has = {"MX7"} if k["HAS_MX7"] else set()
    if k["F_LANES"]:
        has.add("POOL")
        if k["H_TAPS"] > 1:
            has.add("H2")
        if k["FIN_RCP"]:
            has.add("FIN_RCP")
    if k["HAS_FN"]:
        has |= {"FN_EXP"} | ({"FN_FIX"} if k["TAB"] == "ram" else set())
        if k["HAS_MANT"] and k["TAB"] == "ram":
            has.add("FN_MANT")
    for knob, feat in (("HAS_R1", "R1"), ("HAS_R2", "R2"), ("HAS_MASK", "MASK"),
                       ("HAS_B", "B")):
        if k[knob]:
            has.add(feat)
    if k["BUF_WORDS"]:
        has.add("BUF")
    return has


def defines(p):
    """xsim -d defines selecting the profile's parameters in the benches: the
    knobs by value, TAB as the flag SRU_TAB_ROM (a quoted string does not
    survive the xvlog batch file), the profile as its index (SRU_TB_PROF)."""
    k = knobs(p)
    out = [f"SRU_TB_PROF={list(PROFILES).index(p) if p in PROFILES else 0}"]
    out += [f"SRU_{n}={v}" for n, v in k.items() if n != "TAB"]
    if k["TAB"] == "rom":
        out.append("SRU_TAB_ROM")
    return out
