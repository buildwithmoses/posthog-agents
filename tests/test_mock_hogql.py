from posthog_agents.mock.hogql import Store, translate


def test_translate_properties_and_functions():
    q = translate("SELECT countIf(event = 'x'), toStartOfDay(timestamp) FROM events WHERE properties.$browser = {b} AND timestamp > now() - INTERVAL 7 DAY",
                  {"b": "Chrome"}, now="2026-10-20 12:00:00")
    assert "json_extract_string(properties, '$.\"$browser\"')" in q
    assert "'Chrome'" in q and "TIMESTAMP '2026-10-20 12:00:00'" in q and "date_trunc('day', " in q


def test_store_runs_hogql():
    ev = [
        {"event": "invoice_created", "distinct_id": "u1", "timestamp": "2026-10-19 10:00:00", "properties": {"$groups": {"company": "acme"}, "amount": 120}},
        {"event": "invoice_created", "distinct_id": "u2", "timestamp": "2026-10-10 10:00:00", "properties": {"amount": 80}},
        {"event": "$pageview", "distinct_id": "u1", "timestamp": "2026-10-19 11:00:00", "properties": {"$browser": "Chrome"}},
    ]
    s = Store(ev, now="2026-10-20 12:00:00")
    r = s.query("SELECT countIf(event = 'invoice_created') AS n, sum(toFloat(properties.amount)) AS amt, uniq(distinct_id) AS users "
                "FROM events WHERE timestamp > now() - INTERVAL 7 DAY")
    assert r["columns"] == ["n", "amt", "users"]
    assert r["results"] == [[1, 120.0, 1]]


def test_nested_group_key():
    ev = [{"event": "invoice_created", "distinct_id": "u1", "timestamp": "2026-10-19 10:00:00", "properties": {"$groups": {"company": "acme"}}},
          {"event": "invoice_created", "distinct_id": "u2", "timestamp": "2026-10-19 11:00:00", "properties": {"$groups": {"company": "zeta"}}}]
    s = Store(ev, now="2026-10-20 12:00:00")
    r = s.query("SELECT properties.$groups.company AS company, count() AS n FROM events GROUP BY company ORDER BY company")
    assert r["results"] == [["acme", 1], ["zeta", 1]]
