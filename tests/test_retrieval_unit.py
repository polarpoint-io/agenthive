"""Pure unit tests of the traversal algorithm - no server, no DB."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retrieval import build_graph, resolve_start_slugs, retrieve, slug, traverse


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
    nodes = [node("A", body="x" * 100)]
    result = retrieve(nodes, "Does Not Exist")
    assert result["neighborhood_count"] == 0
    assert result["approx_tokens"] == 0
    # A miss served nothing. 100 here used to read as a perfect saving.
    assert result["reduction_pct"] == 0


def test_exact_title_beats_a_shorter_overlapping_title():
    nodes = [
        node("Key Vault", links=["Rotation"]),
        node("Key", links=["Unrelated"]),
        node("Rotation"),
        node("Unrelated"),
    ]
    result = retrieve(nodes, "Key Vault", hops=1)
    titles = [n["title"] for n in result["neighborhood"]]
    assert titles[0] == "Key Vault"
    assert "Rotation" in titles
    assert "Unrelated" not in titles


def test_short_query_finds_the_shortest_covering_title():
    nodes = [
        node("GitOps companion branch naming for values files"),
        node("GitOps rollout"),
        node("Postgres pool"),
    ]
    result = retrieve(nodes, "GitOps", hops=0)
    assert [n["title"] for n in result["neighborhood"]] == ["GitOps rollout"]


def test_longer_query_finds_the_shorter_door_title():
    nodes = [
        node("Front Door", links=["Origin"]),
        node("Origin"),
        node("Postgres pool"),
    ]
    result = retrieve(nodes, "front door origin hostname", hops=0)
    assert [n["title"] for n in result["neighborhood"]] == ["Front Door"]


def test_token_fallback_does_not_match_inside_another_word():
    nodes = [node("Digital transformation"), node("GitOps rollout")]
    result = retrieve(nodes, "git")
    assert result["neighborhood_count"] == 0


def test_single_letter_query_does_not_match_every_title():
    nodes = [node("A note about postgres"), node("Another note")]
    result = retrieve(nodes, "a")
    assert result["neighborhood_count"] == 0


def test_fallback_neighborhood_is_capped():
    leaves = [node(f"Leaf {i}", links=["Widget rollout"]) for i in range(12)]
    nodes = [node("Widget rollout")] + leaves
    fallback = retrieve(nodes, "widget", hops=1)
    assert fallback["neighborhood_count"] == 8
    assert fallback["neighborhood"][0]["title"] == "Widget rollout"

    exact = retrieve(nodes, "Widget rollout", hops=1)
    assert exact["neighborhood_count"] == 13


def test_pending_title_match_points_at_the_note_that_would_hit():
    from retrieval import pending_title_match

    pending = [node("Key Vault rotation", body="still pending")]
    match = pending_title_match(pending, "Key Vault")
    assert match["title"] == "Key Vault rotation"
    assert pending_title_match(pending, "GitOps") is None


def test_resolve_start_slugs_reports_exact_match():
    nodes = [node("GitOps rollout")]
    graph = build_graph(nodes)
    starts, exact = resolve_start_slugs(graph, "  GitOps rollout ")
    assert exact is True
    assert starts == [slug("GitOps rollout")]
