"""
retrieval.py - retrieval-by-traversal over a team's APPROVED memory graph.

Same algorithm as obsidian-vault/scripts/retrieve.py, ported from "walk a
folder of markdown files" to "walk a team's approved memory_nodes". Two
scoping rules baked in on purpose:

1. Only status=approved nodes are ever visited. Pending/rejected nodes
   are invisible to retrieval no matter how they're linked - that's the
   promotion gate actually meaning something.
2. Only links that resolve to another node IN THE SAME TEAM become graph
   edges. A team can never traverse into another team's memory, and an
   unresolved [[link]] to a note that doesn't exist yet is dropped rather
   than treated as a dangling neighbor (this is the same bug fix applied
   to the Obsidian tool after the real vault audit surfaced it).

Anchor resolution: an exact title slug is the fast path and is unchanged.
If that slug is missing, retrieval starts from the shortest approved title
whose tokens cover the query, or failing that from the most specific
shorter title whose tokens all appear in the query. That fallback is capped
so a broad phrase cannot return the whole graph. Tokens are whole words
(a single letter never matches), and body text is not searched.
"""

import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# A one-character token ("a") would match almost every title.
_MIN_TOKEN_LEN = 2
# Applied only when the anchor was not an exact title. Exact matches keep
# the hop and hub limits the caller asked for.
_FALLBACK_MAX_NODES = 8
_FALLBACK_MAX_STARTS = 3
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "with",
    "by", "from", "at", "as", "is", "it", "be", "this", "that",
})


def slug(title: str) -> str:
    return title.strip().lower()


def significant_tokens(title: str) -> list:
    """Whole-word tokens used for the non-exact fallback. Not used by slug()."""
    return [
        tok for tok in _TOKEN_RE.findall(slug(title))
        if len(tok) >= _MIN_TOKEN_LEN and tok not in _STOPWORDS
    ]


def build_graph(nodes: list) -> dict:
    """nodes: list of dicts with 'title' and 'links'. Returns
    {slug: {"node": node, "links": set(slugs)}} with bidirectional
    adjacency (forward links + backlinks), restricted to resolved
    targets only."""
    by_slug = {slug(n["title"]): n for n in nodes}
    known = set(by_slug)

    adj = {s: set() for s in by_slug}
    for s, n in by_slug.items():
        for target in n.get("links", []):
            t = slug(target)
            if t in known:
                adj[s].add(t)
                adj[t].add(s)

    return {"by_slug": by_slug, "adj": adj}


def resolve_start_slugs(graph: dict, anchor_title: str) -> tuple:
    """Return (start_slugs, exact).

    exact is True when anchor_title's slug is an approved note. Otherwise
    start_slugs is a short, ranked fallback (possibly empty)."""
    by_slug = graph["by_slug"]
    exact = slug(anchor_title)
    if exact in by_slug:
        return [exact], True

    wanted = significant_tokens(anchor_title)
    if not wanted:
        return [], False
    wanted_set = set(wanted)

    covering = []
    covered_by_query = []
    for node_slug, node in by_slug.items():
        title_tokens = significant_tokens(node["title"])
        if not title_tokens:
            continue
        title_set = set(title_tokens)
        if wanted_set <= title_set:
            precision = len(wanted_set) / float(len(title_tokens))
            covering.append((-precision, len(title_tokens), len(node_slug), node_slug))
        elif title_set <= wanted_set:
            covered_by_query.append((-len(title_set), len(node_slug), node_slug))

    if covering:
        covering.sort()
        return [covering[0][3]], False

    if not covered_by_query:
        return [], False
    covered_by_query.sort()
    best_token_count = -covered_by_query[0][0]
    starts = [
        row[2] for row in covered_by_query
        if -row[0] == best_token_count
    ][:_FALLBACK_MAX_STARTS]
    return starts, False


def traverse(graph: dict, anchor_title: str, hops: int = 2, hub_cutoff: int = 15) -> list:
    """BFS from anchor. A hub node (out-degree > hub_cutoff) is still
    included as a direct neighbor if reached, but traversal doesn't
    continue THROUGH it past depth 0."""
    from collections import deque

    anchor = slug(anchor_title)
    by_slug, adj = graph["by_slug"], graph["adj"]
    if anchor not in by_slug:
        return []

    visited = {anchor}
    frontier = deque([(anchor, 0)])
    order = [anchor]

    while frontier:
        node, depth = frontier.popleft()
        if depth == hops:
            continue
        if depth > 0 and len(adj.get(node, ())) > hub_cutoff:
            continue
        for neighbor in sorted(adj.get(node, ())):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            order.append(neighbor)
            frontier.append((neighbor, depth + 1))

    return [by_slug[s] for s in order]


def retrieve(approved_nodes: list, anchor_title: str, hops: int = 2,
             hub_cutoff: int = 15) -> dict:
    """High-level entry point used by the server. Returns the neighborhood
    plus an approximate token count, same shape as retrieve.py --show-tokens."""
    graph = build_graph(approved_nodes)
    starts, exact = resolve_start_slugs(graph, anchor_title)
    neighborhood = []
    seen = set()
    for start in starts:
        title = graph["by_slug"][start]["title"]
        for node in traverse(graph, title, hops, hub_cutoff):
            if node["id"] in seen:
                continue
            seen.add(node["id"])
            neighborhood.append(node)
            if not exact and len(neighborhood) >= _FALLBACK_MAX_NODES:
                break
        if not exact and len(neighborhood) >= _FALLBACK_MAX_NODES:
            break

    total_chars = sum(len(n["body"]) for n in neighborhood)
    vault_chars = sum(len(n["body"]) for n in approved_nodes) or 1
    # An empty neighborhood served nothing. Counting that as a 100%
    # reduction made misses look like the best possible saving.
    if neighborhood and vault_chars:
        reduction_pct = round(100 - (100 * total_chars / vault_chars), 1)
    else:
        reduction_pct = 0

    return {
        "anchor": anchor_title,
        # How the anchor landed: an exact title, a token fallback, or
        # nothing. Computed above and previously discarded. A graph where
        # most retrievals fall back is one whose titles don't match how
        # people actually phrase things, which is a naming problem no
        # amount of traversal tuning will fix.
        "resolution": "exact" if exact else ("fallback" if starts else "none"),
        "team_node_count": len(approved_nodes),
        "neighborhood_count": len(neighborhood),
        "neighborhood": [
            {
                "id": n["id"], "title": n["title"], "tier": n["tier"],
                "body": n["body"],
                # Carried so project_index() can hand an agent something to
                # filter on without reading a single body. Costs a few bytes
                # on the full response; makes the index layer worth having.
                "tags": n.get("tags") or [],
            }
            for n in neighborhood
        ],
        "approx_tokens": total_chars // 4,
        "approx_tokens_full_team": vault_chars // 4,
        "reduction_pct": reduction_pct,
    }


def pending_title_match(pending_nodes: list, anchor_title: str):
    """If a still-pending note would have satisfied this anchor, return
    its id and title. Reviewers use this to approve the note that turns
    a repeated miss into a hit. Body text is not included."""
    if not pending_nodes:
        return None
    graph = build_graph(pending_nodes)
    starts, exact = resolve_start_slugs(graph, anchor_title)
    if not starts:
        return None
    node = graph["by_slug"][starts[0]]
    return {"id": node["id"], "title": node["title"], "exact": exact}


# ---------------------------------------------------------------------------
# Progressive disclosure
# ---------------------------------------------------------------------------
# retrieve() answers "what is near this anchor, and here is all of it".
# That is one round trip and no wasted calls, which is why it stays the
# default - but it bills the caller for every body in the neighborhood,
# including the ones they would have skipped had they been able to see the
# titles first.
#
# project_index() is the same traversal with the bodies withheld: ids,
# titles, tiers, tags, and a per-node token cost. The agent picks the ids
# worth reading and fetches only those (POST .../memory/fetch).
#
# Deliberately a projection of the full result rather than a cheaper
# second query. The traversal is the same work either way, so computing
# it once and serving either shape from one cache entry means an index
# call and a full call share cache hits - including CROSS-USER hits,
# which is the metric that actually evidences shared memory. A separate
# query path would have split that in two and halved the apparent reuse.
#
# Honesty about the saving: an agent that fetches every id back has saved
# nothing and spent an extra round trip. So this does NOT touch
# tokens_avoided (see metrics.py). The withheld-vs-refetched pair is
# tracked separately and nets out in Grafana - see observability/README.md.


def project_index(result: dict) -> dict:
    """Strip bodies from a retrieve() result, keeping what an agent needs
    to choose. Shape mirrors retrieve() so a caller can switch modes
    without re-learning the response."""
    index = []
    for node in result.get("neighborhood", []):
        body = node.get("body") or ""
        index.append({
            "id": node["id"],
            "title": node["title"],
            "tier": node["tier"],
            "tags": node.get("tags") or [],
            # What fetching THIS body would cost, so the choice is priced
            # per node rather than as one opaque total.
            "approx_tokens": len(body) // 4,
        })

    index_chars = sum(len(n["title"]) for n in index)
    return {
        "anchor": result.get("anchor"),
        "resolution": result.get("resolution"),
        "mode": "index",
        "team_node_count": result.get("team_node_count", 0),
        "neighborhood_count": result.get("neighborhood_count", 0),
        "neighborhood": index,
        "approx_tokens": index_chars // 4,
        "approx_tokens_full_neighborhood": result.get("approx_tokens", 0),
        "approx_tokens_full_team": result.get("approx_tokens_full_team", 0),
        "reduction_pct": result.get("reduction_pct", 0),
    }


def filter_by_tags(result: dict, tags) -> dict:
    """Narrow a result's neighborhood to nodes carrying any of `tags`.

    Applied AFTER traversal on purpose: filtering before it would change
    which nodes are reachable and silently shrink the graph, so a tag
    filter would quietly become a traversal boundary. This way the walk
    is identical and the filter only decides what gets shown."""
    wanted = {str(t).strip().lower() for t in (tags or []) if str(t).strip()}
    if not wanted:
        return result

    kept = [
        n for n in result.get("neighborhood", [])
        if wanted & {str(t).strip().lower() for t in (n.get("tags") or [])}
    ]
    narrowed = dict(result)
    narrowed["neighborhood"] = kept
    narrowed["neighborhood_count"] = len(kept)
    narrowed["approx_tokens"] = sum(len(n.get("body") or "") for n in kept) // 4
    narrowed["filtered_by_tags"] = sorted(wanted)
    return narrowed


def select_nodes(approved_nodes: list, node_ids) -> dict:
    """Layer 3: bodies for specific ids, from this team's APPROVED nodes
    only.

    Takes the already team-scoped, already approved-only list the caller
    is entitled to and selects within it, rather than looking ids up
    directly. An id is an opaque string a caller could have got anywhere,
    so a direct lookup would be a cross-team read waiting to happen; here
    an id from another team simply is not in the list and lands in
    `missing` like any other unknown id, with no way to tell the two
    apart from outside."""
    wanted = [str(i) for i in (node_ids or [])]
    by_id = {n["id"]: n for n in approved_nodes}

    found, missing = [], []
    seen = set()
    for node_id in wanted:
        if node_id in seen:
            continue
        seen.add(node_id)
        node = by_id.get(node_id)
        if node is None:
            missing.append(node_id)
            continue
        found.append({
            "id": node["id"], "title": node["title"], "tier": node["tier"],
            "tags": node.get("tags") or [], "body": node["body"],
        })

    served_chars = sum(len(n["body"]) for n in found)
    return {
        "mode": "fetch",
        "requested_count": len(seen),
        "node_count": len(found),
        "nodes": found,
        # Not an error. An id can go missing because it was rejected,
        # un-approved, or belongs to another team - all of which are the
        # gate working. The caller is told what it did not get.
        "missing": missing,
        "approx_tokens": served_chars // 4,
    }
