import urllib.error


def test_rate_limit_returns_429_with_retry_after(live_server_rate_limited, client_lib):
    # POST /teams is unauthenticated -> limited per client IP. Limit is 3/min.
    ok = 0
    hit_429 = False
    retry_after = None
    for _ in range(6):
        try:
            client_lib.create_team(live_server_rate_limited, "Team")
            ok += 1
        except urllib.error.HTTPError as e:
            if e.code == 429:
                hit_429 = True
                retry_after = e.headers.get("Retry-After")
                break
            raise

    assert ok <= 3
    assert hit_429
    assert retry_after is not None and int(retry_after) > 0
