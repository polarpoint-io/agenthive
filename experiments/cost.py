"""
What a run actually cost.

Token counts are not comparable across arms on their own. Most tokens
in an agent session are cached re-reads, which bill at a fraction of a
fresh read, and a cache WRITE bills at a premium. An arm that sends less
context pays less on both, every turn - so counting raw tokens
understates the difference between arms, in the direction that flatters
the arm sending more.

Rates are per-provider and belong to whoever runs this, so they are
arguments with documented defaults rather than constants. The defaults
below are the common shape at the time of writing (cache reads about a
tenth of input, cache writes about a quarter above it); CHECK THEM
against your provider's current pricing before quoting a figure.
"""

from dataclasses import dataclass

DEFAULT_CACHE_READ = 0.1
DEFAULT_CACHE_WRITE = 1.25
DEFAULT_OUTPUT = 5.0


@dataclass(frozen=True)
class Pricing:
    """Everything in units of one fresh input token."""

    cache_read: float = DEFAULT_CACHE_READ
    cache_write: float = DEFAULT_CACHE_WRITE
    output: float = DEFAULT_OUTPUT

    def weigh(self, usage: dict) -> float:
        """A single comparable number from one session's usage.

        Unknown keys are ignored rather than guessed at: a provider that
        reports a field this does not model should produce a number that
        is too LOW, which is visible as a discrepancy against a bill,
        rather than one inflated by a fabricated rate."""
        fresh = float(usage.get("input_tokens") or 0)
        read = float(usage.get("cache_read_input_tokens") or 0)
        written = float(usage.get("cache_creation_input_tokens") or 0)
        out = float(usage.get("output_tokens") or 0)
        return (fresh
                + read * self.cache_read
                + written * self.cache_write
                + out * self.output)


def totals(rows: list, pricing: Pricing = None) -> dict:
    """Aggregate a set of results, keeping the raw counts alongside the
    weighted figure so nobody has to trust the weighting to read the
    run."""
    pricing = pricing or Pricing()
    agg = {"input_tokens": 0, "cache_read_input_tokens": 0,
           "cache_creation_input_tokens": 0, "output_tokens": 0}
    weighted = 0.0
    for row in rows:
        usage = row.get("usage") or {}
        for key in agg:
            agg[key] += int(usage.get(key) or 0)
        weighted += pricing.weigh(usage)
    agg["weighted_tokens"] = round(weighted, 1)
    agg["raw_tokens"] = (agg["input_tokens"] + agg["cache_read_input_tokens"]
                         + agg["cache_creation_input_tokens"] + agg["output_tokens"])
    return agg
