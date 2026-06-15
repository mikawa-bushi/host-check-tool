#!/usr/bin/env python3
"""push 前に author/committer メールの許可リストを検証する pre-push ガード。

origin のどのブランチにも未到達のコミット（＝これから push されるコミット）を
走査し、author か committer のメールが許可リスト外なら push を中止する。
秘密情報スキャナはファイル内容しか見ず、コミットの著者メタデータは検査しない
ため、社用/別 identity の混入（例: ``@example.com``）はこのガードで防ぐ。

許可リストは ``MONITOR_ALLOWED_EMAILS``（カンマ区切り、完全一致を追加）で拡張可。
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

_FS = "\x1f"  # フィールド区切り（コミットメッセージと衝突しない制御文字）


def _allowed(email: str) -> bool:
    if email in ALLOWED_EXACT:
        return True
    return any(p.match(email) for p in ALLOWED_PATTERNS)


def _outgoing_commits() -> list[tuple[str, str, str, str]]:
    """origin に未到達のコミットを (sha, author_email, committer_email, subject) で返す。"""
    result = subprocess.run(
        [
            "git",
            "rev-list",
            "HEAD",
            "--not",
            "--remotes=origin",
            f"--format=%H{_FS}%ae{_FS}%ce{_FS}%s",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    commits = []
    for line in result.stdout.splitlines():
        if _FS not in line:  # rev-list が挟む "commit <sha>" 行はスキップ
            continue
        sha, author_email, committer_email, subject = line.split(_FS, 3)
        commits.append((sha, author_email, committer_email, subject))
    return commits


def main() -> int:
    for extra in os.environ.get("MONITOR_ALLOWED_EMAILS", "").split(","):
        extra = extra.strip()
        if extra:
            ALLOWED_EXACT.add(extra)

    try:
        commits = _outgoing_commits()
    except subprocess.CalledProcessError as exc:
        print(f"check-commit-identity: git rev-list に失敗: {exc}", file=sys.stderr)
        return 1

    offenders = []
    for sha, author_email, committer_email, subject in commits:
        bad = {
            role: email
            for role, email in (
                ("author", author_email),
                ("committer", committer_email),
            )
            if not _allowed(email)
        }
        if bad:
            offenders.append((sha[:10], subject, bad))

    if not offenders:
        return 0

    print(
        "\n⛔ push を中止しました: 許可されていない author/committer メールを検出。\n",
        file=sys.stderr,
    )
    for sha, subject, bad in offenders:
        for role, email in bad.items():
            print(f"  {sha}  [{role}: {email}]  {subject}", file=sys.stderr)
    allow = ", ".join(sorted(ALLOWED_EXACT)) + ", *@users.noreply.github.com"
    print(f"\n許可リスト: {allow}", file=sys.stderr)
    print(
        "対処: `git config user.email <正しいメール>` を設定し、該当コミットを "
        "rebase/amend で修正してから push してください。\n"
        "（許可リストの拡張は MONITOR_ALLOWED_EMAILS 環境変数で可能）",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
