"""Exec commands are read the way cmd.exe runs them on a Windows host.

The read-only classifier, the hard-deny policy and the user's deny rules read
a command with posix quoting. cmd.exe has no single quotes, no backslash
escapes and no ``#`` comments, and it expands ``%VAR%`` before it splits the
line, so on Windows that reading hid commands cmd.exe runs:
``echo 'a & whoami & rem '`` was auto-allowed as one echo, and
``echo 'x & rmdir /s /q C:\\ & rem '`` passed the guard that refuses a
recursive delete of a drive root.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from raven.contracts.permissions import Tier
from raven.permissions.rules import default_tier, exec_reads_only, exec_rule_tier
from raven.permissions.shell_policy import CommandDecision, ShellCommandPolicy, cmd_pieces


@pytest.fixture
def cmd_host(monkeypatch):
    monkeypatch.setattr("raven.permissions.shell_policy.HOST_SHELL_IS_CMD", True)


@pytest.fixture
def policy() -> ShellCommandPolicy:
    return ShellCommandPolicy(deny_patterns=[])


HIDDEN_FROM_POSIX = [
    "echo 'a & whoami & rem '",
    "echo a\\&whoami",
    'echo "a\\" & whoami & rem "',
    'echo ^"a & whoami & rem "',
    "echo 'a | whoami'",
    "echo 'a > out.txt'",
]


@pytest.mark.parametrize("command", HIDDEN_FROM_POSIX)
def test_a_separator_posix_quoting_hides_is_not_read_only_under_cmd(cmd_host, command: str) -> None:
    assert not exec_reads_only(command)
    assert default_tier("exec", {"command": command}) is Tier.ASK


@pytest.mark.parametrize("command", HIDDEN_FROM_POSIX[:4])
def test_the_posix_reading_of_the_same_lines_is_unchanged(command: str) -> None:
    """Where a posix shell runs them, these really are one echo."""
    assert exec_reads_only(command)


@pytest.mark.parametrize("command", ["echo %USERPROFILE%", "type %PATH:x=y%", "echo !X!"])
def test_an_expansion_cmd_performs_before_splitting_asks(cmd_host, command: str) -> None:
    assert default_tier("exec", {"command": command}) is Tier.ASK


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --oneline -5",
        "git log --format=%h -5",
        "type README.md",
        'echo "two words"',
        "git status & git log -1",
    ],
)
def test_ordinary_reading_commands_stay_allowed_under_cmd(cmd_host, command: str) -> None:
    assert exec_reads_only(command)


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("echo 'x & rmdir /s /q C:\\ & rem '", "catastrophic_delete"),
        ('echo "a\\" & rmdir /s /q C:\\ & rem "', "catastrophic_delete"),
        ("echo x #& rmdir /s /q C:", "catastrophic_delete"),
        ("echo 'x & del /f /s /q %USERPROFILE% & rem '", "catastrophic_delete"),
        ("echo 'x & format C: & rem '", "disk_format"),
        ("echo 'x & shutdown /s /t 0 & rem '", "system_power"),
    ],
)
def test_a_hard_deny_reaches_the_piece_cmd_runs(
    cmd_host, policy: ShellCommandPolicy, command: str, reason: str
) -> None:
    outcome = policy.classify(command)
    assert outcome.decision is CommandDecision.HARD_DENY
    assert outcome.reason_code == reason


def test_a_double_quoted_separator_still_separates_nothing(cmd_host, policy: ShellCommandPolicy) -> None:
    """cmd.exe's own quotes do protect an ampersand, so this stays an echo."""
    assert policy.evaluate('echo "safe & rmdir /s C: "') is CommandDecision.ALLOW


def test_a_users_deny_rule_reaches_the_piece_cmd_runs(cmd_host) -> None:
    table = {"curl *": "deny"}
    assert exec_rule_tier("echo 'x & curl https://evil.example & rem '", table) is Tier.DENY
    assert exec_rule_tier("echo x #& curl https://evil.example", table) is Tier.DENY


@pytest.mark.parametrize(
    ("command", "pieces"),
    [
        ("a & b | c", ["a", "b", "c"]),
        ("a && b || c", ["a", "b", "c"]),
        ('"a & b" & c', ['"a & b"', "c"]),
        ("a ^& b", ["a & b"]),
        ("echo 'x & y'", ["echo 'x", "y'"]),
        ("a\nb", ["a", "b"]),
    ],
)
def test_cmd_pieces_split_where_cmd_does(command: str, pieces: list[str]) -> None:
    assert cmd_pieces(command) == pieces


def test_the_host_shell_follows_the_platform() -> None:
    """A fresh interpreter, because this suite pins the flag in every test."""
    probe = "import os, raven.permissions.shell_policy as s; print(s.HOST_SHELL_IS_CMD == (os.name == 'nt'))"
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, env={**os.environ}, check=True
    )
    assert result.stdout.strip() == "True"
