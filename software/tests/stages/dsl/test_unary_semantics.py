from kohakutpu.language.text.syntax import (
    BinOp,
    Compare,
    Int,
    Name,
    Neg,
    Not,
    Offset,
    Pos,
    fmt,
    parse,
)


def value(term, env):
    match term:
        case Name(name):
            return env[name]
        case Int(number):
            return number
        case Neg(x):
            return -value(x, env)
        case Not(x):
            return int(not value(x, env))
        case Pos(x):
            return value(x, env)
        case Offset(a, b) | BinOp("+", a, b):
            return value(a, env) + value(b, env)
        case BinOp("*", a, b):
            return value(a, env) * value(b, env)
        case Compare("<", a, b):
            return int(value(a, env) < value(b, env))
    raise AssertionError(f"unexpected expression {term!r}")


def test_unary_operands_keep_their_value():
    env = {"a": 2, "b": 3, "c": 7}
    a, b, c = Name("a"), Name("b"), Name("c")
    total = BinOp("+", a, b)
    for term, expected in [
        (Neg(total), -5),
        (Not(total), 0),
        (BinOp("*", Pos(Compare("<", a, b)), c), 7),
        (BinOp("*", Pos(total), c), 35),
    ]:
        executable = parse(f"value {fmt(term)}")[0].args[0]
        assert value(executable, env) == expected
