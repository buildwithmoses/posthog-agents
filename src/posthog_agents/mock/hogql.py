"""Run HogQL against DuckDB, close enough for the queries these agents send.

Translation (HogQL -> DuckDB):
  properties.$foo / properties.foo     -> json_extract_string(properties, '$."$foo"')  (numbers: toFloat(...) works)
  properties.$groups.company          -> json_extract_string(properties, '$."$groups"."company"')
  person.properties.x                  -> json_extract_string(person_properties, '$."x"')
  toStartOfDay(x)/toStartOfWeek(x)     -> date_trunc('day'|'week', x)
  toDate(x) toFloat(x) toInt(x) toString(x) -> CAST
  countIf(c) / sumIf(x, c) / avgIf(x, c)  -> count(*) FILTER (WHERE c) / sum(x) FILTER (...) / avg(x) FILTER (...)
  quantile(p)(x)                       -> quantile_cont(x, p)
  now() and INTERVAL n DAY work as-is. {placeholder} values are substituted as literals.
`now()` is pinned to the fixture's clock (Store.now) so results are reproducible.
Tables: events(uuid, event, distinct_id, timestamp, properties JSON, person_id, person_properties JSON, $session_id)
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import duckdb


def translate(q: str, values: dict[str, Any] | None = None, now: str | None = None) -> str:
    for k, v in (values or {}).items():
        lit = str(v) if isinstance(v, (int, float)) else "'" + str(v).replace("'", "''") + "'"
        q = q.replace("{" + k + "}", lit)
    q = re.sub(r"person\.properties\.\$?([A-Za-z0-9_$]+)", lambda m: f"json_extract_string(person_properties, '$.\"{m.group(0).split('.',2)[2]}\"')", q)
    # Nested keys such as properties.$groups.company -> json path $."$groups"."company"
    q = re.sub(r"(?<![\w.])properties((?:\.\$?[A-Za-z0-9_]+)+)",
               lambda m: "json_extract_string(properties, '$" + "".join(f'.\"{k}\"' for k in m.group(1).strip('.').split('.')) + "')", q)
    q = re.sub(r"\btoStartOfDay\(", "date_trunc('day', ", q)
    q = re.sub(r"\btoStartOfWeek\(", "date_trunc('week', ", q)
    q = re.sub(r"\btoStartOfHour\(", "date_trunc('hour', ", q)
    q = re.sub(r"\btoFloat\(", "TRY_CAST_FLOAT(", q)
    q = re.sub(r"\btoInt\(", "TRY_CAST_INT(", q)
    q = re.sub(r"\btoDate\(", "TRY_CAST_DATE(", q)
    q = re.sub(r"\btoString\(", "TRY_CAST_STR(", q)
    q = re.sub(r"\bquantile\(([0-9.]+)\)\(([^)]+)\)", r"quantile_cont(\2, \1)", q)
    q = re.sub(r"\bcountIf\(", "COUNTIF(", q)
    if now:
        q = re.sub(r"\bnow\(\)", f"TIMESTAMP '{now}'", q)
    q = re.sub(r"\btoday\(\)", f"CAST(TIMESTAMP '{now}' AS DATE)" if now else "current_date", q)
    return q


class Store:
    def __init__(self, events: list[dict], persons=None, recordings=None, experiments=None, now: str | None = None, handler=None):
        self.now = now or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        self._persons = persons or []
        self._recordings = recordings or []
        self._experiments = experiments or []
        self._handler = handler
        self.db = duckdb.connect()
        for name, fn in [("TRY_CAST_FLOAT", "TRY_CAST(x AS DOUBLE)"), ("TRY_CAST_INT", "TRY_CAST(x AS BIGINT)"),
                         ("TRY_CAST_DATE", "TRY_CAST(x AS DATE)"), ("TRY_CAST_STR", "CAST(x AS VARCHAR)"),
                         ("COUNTIF", "count_if(x)")]:
            self.db.execute(f"CREATE MACRO {name}(x) AS {fn}")
        self.db.execute("CREATE MACRO sumIf(x, c) AS sum(CASE WHEN c THEN x END)")
        self.db.execute("CREATE MACRO avgIf(x, c) AS avg(CASE WHEN c THEN x END)")
        self.db.execute("CREATE MACRO uniq(x) AS count(DISTINCT x)")
        self.db.execute("""CREATE TABLE events (uuid VARCHAR, event VARCHAR, distinct_id VARCHAR, timestamp TIMESTAMP,
            properties JSON, person_id VARCHAR, person_properties JSON, "$session_id" VARCHAR)""")
        rows = [(e.get("uuid") or f"e{i}", e["event"], e["distinct_id"], e["timestamp"], json.dumps(e.get("properties", {})),
                 e.get("person_id") or e["distinct_id"], json.dumps(e.get("person_properties", {})),
                 (e.get("properties") or {}).get("$session_id")) for i, e in enumerate(events)]
        if rows:
            self.db.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)

    @classmethod
    def from_module(cls, mod) -> "Store":
        get = lambda n: getattr(mod, n)() if hasattr(mod, n) else None  # noqa: E731
        return cls(get("load_events") or [], get("load_persons"), get("load_recordings"), get("load_experiments"),
                   getattr(mod, "NOW", None), getattr(mod, "handle", None))

    def query(self, hogql: str, values: dict | None = None) -> dict:
        sql = translate(hogql, values, self.now)
        cur = self.db.execute(sql)
        cols = [d[0] for d in cur.description]
        res = [[v.isoformat(sep=" ") if isinstance(v, datetime) else v for v in row] for row in cur.fetchall()]
        return {"columns": cols, "results": res, "hogql": hogql}

    def list_recordings(self, params: dict) -> list[dict]:
        out = [{k: v for k, v in r.items() if k != "snapshots"} for r in self._recordings]
        if params.get("person_uuid") or params.get("distinct_id"):
            want = params.get("person_uuid") or params.get("distinct_id")
            out = [r for r in out if r.get("distinct_id") == want or r.get("person", {}).get("uuid") == want]
        return out

    def recording(self, rid: str) -> dict | None:
        return next((r for r in self._recordings if r["id"] == rid), None)

    def persons(self, distinct_id: str | None) -> list[dict]:
        return [p for p in self._persons if distinct_id is None or distinct_id in p.get("distinct_ids", [])]

    def experiment(self, eid: int) -> dict | None:
        return next((x for x in self._experiments if x["id"] == eid), None)

    def handle(self, path: str, method: str, body: Any, params: dict) -> Any:
        return self._handler(path, method, body, params) if self._handler else None
