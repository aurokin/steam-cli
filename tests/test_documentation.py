"""Repository documentation hygiene checks."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote


REPO_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"]\(([^)]+)\)")
PERSONAL_HOME = re.compile(r"/(?:Users|home)/[^/\s`)]+/")


RESULTS_ROOT = REPO_ROOT / "evals" / "results"
NAVIGATION_ROOTS = (REPO_ROOT / "AGENTS.md", REPO_ROOT / "README.md")


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in REPO_ROOT.rglob("*.md")
        if ".git" not in path.parts
        and ".venv" not in path.parts
        and not path.is_relative_to(RESULTS_ROOT)
    )


def test_repository_relative_markdown_links_resolve() -> None:
    broken: list[str] = []

    for document in markdown_files():
        for raw_target in MARKDOWN_LINK.findall(document.read_text()):
            target = raw_target.removeprefix("<").removesuffix(">")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative = unquote(target.split("#", 1)[0])
            if not relative:
                continue
            resolved = (document.parent / relative).resolve()
            if not resolved.exists():
                broken.append(
                    f"{document.relative_to(REPO_ROOT)} -> {raw_target}"
                )

    assert broken == []


def test_maintained_documentation_is_reachable_from_entry_points() -> None:
    maintained = {
        REPO_ROOT / "AGENTS.md",
        REPO_ROOT / "CONTRIBUTING.md",
        REPO_ROOT / "README.md",
        REPO_ROOT / "steam-library-agent-research-handoff.md",
        REPO_ROOT / ".agents" / "skills" / "steam-agent" / "SKILL.md",
        *(REPO_ROOT / "docs").rglob("*.md"),
        *(
            path
            for path in (REPO_ROOT / "evals").rglob("*.md")
            if not path.is_relative_to(RESULTS_ROOT)
        ),
    }
    reachable: set[Path] = set()
    pending = list(NAVIGATION_ROOTS)

    while pending:
        document = pending.pop()
        if document in reachable:
            continue
        reachable.add(document)
        for raw_target in MARKDOWN_LINK.findall(document.read_text()):
            target = raw_target.removeprefix("<").removesuffix(">")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative = unquote(target.split("#", 1)[0])
            if not relative:
                continue
            linked = (document.parent / relative).resolve()
            if linked.is_dir():
                linked /= "README.md"
            if linked in maintained and linked not in reachable:
                pending.append(linked)

    unreachable = sorted(
        str(path.relative_to(REPO_ROOT)) for path in maintained - reachable
    )
    assert unreachable == []


def test_design_documents_declare_status_near_top() -> None:
    missing = []
    for document in sorted((REPO_ROOT / "docs" / "design").glob("*.md")):
        first_lines = document.read_text().splitlines()[:6]
        if not any(line.startswith("Status:") for line in first_lines):
            missing.append(str(document.relative_to(REPO_ROOT)))

    assert missing == []


def test_markdown_contains_no_personal_home_paths() -> None:
    violations = []
    for document in markdown_files():
        if PERSONAL_HOME.search(document.read_text()):
            violations.append(str(document.relative_to(REPO_ROOT)))

    assert violations == []
