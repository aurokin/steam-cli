"""Opt-in agent-execution evaluation runner.

This package is development tooling: it is never shipped with the wheel and
normal CI only imports the deterministic pieces (materializer, grader). The
Codex App Server driver requires a locally installed ``codex`` binary and is
exercised manually via ``uv run python -m evals.runner``.
"""
