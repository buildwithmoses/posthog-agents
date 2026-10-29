"""PostHog Agents: five agents for one fictional SaaS company that runs on PostHog."""

import sys


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["mock"]:
        from .core.config import MOCK_PORT
        from .mock.server import serve
        serve(int(args[1]) if len(args) > 1 else MOCK_PORT)
        return
    print("usage: posthog-agents mock [port]   |   python -m posthog_agents.<agent>.cli ...")
