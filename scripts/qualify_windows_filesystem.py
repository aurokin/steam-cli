#!/usr/bin/env python3
"""Qualify local NTFS manifest primitives using only disposable owned files."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

from steam_agent.execution.windows_filesystem import WindowsFilesystem, WindowsFilesystemError


def main() -> int:
    phase = "platform"
    try:
        if sys.platform != "win32":
            raise OSError("Windows required")
        with tempfile.TemporaryDirectory(prefix="steam-agent-ntfs-") as temporary:
            root = Path(temporary)
            library = root / "library 雪"
            steamapps = library / "steamapps"
            steamapps.mkdir(parents=True)
            journals = root / "journals"
            journals.mkdir()
            source = root / "source.acf"
            source.write_text('"AppState" { "appid" "10" "installdir" "Original" "StateFlags" "4" }', encoding="utf-8")
            destination = steamapps / "appmanifest_10.acf"
            old = b'"AppState" { "appid" "10" "installdir" "Old" }'
            destination.write_bytes(old)
            driver = WindowsFilesystem()
            phase = "identity"
            assert driver.identity(steamapps) == driver.identity(steamapps)
            phase = "adoption"
            driver.adopt_manifest(source=source, library=library, appid=10, install_dir_name="Game 雪", journal_dir=journals, operation_id=7)
            assert destination.read_bytes() != old
            phase = "recovery"
            assert driver.reconcile_adoption(library=library, appid=10, journal_dir=journals, operation_id=7) == "completed"
            driver.clear_adoption_journal(appid=10, journal_dir=journals)
            assert driver.reconcile_adoption(library=library, appid=10, journal_dir=journals) == "clean"
            phase = "hardlink"
            os.link(source, root / "second.acf")
            try:
                driver.identity(source, directory=False)
            except WindowsFilesystemError as error:
                assert error.code == "WINDOWS_HARDLINK_REFUSED"
            else:
                raise AssertionError("hardlink accepted")
            phase = "reparse"
            target = root / "target"
            target.mkdir()
            (target / "child.acf").write_bytes(b"owned fixture")
            link = root / "link"
            reparse = "unverified_no_privilege"
            try:
                os.symlink(target, link, target_is_directory=True)
            except OSError:
                pass
            else:
                for path, directory in ((link, True), (link / "child.acf", False)):
                    try:
                        driver.identity(path, directory=directory)
                    except WindowsFilesystemError as error:
                        assert error.code == "WINDOWS_REPARSE_POINT_REFUSED"
                    else:
                        raise AssertionError("reparse accepted")
                reparse = "refused"
        print(json.dumps({"platform": "windows", "identity": True, "manifest_recovery": True, "hardlink_refused": True, "reparse": reparse, "steam_actions": 0}, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({"platform": "windows", "phase": phase, "failure": type(error).__name__, "steam_actions": 0}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
