"""The SRU row reducers (R1 max, R2 sum), as specified, on whole sub-rows.

A sub-row is a list of words; a word is 16 E8M15 values, lane 4r + c in
row r. Results are per row.

max: the largest word under key = s ? ~w[22:0] : {1, w[22:0]}.
sum: EM is the running max exponent field of the row, word by word; an
     element aligns as a = trunc((sig << 8) >> (EM - e)) (0 for e = 0 or
     past 23), signed; T is a word's four a; A = (A >> (EM_n - EM_n-1)) + T,
     an arithmetic shift on a 48-bit signed A. The value is A * 2^(EM - 150).
     special: 1 a NaN or both infinities, 2 +inf, 3 -inf, else 0.
"""

import e8


def key(w):
    return ((~w) & 0x7FFFFF) if w >> 23 else (0x800000 | (w & 0x7FFFFF))


def row_max(words, r):
    best = None
    for wd in words:
        for c in range(4):
            x = wd[4 * r + c]
            if best is None or key(x) > key(best):
                best = x
    return best


def row_sum(words, r):
    em = 0
    a_acc = 0
    nan = pinf = ninf = False
    for i, wd in enumerate(words):
        xs = [wd[4 * r + c] for c in range(4)]
        es = [e8.fields(x)[1] for x in xs]
        em_w = max(es)
        new = em_w if i == 0 else max(em, em_w)
        grow = 0 if i == 0 else new - em
        em = new
        t = 0
        for x in xs:
            s, e, m = e8.fields(x)
            if e == 255:
                if m:
                    nan = True
                elif s:
                    ninf = True
                else:
                    pinf = True
                continue
            d = em - e
            if e == 0 or d > 23:
                continue
            a = ((0x8000 | m) << 8) >> d
            t += -a if s else a
        prev = (
            0 if i == 0 else (a_acc >> grow if grow <= 47 else (-1 if a_acc < 0 else 0))
        )
        a_acc = prev + t
        a_acc = ((a_acc + (1 << 47)) & ((1 << 48) - 1)) - (1 << 47)
    sp = 1 if (nan or (pinf and ninf)) else 2 if pinf else 3 if ninf else 0
    return sp, em, a_acc
