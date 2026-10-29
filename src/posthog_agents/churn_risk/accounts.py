"""Account roster: name, plan, seats and owner. In production this comes from the CRM (HubSpot/Salesforce);
PostHog knows usage, not contracts. Here it's the fictional roster that ships with the mock data."""

from __future__ import annotations


def load_accounts() -> dict[str, dict]:
    from .mock_data import ACCOUNTS
    return {a["account_id"]: a for a in ACCOUNTS}
