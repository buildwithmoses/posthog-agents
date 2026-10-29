"""Who is this ticket from? Decided in code, never by the model.

resolve(): normalize the requester email, ask PostHog for persons with exactly that email, and:
  0 persons            -> not_found   (never fall back to names or addresses mentioned in the body)
  2+ persons           -> ambiguous   (duplicate sign-ups: say so, don't guess)
  1 person             -> check the person's account domain matches the email domain, else account_mismatch
The `fuzzy` variant is the regression: it falls back to searching names and addresses from the ticket body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FREE_MAIL = {"gmail.example", "yahoo.example", "outlook.example", "icloud.example"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


@dataclass
class Resolution:
    status: str                      # found | not_found | ambiguous | account_mismatch
    email: str
    person: dict | None = None
    candidates: list[dict] = field(default_factory=list)
    reason: str = ""

    @property
    def person_id(self) -> str | None:
        return self.person["uuid"] if self.person and self.status == "found" else None


def resolve(ph, email: str, *, body: str = "", variant: str = "baseline") -> Resolution:
    email = normalize_email(email)
    found = ph._get("/persons/", email=email).get("results", []) if email else []
    if variant == "fuzzy" and len(found) != 1:
        found = _fuzzy(ph, email, body) or found
        if found:
            return Resolution("found", email, found[0], found, "fuzzy match")
    if not found:
        return Resolution("not_found", email, reason=f"No PostHog person has the email {email}.")
    if len(found) > 1:
        return Resolution("ambiguous", email, candidates=found,
                          reason=f"{len(found)} PostHog persons share the email {email} (likely duplicate sign-ups).")
    person = found[0]
    if variant == "fuzzy":
        return Resolution("found", email, person, found)
    domain = email.split("@")[-1]
    account_domain = (person.get("properties") or {}).get("company_domain", "")
    if domain in FREE_MAIL:
        return Resolution("account_mismatch", email, person, found, "Personal email address: the account can't be confirmed from the domain.")
    if account_domain and domain != account_domain:
        return Resolution("account_mismatch", email, person, found,
                          f"The requester writes from {domain}, but this person's PostHog account belongs to {account_domain}.")
    return Resolution("found", email, person, found)


def _fuzzy(ph, email: str, body: str) -> list[dict]:
    """REGRESSION ONLY: try addresses in the body, then the local part, then any name-looking word."""
    for addr in EMAIL_RE.findall(body or ""):
        hit = ph._get("/persons/", email=addr.lower()).get("results", [])
        if hit:
            return hit[:1]
    local = email.split("@")[0]
    for term in [local, *re.findall(r"\b[A-Z][a-z]{3,}\b", body or "")]:
        hit = ph._get("/persons/", search=term).get("results", [])
        if hit:
            return hit[:1]
    return []
