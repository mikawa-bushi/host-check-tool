#!/usr/bin/env python3
"""AI エージェントツールの「痕跡」を検出する（パス / ファイル内容 / コミット横断）。

検出対象は下の ``TOOLS``（データ駆動・編集可）。各ツールごとに ``paths``
（ファイル名・ディレクトリ）を正規表現で定義する。本リポジトリでは AI コーディング/
エージェントツールのローカル設定（``.claude/`` ``.codex/`` ``.cursor`` ``.aider*``
等）を「パス」で検出する。``content``（内容）検出はあえて付けない: ``Co-Authored-By:
Claude`` トレーラや ``.gitignore`` の記載まで誤検出するため（必要なら各ツールに
``content`` キーを足せば内容検出も可能。MONITOR_AI_TRACE_EXTRA は名前ベースで両方足す）。

モード:
  scripts/check_ai_traces.py <file>...   # pre-commit: 渡された staged ファイルを検査
  scripts/check_ai_traces.py --pre-push  # push されるコミット(origin 未到達)を検査
  scripts/check_ai_traces.py --all       # 全追跡ファイル + 全コミット履歴を一括監査

いずれも痕跡を見つけたら一覧を表示して終了コード 1 を返す。
追加ツールは ``MONITOR_AI_TRACE_EXTRA="foo,bar"`` で名前ベースで足せる。
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys

# --- 検出対象ツール定義（ここを編集すれば対象を追加・調整できる） -----------
# このリポジトリで弾きたい AI コーディング/エージェントツールのローカル設定。
# 「パス」のみで検出する（content は付けない）。理由はモジュール docstring 参照。
# .gitignore の除外対象と対応させてある。
TOOLS: list[dict] = [
    {"name": "Claude (Claude Code)", "paths": [r"(^|/)\.claude(/|$)"]},
    {"name": "Codex", "paths": [r"(^|/)\.codex(/|$)"]},
    {"name": "Cursor", "paths": [r"(^|/)\.cursor"]},
    {"name": "Aider", "paths": [r"(^|/)\.aider"]},
    {"name": "Continue", "paths": [r"(^|/)\.continue(/|$)"]},
    {"name": "Windsurf", "paths": [r"(^|/)\.windsurf(/|$)"]},
    {"name": "AI agent dir (.agent/.agents)", "paths": [r"(^|/)\.agents?(/|$)"]},
]
# 環境変数で対象ツールを追加（名前ベース: .<name>/ ディレクトリと名前文字列）
for _extra in (
    s.strip() for s in os.environ.get("MONITOR_AI_TRACE_EXTRA", "").split(",")
):
    if _extra:
        TOOLS.append(
            {
                "name": _extra,
                "paths": [rf"(^|/)\.{re.escape(_extra.lower())}(/|$)"],
                "content": [re.escape(_extra)],
            }
        )

for _t in TOOLS:
    _t["_paths"] = [re.compile(p, re.IGNORECASE) for p in _t.get("paths", [])]
    _t["_content"] = [re.compile(p, re.IGNORECASE) for p in _t.get("content", [])]

# 検出器自身の定義/ドキュメントはツール名を正当に含むため対象外にする。
# 追加除外は .ai-trace-ignore（1 行 1 glob、# 始まりはコメント）で指定可能。
SELF_EXCLUDE = [
    "scripts/check_ai_traces.py",
    ".pre-commit-config.yaml",
    "README.md",
    "README_ja.md",
]
try:
    with open(".ai-trace-ignore", encoding="utf-8") as _fh:
        SELF_EXCLUDE += [
            ln.strip() for ln in _fh if ln.strip() and not ln.lstrip().startswith("#")
        ]
except OSError:
    pass


def _excluded(path: str) -> bool:
    norm = path.replace(os.sep, "/")
    return any(fnmatch.fnmatch(norm, pat) for pat in SELF_EXCLUDE)


# (kind, tool, location, snippet)
Finding = tuple


def _git(args: list[str]) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def _read_text(path: str) -> str | None:
    """テキストファイルなら内容を返す。バイナリ/読めない場合は None。"""
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(1_000_000)  # 上限 1MB
    except (OSError, IsADirectoryError):
        return None
    if b"\x00" in chunk:
        return None
    return chunk.decode("utf-8", errors="replace")


def _match_path(path: str) -> list[Finding]:
    norm = path.replace(os.sep, "/")
    out = []
    for tool in TOOLS:
        if any(p.search(norm) for p in tool["_paths"]):
            out.append(("path", tool["name"], path, ""))
    return out


def _match_text(text: str, location: str) -> list[Finding]:
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for tool in TOOLS:
            if any(p.search(line) for p in tool["_content"]):
                out.append(
                    (
                        "content",
                        tool["name"],
                        f"{location}:{lineno}",
                        line.strip()[:120],
                    )
                )
    return out


def scan_files(paths: list[str]) -> list[Finding]:
    """ファイルパスとテキスト内容を検査（pre-commit / 監査の作業ツリー用）。"""
    findings: list[Finding] = []
    for path in paths:
        if not path or path.startswith("--") or _excluded(path):
            continue
        findings += _match_path(path)
        text = _read_text(path)
        if text is not None:
            findings += _match_text(text, path)
    return findings


def scan_commits(shas: list[str]) -> list[Finding]:
    """コミットのメタデータ・追加された行・変更パスを検査。"""
    findings: list[Finding] = []
    for sha in shas:
        short = sha[:10]
        meta = _git(
            ["show", "-s", "--format=%an%x1f%ae%x1f%cn%x1f%ce%x1f%s%x1f%b", sha]
        )
        for field in meta.split("\x1f"):
            findings += [
                ("commit-meta", tool, f"{short}", field.strip()[:120])
                for (_k, tool, _loc, _s) in _match_text(field, short)
            ]
        current_file = ""
        patch = _git(["show", "--no-color", "--format=", sha])
        for line in patch.splitlines():
            if line.startswith("diff --git "):
                current_file = ""  # +++ b/ を見るまで未確定
            elif line.startswith("+++ b/"):
                current_file = line[6:].strip()
                if not _excluded(current_file):
                    for _k, tool, _loc, _s in _match_path(current_file):
                        findings.append(
                            ("commit-path", tool, f"{short} {current_file}", "")
                        )
            elif line.startswith("+") and not line.startswith("+++"):
                if _excluded(current_file):
                    continue
                for _k, tool, _loc, _s in _match_text(line[1:], short):
                    findings.append(
                        ("commit-content", tool, short, line[1:].strip()[:120])
                    )
    return findings


def _outgoing_shas() -> list[str]:
    return _git(["rev-list", "HEAD", "--not", "--remotes=origin"]).split()


def _report(findings: list[Finding], header: str) -> int:
    uniq = sorted(set(findings))
    if not uniq:
        return 0
    print(
        f"\n⛔ {header}: AI エージェントツールの痕跡を検出しました。\n", file=sys.stderr
    )
    for kind, tool, location, snippet in uniq:
        tail = f"  | {snippet}" if snippet else ""
        print(f"  [{kind}] {tool}: {location}{tail}", file=sys.stderr)
    print(
        "\n対処: 該当ファイル/行を削除するか、履歴に入っている場合は filter-branch 等で"
        "書き換えてください。検出対象は scripts/check_ai_traces.py の TOOLS で調整可。",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str]) -> int:
    if "--all" in argv:
        tracked = _git(["ls-files"]).splitlines()
        all_shas = _git(["rev-list", "--all"]).split()
        findings = scan_files(tracked) + scan_commits(all_shas)
        return _report(findings, "全履歴監査")
    if "--pre-push" in argv:
        return _report(scan_commits(_outgoing_shas()), "push 前検査")
    # 既定: pre-commit から渡された staged ファイル
    return _report(scan_files(argv), "commit 前検査")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
