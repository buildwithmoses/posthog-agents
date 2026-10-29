"""Code checks between the model and anything that gets posted.

- Timeline steps must cite an evidence id (E#/R#) gathered for THIS person; anything else is dropped and counted.
- Every timestamp and recording link in the note is rendered by code from evidence, never typed by the model.
- Free text is scrubbed: timestamps, recording ids and evidence ids the model wrote are removed, and any email
  address other than the requester's, phone numbers and card-like numbers are redacted.
- The customer reply draft also loses internal references (links, E#/R# ids, recording ids).
"""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
TIME_RE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}[ T])?\d{1,2}:\d{2}(?::\d{2})?(?:\s?(?:UTC|am|pm|AM|PM))?\b")
REC_RE = re.compile(r"\brec_[A-Za-z0-9_]+\b")
EVID_RE = re.compile(r"\[?\b[ER]\d{1,3}\b\]?")
LINK_RE = re.compile(r"https?://\S+")


def scrub(text: str, requester_email: str, *, customer_facing: bool = False) -> str:
    t = text or ""
    t = EMAIL_RE.sub(lambda m: m.group(0) if m.group(0).lower() == requester_email else "[email removed]", t)
    t = CARD_RE.sub("[number removed]", t)
    t = PHONE_RE.sub("[phone removed]", t)
    t = REC_RE.sub("", t)
    t = TIME_RE.sub("", t)
    if customer_facing:
        t = LINK_RE.sub("", t)
        t = EVID_RE.sub("", t)
    t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"[ \t]{2,}", " ", t).strip()
    return t


def check_citations(draft: dict, evidence: dict) -> tuple[list[dict], list[str]]:
    """Return (valid timeline steps with their evidence, invalid ids)."""
    by_id = {**{e["id"]: e for e in evidence.get("events", [])}, **{r["id"]: r for r in evidence.get("recordings", [])}}
    valid, invalid = [], []
    for step in draft.get("timeline", []):
        eid = str(step.get("evidence_id", "")).strip().strip("[]")
        if eid in by_id:
            valid.append({**step, "evidence_id": eid, "item": by_id[eid]})
        else:
            invalid.append(eid or "(none)")
    for eid in draft.get("cause_evidence_ids", []):
        if str(eid).strip("[]") not in by_id:
            invalid.append(str(eid))
    return valid, invalid


def render_note(ticket: dict, resolution: dict, evidence: dict | None, draft: dict | None, email: str) -> str:
    st = resolution["status"]
    if st == "not_found":
        return (f"**Ticket enricher:** no PostHog person has the email {email}, so nothing was attached. "
                "Names or addresses mentioned in the ticket were not looked up. Ask the requester which email they log in with.")
    if st == "ambiguous":
        return (f"**Ticket enricher:** {resolution['reason']} Nothing was attached, to avoid showing the wrong person's activity. "
                "Confirm which login the requester uses (or merge the duplicate persons) and re-run.")
    if st == "account_mismatch":
        return (f"**Ticket enricher:** {resolution['reason']} Nothing was attached until someone confirms the requester "
                "is allowed to see that account's activity.")
    ev = evidence or {}
    head = (f"**What the requester actually did** (PostHog, {ev['window'][0]} to {ev['window'][1]} UTC)\n"
            f"Plan: {ev.get('plan') or 'unknown'} · Role: {ev.get('role') or 'unknown'}")
    if not ev.get("events"):
        return (head + f"\n\nNo activity in the 48 hours before this ticket. Last seen in PostHog: {ev.get('last_seen') or 'never'}."
                " Ask the customer when the problem happened, then re-run.")
    lines = [head, ""]
    if draft:
        if draft.get("summary"):
            lines += [f"Summary: {scrub(draft['summary'], email)}", ""]
        lines.append("Timeline (UTC):")
        for s in draft.get("_valid_steps", []):
            it = s["item"]
            if s["evidence_id"].startswith("E"):
                lines.append(f"- {it['at']} · `{it['event']}` {it['detail']} — {scrub(s.get('what', ''), email)} [{s['evidence_id']}]")
            else:
                lines.append(f"- {it['start']} recording {it['recording_id']} — {scrub(s.get('what', ''), email)} [{s['evidence_id']}]")
    if ev.get("recordings"):
        lines += ["", "Recordings, best match first:"]
        for r in ev["recordings"]:
            lines.append(f"- {r['id']} {r['start']} to {r['end']} UTC, {r['why']}: {r['link']}")
    if ev.get("flags"):
        lines += ["", "Feature flags seen: " + ", ".join(f"{k} = {v}" for k, v in sorted(ev["flags"].items()))]
    if draft:
        cause_ids = [c for c in draft.get("cause_evidence_ids", []) if c in {x['id'] for x in ev['events'] + ev['recordings']}]
        lines += ["", f"Likely cause: {scrub(draft.get('likely_cause', ''), email)}" + (f" ({', '.join(cause_ids)})" if cause_ids else ""),
                  f"Next step: {scrub(draft.get('next_step', ''), email)}"]
    lines += ["", "A reply to the customer is drafted and waits for @amara's approval."]
    return "\n".join(lines)
