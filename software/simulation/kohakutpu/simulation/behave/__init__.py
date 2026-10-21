"""KohakuTPU's behaviour model: a card's packages run for values, with no
cycles -- the node's steps by the firmware's semantics
(`kohakuaccel.simulation.package.Interpreter`), each unit by a model of what
it computes (`mover`, ...). A unit with no model yet is refused by name
(`card.Unmodelled`)."""
