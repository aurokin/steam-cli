"""Command-line entry point for Steam Agent."""

from __future__ import annotations

from collections.abc import Sequence

from steam_agent.cli import main as cli_main


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI process boundary."""
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
