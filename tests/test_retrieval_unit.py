"""Pure unit tests of the traversal algorithm - no server, no DB."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retrieval import build_graph, retrieve, slug, traverse


def node(title, body="body", tier="L1", links=None, node_id=None):
    return {"id": node_id or title, "title": title, "body": body, "tier": tier, "links": links or []}


def test_slug_normalizes_case_and_whitespace():
    assert slug("  Postgres  ") == "postgres"


def test_unresolved_links_are_dropped_not_dangling():
    nodes = [node("A", links=["B", "Nonexistent"])]
    graph = build_graph(nodes)
    assert graph["adj"][slug("A")] == set()


def test_links_are_bidirectional():
    nodes = [node("A", links=["B"]), node("B")]
    graph = build_graph(nodes)
    assert slug("b") in graph["adj"][slug("a")]
    assert slug("a") in graph["adj"][slug("b")]


def test_traverse_respects_hop_limit():
    nodes = [node("A", links=["B"]), node("B", links=["C"]), node("C")]
    graph = build_graph(nodes)
    result = traverse(graph, "A", hops=1)
    titles = {n["title"] for n in result}
    assert titles == {"A", "B"}


def test_hub_cutoff_stops_traversal_through_hub_but_not_visitation():
    leaves = [node(f"Leaf{i}", links=["Hub"]) for i in range(6)]
    hub = node("Hub", links=["Anchor"] + [f"Leaf{i}" for i in range(6)])
    anchor = node("Anchor", links=["Hub"])
    nodes = [anchor, hub] + leaves
    graph = build_graph(nodes)

    loose = traverse(graph, "Anchor", hops=2, hub_cutoff=15)
    assert len(loose) == 1 + 1 + 6  # anchor + hub + all leaves

    tight = traverse(graph, "Anchor", hops=2, hub_cutoff=3)
    titles = {n["title"] for n in tight}
    assert titles == {"Anchor", "Hub"}  # hub reached, but not traversed through


def test_retrieve_reports_token_reduction():
    nodes = [node("A", body="x" * 100, links=["B"]), node("B", body="y" * 100)]
    result = retrieve(nodes, "A", hops=1)
    assert result["neighborhood_count"] == 2
    assert result["approx_tokens"] > 0
    assert result["reduction_pct"] == 0  # full team reached in this tiny graph


def test_retrieve_missing_anchor_returns_empty_neighborhood():
    nodes = [node("A")]
    result = retrieve(nodes, "Does Not Exist")
    assert result["neighborhood_count"] == 0
