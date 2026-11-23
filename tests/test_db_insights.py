import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import Store


def test_aliased_misses_are_excluded_before_the_limit(tmp_path):
    store = Store(db_path=str(tmp_path / "test.db"))
    team, _ = store.create_team("Test team")
    node = store.create_memory(
        team["id"], "Postgres connection pool exhaustion",
        "A detailed note explaining how to diagnose connection pool exhaustion.",
    )
    store.backend.execute(
        "UPDATE memory_nodes SET status = 'approved' WHERE id = ?", (node["id"],))

    for i in range(100):
        anchor = f"old gap {i}"
        for _ in range(2):
            store.record_retrieval_miss(team["id"], anchor, "user")
        store.create_alias(team["id"], anchor, node["id"], created_by="admin")

    store.record_retrieval_miss(team["id"], "Postgres pool dying", "user")

    rows = store.list_retrieval_misses(
        team["id"], min_count=1, limit=100, exclude_aliased=True)

    assert [row["anchor"] for row in rows] == ["Postgres pool dying"]
