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
        "team_node_count": len(approved_nodes),
        "neighborhood_count": len(neighborhood),
        "neighborhood": [
            {"id": n["id"], "title": n["title"], "tier": n["tier"], "body": n["body"]}
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
