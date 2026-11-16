"""What a diagnosis says about itself: waits, commands, thresholds and fallbacks.

* ``あと N 時間`` is a lower bound for MEM-PRESSURE-01, which counts local days
  once its 24 hours are in, so the wait is worded "at least".
* A name that starts with ``-`` stays a name in ``systemctl`` / ``df`` / ``du``.
* DISK-INODE-01 cuts the capacity figure at the levels DISK-FULL-01 uses.
* A threshold that falls back to its default is said only for a rule that ran,
  and ``compare`` shows it as ``check`` does.
* CPU-PERIODIC-01 names the tolerance it really used.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner
from synth import add_spike_events, add_sys_baseline, disk_row, make_db
from test_compare_cli import _clean_db

from procdiag.cli import cli
from procdiag.config import ProcdiagConfig, RulesConfig
from procdiag.core import Confidence, Finding, Severity, Window, configure_rule
from procdiag.diagnosis import Engine
from procdiag.diagnosis.io_disk import DiskInodeRule

_H = 3600.0
_D = 86400.0
# 2026-11-02 00:00 UTC; the clock of these tests is the data's, not the host's.
_DAY0 = datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def utc_host(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _past_db(directory, *, hours: float, start: float):
    directory.mkdir(parents=True, exist_ok=True)
    store = make_db(directory, hours=hours)
    add_sys_baseline(store, hours=hours, start_ts=start)
    store.close()
    return store.path


def _run(*args):
    return CliRunner().invoke(cli, [*map(str, args)])


class TestTheWaitIsALowerBound:
    def test_compare_says_at_least(self, tmp_path):
        before = _clean_db(tmp_path / "a", hours=2)
        after = _clean_db(tmp_path / "b", hours=2)

        conclusion = _run("compare", "--no-color", before, after).stdout.split("結果:")[
            1
        ]

        assert "あと少なくとも 22.0 時間" in conclusion
        assert "あと最長" not in conclusion

    def test_check_says_at_least(self, tmp_path):
        db = _clean_db(tmp_path / "a", hours=2)

        out = _run("check", "--no-color", "--db", db).stdout

        (line,) = [l for l in out.splitlines() if l.startswith("⚠ 評価範囲")]
        assert "あと少なくとも 22.0 時間" in line
        assert "あと最長 22.0 時間の記録で全ルール" not in line

    def test_a_day_can_still_be_short_after_the_waited_hours(self, tmp_path, utc_host):
        """Why "at least": recorded from 20:00, 24 hours hold no second whole day."""
        start = _DAY0 + 20 * _H
        short = _past_db(tmp_path / "a", hours=2, start=start)
        long = _past_db(tmp_path / "b", hours=24, start=start)
        window = Window(start_ts=start, end_ts=start + 24 * _H)

        from procdiag.storage import Storage

        with Storage(short, read_only=True) as store:
            first = Engine(ProcdiagConfig()).run(
                store, only=["MEM-PRESSURE-01"], window=Window(start, start + 2 * _H)
            )
        with Storage(long, read_only=True) as store:
            second = Engine(ProcdiagConfig()).run(
                store, only=["MEM-PRESSURE-01"], window=window
            )

        (waiting,) = first.skipped_rules
        assert waiting.remaining_hours == pytest.approx(22.0)
        (still,) = second.skipped_rules
        assert still.remaining_hours > 1.0


class _Evidence(dict):
    """Evidence whose figures nobody set are 0, so a template renders from the names alone."""

    def __missing__(self, key: str) -> int:
        return 0


def _commands(rule_id: str, **evidence) -> list[str]:
    finding = Finding(
        rule_id=rule_id,
        severity=Severity.WARNING,
        subject="x",
        summary="s",
        evidence=_Evidence(evidence),
        detected_at=(0.0, 1.0),
        confidence=Confidence.MEDIUM,
    )
    text = Engine(ProcdiagConfig())._render_proposal(finding, [])
    return re.findall(r"`([^`]*)`", text)


def _argv(commands: list[str], program: str, *, has: str = "") -> list[str]:
    (found,) = [c for c in commands if c.startswith(program) and has in c]
    return shlex.split(found)


class TestANameThatLooksLikeAnOptionStaysAName:
    NAME = "-x"

    def test_systemctl_status(self):
        commands = _commands(
            "SERVICE-RESTART-01", unit="-x.service", restarts=2, results=[]
        )

        assert _argv(commands, "systemctl status")[:4] == [
            "systemctl",
            "status",
            "--",
            "-x.service",
        ]

    def test_journalctl_takes_the_name_as_the_argument_of_u(self):
        commands = _commands(
            "SERVICE-RESTART-01", unit="-x.service", restarts=2, results=[]
        )

        argv = _argv(commands, "journalctl")
        assert argv[argv.index("-u") + 1] == "-x.service"

    def test_du_of_a_full_mount(self):
        commands = _commands("DISK-FULL-01", mountpoint=self.NAME)

        assert _argv(commands, "du -sh")[:4] == ["du", "-sh", "--", "-x/*"]

    def test_du_of_a_growing_directory(self):
        commands = _commands("DISK-GROWTH-01", path=self.NAME)

        assert _argv(commands, "du -sh")[:4] == ["du", "-sh", "--", "-x/*"]

    def test_df_and_du_of_an_inode_shortage(self):
        commands = _commands("DISK-INODE-01", mountpoint=self.NAME)

        assert _argv(commands, "df -i") == ["df", "-i", "--", "-x"]
        assert _argv(commands, "sudo du --inodes")[:6] == [
            "sudo",
            "du",
            "--inodes",
            "-x",
            "--",
            "-x",
        ]


class TestInodeFindingsCutTheCapacityAtTheLevelsInUse:
    @staticmethod
    def _shown(tmp_path, overrides) -> float:
        store = make_db(tmp_path, hours=3)
        store.buffer(
            "disk_samples",
            [
                disk_row(
                    ts=time.time() - 60,
                    mountpoint="/var",
                    total=10000,
                    used=7996,
                    inodes_total=1000,
                    inodes_used=960,
                )
            ],
        )
        store.flush()
        config = ProcdiagConfig(rules=RulesConfig(overrides=overrides))
        rule, error = configure_rule(DiskInodeRule, config)
        assert error is None
        now = time.time()
        (finding,) = rule.evaluate(store, Window(now - 3 * _H, now))
        store.close()
        return finding.evidence["bytes_used_pct"]

    def test_a_lowered_warning_level_is_not_reached_by_rounding(self, tmp_path):
        assert self._shown(tmp_path, {"DISK-FULL-01": {"warning_pct": 80.0}}) == 79.9

    def test_a_lowered_critical_level_is_not_reached_by_rounding(self, tmp_path):
        assert self._shown(tmp_path, {"DISK-FULL-01": {"critical_pct": 80.0}}) == 79.9

    def test_the_default_levels_still_round_normally(self, tmp_path):
        assert self._shown(tmp_path, {}) == 80.0


_NOT_USABLE = {"IO-BOTTLENECK-01": {"top_n": 0}}


class TestTheFallbackNoteIsForRulesThatRan:
    @staticmethod
    def _diagnose(tmp_path, overrides, *, hours):
        store = make_db(tmp_path, hours=hours)
        add_sys_baseline(store, hours=hours)
        store.conn.commit()
        diag = Engine(ProcdiagConfig(rules=RulesConfig(overrides=overrides))).run(store)
        store.close()
        return diag

    def test_a_rule_skipped_for_the_hours_it_needs_adds_no_note(self, tmp_path):
        diag = self._diagnose(tmp_path, _NOT_USABLE, hours=0.2)

        assert "IO-BOTTLENECK-01" in [r.rule_id for r in diag.skipped_rules]
        assert [n for n in diag.quality.notes if "top_n" in n] == []
        assert [r for r in diag.reasoning if "既定値で評価" in r] == []

    def test_a_rule_without_its_rows_adds_no_note(self, tmp_path):
        diag = self._diagnose(tmp_path, {"OOM-KILL-01": {"max_findings": 0}}, hours=3)

        assert "OOM-KILL-01" in [r.rule_id for r in diag.uncovered_rules]
        assert [n for n in diag.quality.notes if "max_findings" in n] == []

    def test_a_rule_that_ran_still_does(self, tmp_path):
        diag = self._diagnose(
            tmp_path, {"KERNEL-ERROR-01": {"max_samples": 0}}, hours=3
        )

        assert "KERNEL-ERROR-01" in diag.evaluated_rules
        assert len([n for n in diag.quality.notes if "max_samples" in n]) == 1


class TestCompareShowsTheFallbackToo:
    @pytest.fixture
    def config(self, tmp_path):
        path = tmp_path / "config.py"
        path.write_text(
            "from procdiag.config import ProcdiagConfig, RulesConfig\n"
            "config = ProcdiagConfig(rules=RulesConfig(\n"
            '    overrides={"KERNEL-ERROR-01": {"max_samples": 0}}))\n',
            encoding="utf-8",
        )
        return path

    def test_the_text_names_the_threshold_once(self, tmp_path, config):
        before = _clean_db(tmp_path / "a", hours=30)
        after = _clean_db(tmp_path / "b", hours=30)

        out = _run("compare", "--no-color", "--config", config, before, after).stdout

        lines = [l for l in out.splitlines() if "max_samples" in l]
        assert len(lines) == 1, out
        assert lines[0].startswith("注意: ")
        assert "既定値" in lines[0]

    def test_json_has_it_as_a_note(self, tmp_path, config):
        before = _clean_db(tmp_path / "a", hours=30)
        after = _clean_db(tmp_path / "b", hours=30)

        data = json.loads(
            _run(
                "compare", "--format", "json", "--config", config, before, after
            ).stdout
        )

        (note,) = [n for n in data["notes"] if n["kind"] == "config_fallback"]
        assert "max_samples" in note["message"]

    def test_without_a_fallback_there_is_no_note(self, tmp_path):
        before = _clean_db(tmp_path / "a", hours=30)
        after = _clean_db(tmp_path / "b", hours=30)

        data = json.loads(_run("compare", "--format", "json", before, after).stdout)

        assert [n for n in data["notes"] if n["kind"] == "config_fallback"] == []


class TestCpuPeriodicNamesTheToleranceItUsed:
    @staticmethod
    def _finding(tmp_path, tol_sec, monkeypatch):
        monkeypatch.setenv("TZ", "UTC")
        time.tzset()
        spikes = [_DAY0 + d * _D + 14 * _H + 20 for d in range(7)]
        store = make_db(tmp_path, hours=7 * 24)
        add_sys_baseline(store, hours=7 * 24, start_ts=_DAY0)
        add_spike_events(store, "cpu", spikes)
        config = ProcdiagConfig(
            rules=RulesConfig(overrides={"CPU-PERIODIC-01": {"tol_sec": tol_sec}})
        )
        diag = Engine(config).run(store, only=["CPU-PERIODIC-01"])
        store.close()
        (finding,) = diag.findings
        return finding

    def teardown_method(self):
        time.tzset()

    @pytest.mark.parametrize(
        ("tol_sec", "shown"), [(0, "±1秒"), (30, "±30秒"), (120, "±2分")]
    )
    def test_the_proposal_says_the_tolerance_in_force(
        self, tmp_path, monkeypatch, tol_sec, shown
    ):
        finding = self._finding(tmp_path, tol_sec, monkeypatch)

        assert shown in finding.proposal
        assert "±0分" not in finding.proposal

    def test_the_evidence_holds_the_tolerance_in_force(self, tmp_path, monkeypatch):
        finding = self._finding(tmp_path, 0, monkeypatch)

        assert finding.evidence["tol_minutes"] * 60 == pytest.approx(1.0)
