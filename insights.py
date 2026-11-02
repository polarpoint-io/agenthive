"""
insights.py - the memory-processing layer: what the graph has learned
about its own use.

AgentHive already records every signal needed to improve itself and has
never acted on any of them. memory_access_log knows which nodes get
served together and to whom; retrieval_misses knows what people keep
looking for and never find; and every approve/reject is a human-labelled
example of what this team does and does not want kept. All of it
currently terminates in a dashboard.

This module turns those signals into PROPOSALS. Four of them:

  1. gaps          - anchors that keep missing, and whether something
                     already pending would have answered them
  2. link edges    - nodes repeatedly retrieved together but not linked
  3. tiers         - which nodes earned promotion, which went cold
  4. rules         - auto-approve rules the team's own decisions justify

TWO RULES THIS MODULE IS BUILT AROUND
-------------------------------------
**No model.** Every function here is arithmetic over rows. Nothing calls
an LLM, because the moment AgentHive calls one it becomes the proxy its
ADR exists to avoid, and a memory system that rewrites its own contents
with a model is a memory system you can no longer audit.

**Proposals, not actions.** Nothing here mutates anything. Each function
returns a suggestion for a human to accept or ignore. That is not
timidity: self-improving memory is a documented attack surface (poison
what gets consolidated and you poison every future retrieval), and the
approval gate is the defence. A suggestion that applied itself would
route around the one control that makes this system trustworthy.

The one exception is tiers, which an admin can apply explicitly - and
tier is metadata about how a node is USED, never a word of its content.
"""

from governance import NEVER_AUTO_APPROVE, needs_human
from retrieval import pending_title_match, slug

# A pair of nodes has to co-occur in several DIFFERENT retrievals before
# it means anything. Two nodes served together once are a coincidence of
# one person's anchor; five times is a relationship the graph is missing.
DEFAULT_MIN_SHARED = 3

# Rule mining needs enough decisions to not be noise, and near-total
# agreement to be worth proposing. A rule skips human review entirely,
# so the bar is deliberately higher than "usually fine".
DEFAULT_MIN_SUPPORT = 5
DEFAULT_MIN_PRECISION = 0.9

# The inverse signal: a writer whose output is mostly rejected is worth
# naming, because the fix is upstream in that agent's prompt, not in the
# review queue.
DEFAULT_NOISE_PRECISION = 0.4

# Promotion thresholds. Deliberately boring integers rather than a
# learned score - an admin has to be able to look at a tier and say why.
TIER_RULES = (
    ("L3", 3, 5),   # tier, min distinct users, min retrievals
    ("L2", 1, 2),
)
DEFAULT_TIER = "L1"

# One contradiction stops a node being promoted any further.
#
# Tiers measure whether a note is READ, which is popularity, not truth.
# Without this a note that is WRONG but useful-looking keeps being
# retrieved, keeps climbing, and ends up ranked canonical - the system
# confidently amplifying the one thing it should have caught. A single
# teammate saying "I tried this and it was wrong" is enough to stop the
# climb; it takes more to push it back down, because one dissenting
# voice should not be able to demote a note the rest of the team relies
# on.
CONTRADICTIONS_BLOCK_PROMOTION = 1
CONTRADICTIONS_FORCE_DEMOTION = 2


def rank_gaps(miss_rows, pending_nodes, min_count: int = 2) -> list:
    """What this team keeps looking for and never finds.

    A single miss is someone phrasing an anchor badly. The same anchor
    missing repeatedly is the graph being incomplete, which is a
    different problem with a different fix: somebody has to write the
    note. Default min_count=2 draws that line.

    When something already PENDING would have answered the anchor, the
    fix is not "write a note", it is "approve the one sitting in the
    queue" - so that is called out separately and first."""
    from retrieval import pending_title_match

    gaps = []
    for row in miss_rows:
        if (row["miss_count"] or 0) < min_count:
            continue
        match = pending_title_match(pending_nodes, row["anchor"])
        gaps.append({
            "anchor": row["anchor"],
            "miss_count": row["miss_count"],
            "last_missed_at": row["last_missed_at"],
            # An awaiting-review answer is a queue problem, not a
            # knowledge problem. Different owner, different urgency.
            "awaiting_review": bool(match),
            "pending_id": match["id"] if match else None,
            "pending_title": match["title"] if match else None,
        })
    gaps.sort(key=lambda g: (not g["awaiting_review"], -g["miss_count"]))
    return gaps


def suggest_links(pairs, nodes, min_shared: int = DEFAULT_MIN_SHARED) -> list:
    """Edges the graph is missing.

    Retrieval here IS traversal over links, so a missing edge is not a
    cosmetic gap - it is a node that cannot be reached from an anchor it
    genuinely relates to. Two nodes the same retrievals keep returning
    together are reachable from the same anchors today by coincidence of
    one hop; linking them makes that reliable.

    Pairs already linked in either direction are dropped: the graph
    builds adjacency bidirectionally (see retrieval.build_graph), so a
    one-way link is already a two-way edge and suggesting it again is
    noise."""
    by_id = {n["id"]: n for n in nodes}

    linked = set()
    titles_to_id = {}
    for node in nodes:
        titles_to_id[(node["title"] or "").strip().lower()] = node["id"]
    for node in nodes:
        for target in node.get("links") or []:
            target_id = titles_to_id.get(str(target).strip().lower())
            if target_id:
                linked.add(frozenset((node["id"], target_id)))

    out = []
    for row in pairs:
        a_id, b_id = row["a_id"], row["b_id"]
        if (row["shared"] or 0) < min_shared:
            continue
        if frozenset((a_id, b_id)) in linked:
            continue
        a, b = by_id.get(a_id), by_id.get(b_id)
        if a is None or b is None:
            continue  # one of them was rejected or un-approved since
        out.append({
            "a_id": a_id, "a_title": a["title"],
            "b_id": b_id, "b_title": b["title"],
            "shared_retrievals": row["shared"],
        })
    return out


def propose_tiers(stats, default_tier: str = DEFAULT_TIER) -> list:
    """Which nodes the team actually uses.

    `tier` has existed on every node since the first schema and has
    never been anything but its default. It is the natural place to
    record that five different people have pulled a note up, or that
    nothing has touched one since it was approved.

    Promotion and demotion both fall out of computing the target tier
    from current stats rather than ratcheting - a node that was useful
    last quarter and is dead now drops back on its own, which is the
    point. Unbounded accumulation is documented to degrade retrieval
    quality; a tier that only ever goes up would reproduce that.

    Returns only nodes whose tier should CHANGE."""
    out = []
    for row in stats:
        users = row["distinct_users"] or 0
        hits = row["access_count"] or 0
        against = row.get("contradictions") or 0
        current = row["tier"] or default_tier

        target = default_tier
        for tier, min_users, min_hits in TIER_RULES:
            if users >= min_users and hits >= min_hits:
                target = tier
                break

        # Correctness outranks popularity. Earning a tier on retrieval
        # counts for nothing once somebody has said the note is wrong.
        reason = "use"
        if against >= CONTRADICTIONS_FORCE_DEMOTION:
            target = default_tier
            reason = "contradicted"
        elif against >= CONTRADICTIONS_BLOCK_PROMOTION and target > current:
            target = current          # hold it where it is, do not climb
            reason = "contradicted"

        if target == current:
            continue
        out.append({
            "id": row["id"],
            "title": row["title"],
            "current_tier": current,
            "proposed_tier": target,
            "direction": "promote" if target > current else "demote",
            "reason": reason,
            "access_count": hits,
            "distinct_users": users,
            "confirmations": row.get("confirmations") or 0,
            "contradictions": against,
            "last_accessed_at": row["last_accessed_at"],
        })
    out.sort(key=lambda r: (r["direction"], -r["access_count"]))
    return out


# ruff flags the datetime handling below (UP017, FURB162) and suggests
# datetime.UTC and dropping the "Z" replacement. Both are Python 3.11+.
# This repo targets 3.11, but db.now() emits a trailing "Z" and
# fromisoformat only learned to parse one in 3.11 - so the portable form
# stays, deliberately, rather than being taken on faith. Verified by
# reverting it and watching three tier tests go red on 3.10.
def find_stale(stats, min_age_days: int = 90, now_iso: str = "") -> list:
    """Approved long ago, never once retrieved.

    Not proposed for deletion - nothing here deletes. It is a prune list
    for a human, because a graph that only grows gets worse at answering
    over time, and nobody notices because the failure mode is a slightly
    worse neighbourhood rather than an error."""
    import datetime

    if now_iso:
        reference = datetime.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    else:
        reference = datetime.datetime.now(datetime.timezone.utc)

    out = []
    for row in stats:
        if (row["access_count"] or 0) > 0:
            continue
        # Somebody vouched for it since. Unread is not the same as
        # unwanted, and a prune list that ignores that is how a team
        # loses the note nobody needed until the day they did.
        if (row.get("confirmations") or 0) > 0:
            continue
        created = row.get("created_at")
        if not created:
            continue
        try:
            created_at = datetime.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except ValueError:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=datetime.timezone.utc)
        age_days = (reference - created_at).days
        if age_days >= min_age_days:
            out.append({
                "id": row["id"], "title": row["title"],
                "age_days": age_days, "access_count": 0,
            })
    out.sort(key=lambda r: -r["age_days"])
    return out


def _group_decisions(decisions):
    """Bucket decisions by tag and by agent, counting approvals.

    Auto-approved nodes are excluded entirely. A rule approved them, so
    counting them as evidence that the rule is a good idea is circular -
    it would let one hasty rule justify its own successors."""
    groups = {}
    for d in decisions:
        if d.get("auto_approved"):
            continue
        keys = [("tag", str(t).strip().lower()) for t in (d.get("tags") or []) if str(t).strip()]
        if d.get("agent_id"):
            keys.append(("agent", d["agent_id"]))
        for key in keys:
            bucket = groups.setdefault(key, {"approved": 0, "total": 0})
            bucket["total"] += 1
            if d["status"] == "approved":
                bucket["approved"] += 1
    return groups


def mine_rules(decisions, existing_rules=(),
               min_support: int = DEFAULT_MIN_SUPPORT,
               min_precision: float = DEFAULT_MIN_PRECISION) -> dict:
    """Auto-approve rules this team's own decisions already justify.

    Nobody else in the memory literature can do this, because nobody
    else keeps a human in the write path. Every approve and reject here
    is a free label saying what this team wants kept. A tag approved
    40 times out of 40 is a rule somebody should have written months
    ago; the system can notice and offer it.

    Returns both directions. `rules` are candidates to create; `noisy`
    names groups rejected most of the time, where the fix is upstream in
    that agent's prompt rather than anywhere in this queue.

    Reserved tags are never proposed, whatever the evidence. The whole
    point of NEVER_AUTO_APPROVE is that a security_alert gets a human
    regardless of how routine it has looked so far - a mined rule that
    overrode that would quietly undo the guardrail."""
    have_tags = {
        str(r.get("match_tag")).strip().lower()
        for r in existing_rules if r.get("match_tag")
    }
    have_agents = {r.get("match_agent_id") for r in existing_rules if r.get("match_agent_id")}

    rules, noisy = [], []
    for (kind, value), counts in _group_decisions(decisions).items():
        total, approved = counts["total"], counts["approved"]
        if total < min_support:
            continue
        precision = approved / float(total)

        if kind == "tag" and value in NEVER_AUTO_APPROVE:
            continue
        already = value in have_tags if kind == "tag" else value in have_agents

        entry = {
            "match": kind, "value": value,
            "approved": approved, "total": total,
            "precision": round(precision, 3),
        }
        if precision >= min_precision and not already:
            rules.append(entry)
        elif precision <= DEFAULT_NOISE_PRECISION:
            noisy.append(entry)

    rules.sort(key=lambda r: (-r["precision"], -r["total"]))
    noisy.sort(key=lambda r: (r["precision"], -r["total"]))
    return {"rules": rules, "noisy": noisy}


def triage_pending(pending_nodes, miss_rows, now_iso: str = "") -> list:
    """Order the review queue by what is waiting on it.

    The queue is otherwise a flat list by age, which is the wrong order
    the moment it has anything in it. A node that would close an anchor
    people have missed nine times is worth a reviewer's attention ahead
    of forty backstop nodes that happen to be older, and nothing in the
    product has ever said so.

    This is triage, not a decision. Every node still comes back; only
    the order and the annotations change, and a reviewer can ignore both.
    Automating the ORDER makes a human faster. Automating the verdict is
    the thing worth refusing, because approval is what makes a memory
    visible to every agent on the team.

    `closes_gap` carries the miss count, so "how much pain is this
    answer sitting on" is visible without cross-referencing anything."""
    by_anchor = {}
    for row in miss_rows:
        key = slug(row["anchor"])
        if key:
            by_anchor[key] = row["miss_count"] or 0

    out = []
    for node in pending_nodes:
        title_key = slug(node.get("title") or "")
        misses = by_anchor.get(title_key, 0)

        # An exact title match is the confident case. Fall back to the
        # same resolver retrieval itself uses, so "would this close the
        # gap" is answered the way the gap would actually be answered.
        if not misses:
            for anchor_key, count in by_anchor.items():
                match = pending_title_match([node], anchor_key)
                if match:
                    misses = max(misses, count)
                    break

        entry = dict(node)
        entry["closes_gap"] = misses > 0
        entry["gap_miss_count"] = misses
        entry["needs_human"] = needs_human(node.get("tags"))
        out.append(entry)

    # Gap-closers first, by how much they have cost; then anything a
    # reserved tag puts beyond automation; then oldest.
    out.sort(key=lambda n: (
        not n["closes_gap"],
        -n["gap_miss_count"],
        not n["needs_human"],
        n.get("created_at") or "",
    ))
    return out


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------
# Whether any of this helps is the one question AgentHive has never been
# able to answer. It never sees the task, so it cannot judge success on
# its own - but a session can report back, and git can be asked whether
# the work survived.
#
# Two honesty rules run through everything below.
#
# Sessions that reported nothing are counted as UNKNOWN, never folded
# into the denominator as failures or quietly dropped. A rate computed
# over whoever bothered to answer is a measure of who answers.
#
# And a comparison on thin data is refused outright rather than
# reported with a caveat nobody reads. A dashboard that prints 100%
# from two sessions will be screenshotted and quoted; one that says
# "not enough data" will not.

MIN_SESSIONS_PER_ARM = 20


def _rate(rows, key="outcome"):
    """Success rate over sessions that actually said something."""
    said = [r for r in rows if r.get(key) in ("succeeded", "partial", "failed")]
    if not said:
        return None, 0, 0
    good = sum(1 for r in said if r[key] == "succeeded")
    return round(good / len(said), 3), good, len(said)


def _survival(rows):
    checked = [r for r in rows if r.get("work_survived") is not None]
    if not checked:
        return None, 0, 0
    kept = sum(1 for r in checked if r["work_survived"])
    return round(kept / len(checked), 3), kept, len(checked)


def _arm(rows, label):
    rate, good, said = _rate(rows)
    surv, kept, checked = _survival(rows)
    turns = [r["turns"] for r in rows if r.get("turns")]
    return {
        "arm": label,
        "sessions": len(rows),
        "reported": said,
        "silent": len(rows) - said,
        "success_rate": rate,
        "succeeded": good,
        "work_survival_rate": surv,
        "work_verified": checked,
        "work_survived": kept,
        "median_turns": sorted(turns)[len(turns) // 2] if turns else None,
    }


def compare_sessions(sessions, min_per_arm: int = MIN_SESSIONS_PER_ARM,
                      holdout_running: bool = False) -> dict:
    """Did memory help?

    Three arms, in descending order of how much they prove:

      held_out  - memory deliberately withheld. The only arm that makes
                  this causal, and the only one that costs something.
      served    - retrieved and got something.
      no_memory - never asked. Observational and heavily confounded: a
                  session that does not ask may differ from one that
                  does in every way that matters, so this arm is
                  reported but must not be read as a control.
    """
    served, held, none = [], [], []
    for s in sessions:
        if s.get("held_out"):
            held.append(s)
        elif (s.get("retrievals") or 0) > 0 and (s.get("nodes_served") or 0) > 0:
            served.append(s)
        else:
            none.append(s)

    arms = [_arm(served, "served"), _arm(held, "held_out"), _arm(none, "no_memory")]
    by = {a["arm"]: a for a in arms}

    # Which arms have to be populated depends on whether a holdout is
    # actually running. Demanding held_out sessions when nobody turned
    # the holdout on would report "not enough data" forever, however
    # much real data accumulated - the report would never say anything
    # at all, which is a worse failure than saying something qualified.
    required = ["served", "held_out"] if holdout_running else ["served", "no_memory"]
    # Both conditions, not just the threshold. A caller can set
    # min_per_arm to 0, and an arm nobody reported on has no rate to
    # subtract - computing a delta from two Nones is how this endpoint
    # returns a 500 instead of an honest "no answer yet".
    thin = [name for name in required
            if by[name]["reported"] < min_per_arm
            or by[name]["success_rate"] is None]

    verdict, note = None, None
    if thin:
        note = (
            f"Not enough reported sessions to compare ({', '.join(thin)}: fewer "
            f"than {min_per_arm}, or none reporting an outcome at all). No "
            "verdict is better than one drawn from a handful.")
    elif holdout_running:
        delta = by["served"]["success_rate"] - by["held_out"]["success_rate"]
        verdict = {"comparison": "served vs held_out",
                   "delta": round(delta, 3), "causal": True}
        note = ("A holdout is running, so this difference is causal for this "
                "team's work - not a claim about anyone else's.")
    else:
        delta = by["served"]["success_rate"] - by["no_memory"]["success_rate"]
        verdict = {"comparison": "served vs no_memory",
                   "delta": round(delta, 3), "causal": False}
        note = ("No holdout is running, so this is OBSERVATIONAL and not a "
                "causal claim. Sessions that retrieve differ from sessions "
                "that do not in ways nothing here controls for - the people "
                "who reach for memory may simply be doing the kind of work "
                "memory suits. Set AGENTHIVE_HOLDOUT_PERCENT to find out.")

    return {"arms": arms, "verdict": verdict, "note": note,
            "min_sessions_per_arm": min_per_arm,
            "holdout_running": holdout_running}


# Tool pruning thresholds. A tool is only dropped on evidence from more
# than one person: one engineer who never uses the browser tool is a
# preference, a whole team that never uses it is a fact.
PRUNE_MIN_SESSIONS = 5
PRUNE_MIN_USERS = 2


def propose_tool_pruning(usage, min_sessions: int = PRUNE_MIN_SESSIONS,
                          min_users: int = PRUNE_MIN_USERS) -> dict:
    """Tools this team loads and never uses.

    Every loaded tool's definition is sent with every request, so an
    unused one is a standing tax on every turn. Dropping it is safe
    precisely because it is reversible - and because being wrong costs
    one command to undo, not a lost answer."""
    prune, keep, thin = [], [], []
    for row in usage:
        entry = {"tool": row["tool"], "uses": row["uses"],
                 "sessions": row["sessions"], "users": row["users"]}
        if row["uses"] > 0:
            keep.append(entry)
        elif row["sessions"] >= min_sessions and row["users"] >= min_users:
            prune.append(entry)
        else:
            entry["needs"] = (f"{min_sessions} sessions across {min_users} "
                              "people before this is evidence")
            thin.append(entry)
    return {"prune": prune, "keep": keep, "not_yet": thin,
            "min_sessions": min_sessions, "min_users": min_users}
