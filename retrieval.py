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
"""


def slug(title: str) -> str:
    return title.strip().lower()


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
    neighborhood = traverse(graph, anchor_title, hops, hub_cutoff)

    total_chars = sum(len(n["body"]) for n in neighborhood)
    vault_chars = sum(len(n["body"]) for n in approved_nodes) or 1

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
        "reduction_pct": round(100 - (100 * total_chars / vault_chars), 1) if vault_chars else 0,
    }
