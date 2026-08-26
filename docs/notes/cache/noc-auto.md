---
title: Auto-caching routers
summary: Routers that snoop memory traffic and answer later requests locally — the only option that changes what the mesh is, and the only one that risks deadlock.
tags:
  - notes
  - memory
  - mesh
  - research
---

# Mesh auto-caching: routers that remember

Status: **PROPOSAL — nothing described on this page is built, and it is not
recommended first**, for the reasons in §4. No router in this tree caches
anything, and no figure here for what it would cost is a measurement. Read
[notes/README](../README.md) before treating any of it as a description of
hardware.

Routers snoop memory traffic passing through them and answer later requests
locally. This is the only option here that changes what the mesh *is*, and the
only one that risks deadlock.

## 1. The idea

A read response flowing from the memory agent to a compute unit passes through
routers on the way. If a router keeps a copy, the next request for the same
address that reaches that router can be answered on the spot -- shorter latency,
and the request never reaches the agent or DRAM.

For a machine where many compute units read the *same* weights, that is exactly
the traffic pattern a router sees.

## 2. Auto vs staging, inside the router

**Staging (explicit).** The router owns an addressable buffer in the reserved L2
range. Software places data there; a request in that range is served locally. No
tags, no replacement, deterministic.

But note what this actually is: a scratchpad that happens to live inside a router.
It has the same semantics as [noc-staging](noc-staging.md) with worse placement
freedom, because now the storage must sit next to a router rather than next to a
URAM column. **Staging-inside-router is dominated by staging-as-endpoint.** If the
storage is explicit, put it on a local port.

**Auto (transparent).** The router inspects flits and populates itself. This is
the only version that justifies touching the router, because it is the only one
that does something an endpoint cannot: act on traffic *in flight*, without anyone
addressing it.

So the real question is not "auto or staging" -- it is "auto, or nothing".

## 3. What auto-caching would have to get right

**Tag lookup on the router's critical path.** The router is MEASURED at >= 450 MHz
(2.5 ns, +0.278 ns, 7 logic levels). A tag compare in the forwarding path eats
that margin, and the router is instanced many times at **3,281 LUT apiece** -- so
both the frequency and the area cost multiply.

Mitigation: keep the lookup off the forwarding path entirely -- snoop responses
into the cache, but check the tag only on the *local* port, not on through-traffic.
That makes it a local-port cache, which is much closer to
[noc-staging](noc-staging.md) again.

**Deadlock.** The mesh's freedom from deadlock rests on its routing discipline. A
router that *generates* a response changes the traffic pattern: a request flit is
consumed and a response flit is injected, at a point that was previously a pure
forwarder. Any proof of deadlock freedom has to be redone, and the retry-based
flow control assumes the sender retries against a receiver that is not also a
source.

**Coherence between routers.** Two routers can cache the same line. Under the
read-only discipline of [mag-staging](mag-staging.md) §1 that is harmless --
operands do not change during a pass -- but the invalidation at a pass boundary
now has to reach every router, not one store. That is a broadcast the mesh has no
mechanism for today.

**Instruction changes.** This needs them: at minimum a range invalidate, and
probably a "do not cache this" hint for streamed activations that will never be
re-read and would only evict weights.

## 4. Why not first

**Shared fetch already does the useful part.** A fill descriptor names up to three
other compute units sharing an operand; the lowest-numbered issues one descriptor
and the agent multicasts to all of them. The dominant
same-address-many-readers case is *already* one DRAM read -- once the rendezvous
that mechanism still needs is built
([projects/kohakutpu/isa.md](../../projects/kohakutpu/isa.md) §3).

An auto-cache would capture the cases shared fetch does not: readers the compiler
did not group, and temporal reuse across passes. Both are real, but neither has
been measured, and the mechanism is expensive enough that it should be justified
by a number rather than by plausibility.

**What to measure first:** on a real workload, how many DRAM reads are for an
address some compute unit already read this pass, *excluding* those shared fetch
already merged? If that number is small, auto-caching has nothing to catch.

## 5. If it were built anyway

The least-risk shape, in order of increasing ambition:

1. **Local-port cache only.** Tag check on the local port; through-traffic
   untouched. Router frequency preserved. Effectively
   [noc-staging](noc-staging.md) with automatic population.
2. **Snoop-fill, explicit-hit.** Router populates automatically from responses it
   forwards, but only answers requests in the reserved L2 range. Keeps the address
   decode explicit, so no aliasing with DRAM addresses.
3. **Full transparent cache.** Tag check on all traffic. Everything in §3 applies.

Stage 2 is the interesting midpoint: automatic *population* with explicit
*addressing*. It gets the "router remembers what went past" benefit while leaving
the address map unambiguous and requiring no coherence protocol beyond range
invalidation.

## 6. The honest case for it

It would make this interconnect genuinely different. A mesh whose routers hold the
weight working set turns multicast from a compiler-scheduled event into a property
of the fabric -- and for transformer weights read by every cluster in a column,
that is the right shape.

That is a real architectural idea and worth keeping. It is also the option most
likely to cost a rebuild of the mesh's correctness argument, so it should follow
the measurement in §4, not precede it.
