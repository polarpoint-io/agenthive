#!/usr/bin/env python3
"""
Where are the tokens actually going?

Reads a running AgentHive's /metrics and answers the question the
dashboard does not: is this costing you more than it saves?

    python3 scripts/diagnose_tokens.py http://127.0.0.1:8790

Deliberately willing to conclude "this is costing you tokens".
agenthive_tokens_avoided_total compares what was served against dumping
the whole team graph, which nobody would ever do - so that number can
look excellent while real spend is flat. The honest baseline is not
running AgentHive at all, and against that baseline memory ADDS input
tokens and has to earn them back in work avoided.
"""

import sys
import urllib.request

# agenthive-mcp ships nine tool definitions with every request. Measured
# from its server.py docstrings, not estimated.
MCP_TOOL_OVERHEAD = 1130


def scrape(url: str) -> dict:
    with urllib.request.urlopen(url.rstrip("/") + "/metrics", timeout=15) as r:
        text = r.read().decode()
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or " " not in line:
            continue
        name, _, value = line.rpartition(" ")
        try:
            out[name.strip()] = float(value)
        except ValueError:
            pass
    return out


def g(m, name, **labels):
    if labels:
        key = name + "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
        return m.get(key, 0.0)
    return m.get(name, 0.0)


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    m = scrape(argv[0])

    full = g(m, "agenthive_retrievals_by_mode_total", mode="full")
    index = g(m, "agenthive_retrievals_by_mode_total", mode="index")
    retrievals = full + index
    served = g(m, "agenthive_tokens_served_total")
    withheld = g(m, "agenthive_index_tokens_withheld_total")
    refetched = g(m, "agenthive_fetch_tokens_served_total")
    empty = g(m, "agenthive_retrieval_empty_total")
    hits = g(m, "agenthive_retrieval_hits_total")
    turns_sum = g(m, "agenthive_session_turns_sum")
    turns_n = g(m, "agenthive_session_turns_count")

    print("=" * 68)
    print("WHERE THE TOKENS WENT")
    print("=" * 68)
    if not retrievals:
        print("\nNo retrievals recorded. Nothing is using this yet.")
        return 0

    mean_turns = (turns_sum / turns_n) if turns_n else 0
    print(f"\n  Retrievals          {retrievals:,.0f}  "
          f"({index:,.0f} index, {full:,.0f} full)")
    print(f"  Tokens served       {served:,.0f}")
    print(f"  Empty retrievals    {empty:,.0f} of {empty + hits:,.0f}")
    if turns_n:
        print(f"  Mean turns/session  {mean_turns:.1f}  "
              f"(from {turns_n:,.0f} reported sessions)")
    else:
        print("  Mean turns/session  unknown - nothing reports turns yet")

    findings = []

    share = index / retrievals if retrievals else 0
    print(f"\n1. Progressive disclosure  index mode on {share:.0%} of retrievals")
    if share < 0.2:
        findings.append(
            "Index mode is barely used, so progressive disclosure saves you "
            "almost nothing. It is opt-in and retrieve_context is documented "
            "as the default, so agents take the expensive path. THIS IS "
            "LIKELY THE BIGGEST GAP.")
    if index:
        net = withheld - refetched
        print(f"   withheld {withheld:,.0f} - refetched {refetched:,.0f} "
              f"= net {net:,.0f}")
        if net <= 0:
            findings.append(
                "When index mode IS used, agents fetch back everything they "
                "indexed: two round trips, no saving.")

    overhead = MCP_TOOL_OVERHEAD * max(turns_n, 1) * max(mean_turns, 1)
    print(f"\n2. MCP tool definitions    ~{MCP_TOOL_OVERHEAD:,} tokens per request")
    if turns_n:
        print(f"   {turns_n:,.0f} sessions x {mean_turns:.0f} turns "
              f"=> ~{overhead:,.0f} tokens of standing cost")
        if overhead > served:
            findings.append(
                f"Tool definitions (~{overhead:,.0f}) cost MORE than everything "
                f"retrieval served ({served:,.0f}). On these numbers AgentHive "
                "is net-negative on tokens before any benefit is counted.")
    else:
        print("   cannot size this without turn counts - pass turns= to log_session")

    miss_rate = empty / (empty + hits) if (empty + hits) else 0
    print(f"\n3. Wasted retrievals       {miss_rate:.0%} returned nothing")
    if miss_rate > 0.3:
        findings.append(
            f"{miss_rate:.0%} of retrievals return nothing - a paid round trip "
            "for no context. Check /gaps and alias the phrasings that keep "
            "missing.")

    print("\n" + "=" * 68)
    print("VERDICT")
    print("=" * 68)
    if findings:
        for i, f in enumerate(findings, 1):
            print(f"\n  {i}. {f}")
    else:
        print("\n  Nothing obviously wrong in the token path.")

    avoided = g(m, "agenthive_tokens_avoided_total")
    print(f"\n  The number NOT to quote: tokens_avoided_total = {avoided:,.0f}.")
    print("  It is measured against dumping the whole team graph, a baseline")
    print("  nobody would ever run, so it cannot tell you whether spend fell.")
    print("  The honest baseline is not running AgentHive at all - against")
    print("  that, memory ADDS input tokens and has to earn them back in work")
    print("  avoided, which is what /evidence and session turns measure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
