#!/usr/bin/env python3
"""push 前に author/committer の名前とメールを許可リストで検証する pre-push ガード。

origin のどのブランチにも未到達のコミット（＝これから push されるコミット）を
走査し、author か committer の名前・メールが許可リスト外なら push を中止する。
秘密情報スキャナはファイル内容しか見ず、コミットの著者メタデータは検査しない
ため、別 identity の混入（例: ``@example.com``）や、メールは noreply でも
名前に本名が入ったコミットはこのガードで防ぐ。

許可リストは ``MONITOR_ALLOWED_EMAILS`` / ``MONITOR_ALLOWED_NAMES``
（カンマ区切り、完全一致を追加）で拡張可。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

# 完全一致で許可するメール（正規の公開 identity）
ALLOWED_EXACT = {
    "tomatosimple1104@gmail.com",  # hardwork9047
    "noreply@github.com",  # GitHub Web のマージコミット committer
}
# 正規表現で許可するメール
ALLOWED_PATTERNS = [
    re.compile(r".+@users\.noreply\.github\.com$"),  # GitHub のアカウント別 noreply
]
# 完全一致で許可する名前
ALLOWED_NAMES = {
    "hardwork9047",
    "GitHub",  # GitHub Web のマージコミット committer
}

_FS = "\x1f"  # フィールド区切り（コミットメッセージと衝突しない制御文字）


def _allowed(email: str) -> bool:
    if email in ALLOWED_EXACT:
        return True
    return any(p.match(email) for p in ALLOWED_PATTERNS)


def _outgoing_commits() -> list[tuple[str, str, dict[str, str]]]:
    """origin に未到達のコミットを (sha, subject, {"author name": ..., ...}) で返す。"""
    result = subprocess.run(
        [
            "git",
            "rev-list",
            "HEAD",
            "--not",
            "--remotes=origin",
            f"--format=%H{_FS}%an{_FS}%ae{_FS}%cn{_FS}%ce{_FS}%s",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    commits = []
    for line in result.stdout.splitlines():
        if _FS not in line:  # rev-list が挟む "commit <sha>" 行はスキップ
            continue
        sha, an, ae, cn, ce, subject = line.split(_FS, 5)
        identity = {
            "author name": an,
            "author email": ae,
            "committer name": cn,
            "committer email": ce,
        }
        commits.append((sha, subject, identity))
    return commits


def _extend(target: set[str], env: str) -> None:
    for extra in os.environ.get(env, "").split(","):
        extra = extra.strip()
        if extra:
            target.add(extra)


def main() -> int:
    _extend(ALLOWED_EXACT, "MONITOR_ALLOWED_EMAILS")
    _extend(ALLOWED_NAMES, "MONITOR_ALLOWED_NAMES")

    try:
        commits = _outgoing_commits()
    except subprocess.CalledProcessError as exc:
        print(f"check-commit-identity: git rev-list に失敗: {exc}", file=sys.stderr)
        return 1

    offenders = []
    for sha, subject, identity in commits:
        bad = {
            field: value
            for field, value in identity.items()
            if not (
                value in ALLOWED_NAMES if field.endswith("name") else _allowed(value)
            )
        }
        if bad:
            offenders.append((sha[:10], subject, bad))

    if not offenders:
        return 0

    print(
        "\n⛔ push を中止しました: 許可されていない author/committer の名前・メールを検出。\n",
        file=sys.stderr,
    )
    for sha, subject, bad in offenders:
        for field, value in bad.items():
            print(f"  {sha}  [{field}: {value}]  {subject}", file=sys.stderr)
    allow = ", ".join(sorted(ALLOWED_EXACT)) + ", *@users.noreply.github.com"
    print(f"\n許可メール: {allow}", file=sys.stderr)
    print(f"許可する名前: {', '.join(sorted(ALLOWED_NAMES))}", file=sys.stderr)
    print(
        "対処: `git config user.name` / `git config user.email` を正しい値にし、"
        "該当コミットを rebase/amend で修正してから push してください。\n"
        "（許可リストの拡張は MONITOR_ALLOWED_EMAILS / MONITOR_ALLOWED_NAMES 環境変数で可能）",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
