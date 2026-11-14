"""The pre-push guard must read a history whose diff text is not clean UTF-8."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_ai_traces.py"


def _git(repo: Path, *args: str, check: bool = True) -> None:
    identity = ["-c", "user.name=tester", "-c", "user.email=tester@example.com"]
    subprocess.run(
        ["git", *identity, *args], cwd=repo, check=check, capture_output=True
    )


def _resolved_merge(repo: Path) -> None:
    """A merge commit whose combined diff names a Japanese function line."""
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    header = "x" + "日本語の長い関数名" * 12
    lines = [header] + [f"  line {i}" for i in range(1, 40)]
    (repo / "a.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    original = (repo / "a.txt").read_text(encoding="utf-8")
    _git(repo, "checkout", "-q", "-b", "side")
    side = original.replace("  line 30\n", "  line 30 side\n")
    (repo / "a.txt").write_text(side, encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "side")
    _git(repo, "checkout", "-q", "main")
    main = original.replace("  line 31\n", "  line 31 main\n")
    (repo / "a.txt").write_text(main, encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "main")
    _git(repo, "merge", "-q", "--no-edit", "side", check=False)
    both = original.replace("  line 30\n", "  line 30 side\n")
    both = both.replace("  line 31\n", "  line 31 main\n")
    (repo / "a.txt").write_text(both, encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "--no-edit")


def test_pre_push_scans_a_merge_whose_hunk_header_cuts_a_character(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _resolved_merge(repo)
    monkeypatch.chdir(repo)
    spec = importlib.util.spec_from_file_location("check_ai_traces", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.main(["--pre-push"]) == 0
