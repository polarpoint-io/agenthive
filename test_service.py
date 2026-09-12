#!/usr/bin/env python3
"""
test_service.py - end-to-end test against a REAL running server.py.

Not a unit test suite - a scripted walkthrough that proves the properties
that actually matter for this design:

  1. A newly logged memory is PENDING and invisible to retrieval.
  2. Approving it makes it visible; rejecting it keeps it invisible forever.
  3. Two teams cannot see each other's memory, even with identical anchor
     titles and identical link structures.
  4. A hub node (high out-degree) is reachable directly but doesn't pull in
     everything beyond it - the same guardrail as the Obsidian tool.

Run:  python3 server.py --port 8791 --db /tmp/test_team_memory.db &
      python3 test_service.py
"""
import sys
import time

from client import TeamMemoryClient, create_team

BASE = "http://127.0.0.1:8791"
PASS = 0
FAIL = 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   - {label}")
    else:
        FAIL += 1
        print(f"  FAIL - {label}")


def wait_for_server(retries=20):
    import urllib.request
    for _ in range(retries):
        try:
            urllib.request.urlopen(BASE + "/teams", timeout=1)
        except urllib.error.HTTPError:
            return  # 405/400 on GET /teams still proves the server is up
        except Exception:
            time.sleep(0.25)
            continue
        return
    raise SystemExit("server never came up")


def main():
    import urllib.error
    wait_for_server()

    print("\n=== 1. Pending memory is invisible to retrieval ===")
    team_a = create_team(BASE, "Platform Team")
    client_a = TeamMemoryClient(BASE, team_a["api_key"], team_a["id"])
    agent = client_a.create_agent("bug-fix engineer", "fixes prod incidents")

    client_a.log_session(
        title="Postgres connection pool exhaustion",
        body="Root cause: pgbouncer max_client_conn too low for the new "
             "worker fleet size. Fix: raised to 400, added an alert at 80%.",
        agent_id=agent["id"],
        tags=["postgres", "incident"],
        links=[],
    )

    result = client_a.retrieve_context("Postgres connection pool exhaustion")
    check("pending node has zero neighborhood before approval",
          result["neighborhood_count"] == 0)

    pending = client_a.list_pending()
    check("exactly one pending node", len(pending["pending"]) == 1)
    node_id = pending["pending"][0]["id"]

    client_a.approve(node_id, reviewed_by="surj")
    result = client_a.retrieve_context("Postgres connection pool exhaustion")
    check("approved node now appears in retrieval",
          result["neighborhood_count"] == 1)
    check("approx_tokens is a real positive number",
          result["approx_tokens"] > 0)

    print("\n=== 2. Rejected memory stays invisible forever ===")
    client_a.log_session(
        title="Speculative note about switching databases",
        body="Half-formed idea, not reviewed, should never surface.",
        agent_id=agent["id"],
    )
    pending = client_a.list_pending()
    reject_id = [p["id"] for p in pending["pending"] if "Speculative" in p["title"]][0]
    client_a.reject(reject_id, reviewed_by="surj", reason="not ready, needs discussion")

    result = client_a.retrieve_context("Speculative note about switching databases")
    check("rejected node never appears in retrieval",
          result["neighborhood_count"] == 0)

    print("\n=== 3. Two teams cannot see each other's memory ===")
    team_b = create_team(BASE, "Data Team")
    client_b = TeamMemoryClient(BASE, team_b["api_key"], team_b["id"])

    node = client_b.log_session(
        title="Postgres connection pool exhaustion",  # same title on purpose
        body="Completely different incident, different team, same title.",
    )
    client_b.approve(node["id"], reviewed_by="data-lead")

    result_a = client_a.retrieve_context("Postgres connection pool exhaustion")
    result_b = client_b.retrieve_context("Postgres connection pool exhaustion")
    check("team A's retrieval body is team A's content",
          "pgbouncer" in result_a["neighborhood"][0]["body"])
    check("team B's retrieval body is team B's content, not team A's",
          "pgbouncer" not in result_b["neighborhood"][0]["body"]
          and "different incident" in result_b["neighborhood"][0]["body"])

    # cross-team key should be rejected outright
    try:
        import urllib.error
        client_a.team_id = team_b["id"]  # try to use team A's key against team B's id
        client_a.retrieve_context("Postgres connection pool exhaustion")
        check("using team A's key against team B's id is rejected", False)
    except RuntimeError as e:
        check("using team A's key against team B's id is rejected", "401" in str(e))
    client_a.team_id = team_a["id"]  # restore

    print("\n=== 4. Hub cutoff stops traversal through a heavily-linked node ===")
    team_c = create_team(BASE, "Hub Test Team")
    client_c = TeamMemoryClient(BASE, team_c["api_key"], team_c["id"])

    # Build: Anchor -> Hub -> [Leaf1..Leaf6]. Hub has out-degree 7 (anchor + 6 leaves).
    leaves = [f"Leaf {i}" for i in range(1, 7)]
    hub = client_c.log_session(
        title="Hub", body="A hub note with many links.",
        links=["Anchor"] + leaves,
    )
    anchor = client_c.log_session(
        title="Anchor", body="The starting point.", links=["Hub"],
    )
    leaf_ids = []
    for leaf in leaves:
        n = client_c.log_session(title=leaf, body=f"Content of {leaf}.", links=["Hub"])
        leaf_ids.append(n["id"])

    for n in [hub, anchor] + [{"id": i} for i in leaf_ids]:
        client_c.approve(n["id"], reviewed_by="tester")

    result_no_cutoff = client_c.retrieve_context("Anchor", hops=2, hub_cutoff=15)
    result_with_cutoff = client_c.retrieve_context("Anchor", hops=2, hub_cutoff=3)

    check("without a tight cutoff, all leaves are reachable in 2 hops",
          result_no_cutoff["neighborhood_count"] == 1 + 1 + len(leaves))  # anchor+hub+leaves
    check("with hub_cutoff=3, Hub is reached but leaves beyond it are not",
          result_with_cutoff["neighborhood_count"] == 2)  # anchor + hub only

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
