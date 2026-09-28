"""Heavy commands wait while host memory is critical; everything else runs at once."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew import resource_status as rs


def _status(posture: str, available_gb: float = 1.0) -> rs.ResourceStatus:
    return rs.ResourceStatus(
        available_gb=available_gb,
        cpu_count=8,
        load_per_cpu=None,
        posture=posture,
        pressure_gb=4.0,
        critical_gb=2.0,
    )


@pytest.mark.parametrize(
    "command",
    [
        "pytest test/test_x.py -q",
        ".venv/bin/python -m pytest -n auto",
        "cd website && npm run build",
        "pnpm test",
        "make build",
        "cargo test --workspace",
        "go test ./...",
        "./gradlew build",
        "npx vitest run",
        "docker build -t x .",
    ],
)
def test_builds_and_test_runs_are_heavy(command: str) -> None:
    assert rs.is_heavy_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "python3 -m pytest -q",
        "env CI=1 pytest",
        "FOO=bar BAZ=1 pytest -x",
        "nice -n 10 make -j8",
        "timeout 600 pytest",
        "sudo make install",
        "cd website && npx --yes vitest run",
        "echo start; make build",
        "git status || make",
        "ls\npytest",
        "ls | tsc",
        "  pytest",
    ],
)
def test_wrapped_and_chained_heavy_programs_are_heavy(command: str) -> None:
    assert rs.is_heavy_command(command)


@pytest.mark.parametrize(
    "command",
    [None, "", "git status", "ls -la", "cmake --version", "npm ci", "gofmt -l ."],
)
def test_ordinary_commands_are_not_heavy(command: str | None) -> None:
    assert not rs.is_heavy_command(command)


@pytest.mark.parametrize(
    "command",
    [
        'grep -rn "pytest" test/',
        'git commit -m "make the build green"',
        "rg 'npm run build' website/",
        "cat Makefile",
        "echo pytest && ls",
        "git log --grep=jest",
        "python -m venv .venv",
        "npx prettier --check .",
        "cd tsc-fixtures && ls",
    ],
)
def test_heavy_words_in_arguments_are_not_heavy(command: str) -> None:
    """Only the PROGRAM a segment runs is judged, never the words it is given."""
    assert not rs.is_heavy_command(command)


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "fix: pytest hangs; make it bounded"',
        "git commit -m 'fix; pytest later'",
        'git commit -m "fix || make && pytest | tsc"',
        "echo 'a\nmake build'",
        'echo "quoted \\" ; pytest"',
        "echo \\; pytest",
    ],
)
def test_separators_inside_quotes_do_not_split(command: str) -> None:
    """A ``;`` or ``&&`` inside a quoted argument starts no new command."""
    assert not rs.is_heavy_command(command)


@pytest.mark.parametrize(
    "command",
    [
        'echo "a;b" && pytest -q',
        "cd x; make build",
        "echo 'done' ; make",
        'echo \\"; pytest',
        "echo 'a' && echo \"b\" || pytest",
    ],
)
def test_separators_outside_quotes_still_split(command: str) -> None:
    assert rs.is_heavy_command(command)


@pytest.mark.parametrize(
    "command, heavy",
    [
        ("true & pytest -n auto", True),
        ("server & playwright test", True),
        ("pytest 2>&1 | tee log", True),
        ("echo hi 2>&1", False),
        ("ls &> out.txt", False),
        ("ls &>> out.txt", False),
        ("echo hi >&2", False),
        ("read x <&3", False),
        ('git commit -m "a & pytest"', False),
    ],
)
def test_a_lone_ampersand_splits_but_a_redirection_does_not(command: str, heavy: bool) -> None:
    """A background ``&`` ends a command; the ``&`` in ``2>&1`` or ``&>f`` does not."""
    assert rs.is_heavy_command(command) is heavy


@pytest.mark.parametrize(
    "command, heavy",
    [
        ('git commit -m "fix; pytest later', False),
        ("echo 'unterminated && make", False),
        ('pytest -k "unterminated', True),
        ("echo done; make \\", True),
    ],
)
def test_unbalanced_quotes_never_raise(command: str, heavy: bool) -> None:
    """An unclosed quote leaves the rest of the line as one segment."""
    assert rs.is_heavy_command(command) is heavy


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, secs: float) -> None:
        self.now += secs


def _run(coro):
    return asyncio.run(coro)


def test_a_heavy_command_waits_until_memory_frees() -> None:
    clock = _Clock()
    postures = iter(["critical", "critical", "tight"])
    ticks: list[float] = []

    async def on_wait() -> None:
        ticks.append(clock.now)

    held = _run(
        rs.hold_heavy_command(
            "pytest -q",
            poll_secs=10.0,
            probe_fn=lambda: _status(next(postures)),
            sleep=clock.sleep,
            clock=clock,
            on_wait=on_wait,
        )
    )
    assert held == pytest.approx(20.0)
    assert ticks == [10.0, 20.0]  # the caller is told it is alive on every poll


def test_the_hold_is_bounded_and_then_runs_anyway() -> None:
    clock = _Clock()
    held = _run(
        rs.hold_heavy_command(
            "make build",
            hold_secs=30.0,
            poll_secs=10.0,
            probe_fn=lambda: _status("critical"),
            sleep=clock.sleep,
            clock=clock,
        )
    )
    assert held == pytest.approx(30.0)


@pytest.mark.parametrize("posture", ["ample", "tight", "unknown"])
def test_only_a_critical_host_holds(posture: str) -> None:
    clock = _Clock()
    held = _run(
        rs.hold_heavy_command(
            "pytest", probe_fn=lambda: _status(posture), sleep=clock.sleep, clock=clock
        )
    )
    assert held == 0.0 and clock.now == 0.0


def test_light_commands_never_probe() -> None:
    def probe_fn() -> rs.ResourceStatus:
        raise AssertionError("a light command must not read the host")

    assert _run(rs.hold_heavy_command("git status", probe_fn=probe_fn)) == 0.0


def test_a_failing_probe_runs_the_command_now() -> None:
    def probe_fn() -> rs.ResourceStatus:
        raise OSError("unreadable")

    assert _run(rs.hold_heavy_command("pytest", probe_fn=probe_fn)) == 0.0


def test_a_zero_bound_disables_the_hold() -> None:
    def probe_fn() -> rs.ResourceStatus:
        raise AssertionError("disabled hold must not read the host")

    assert _run(rs.hold_heavy_command("pytest", hold_secs=0, probe_fn=probe_fn)) == 0.0


def _cfg(gate: bool) -> SimpleNamespace:
    return SimpleNamespace(
        agent=SimpleNamespace(
            resource_pressure_gb=4.0, resource_critical_gb=2.0, admission_gate=gate
        )
    )


def test_a_disabled_admission_gate_skips_the_hold() -> None:
    """``agent.admission_gate: false`` makes CRITICAL advisory-only for the
    start gate; the heavy-command hold is the same enforcement and honours it."""

    def probe_fn() -> rs.ResourceStatus:
        raise AssertionError("a disabled gate must not read the host")

    clock = _Clock()
    held = _run(
        rs.hold_heavy_command(
            "pytest", probe_fn=probe_fn, sleep=clock.sleep, clock=clock, cfg=_cfg(gate=False)
        )
    )
    assert held == 0.0 and clock.now == 0.0


def test_an_enabled_admission_gate_still_holds() -> None:
    clock = _Clock()
    postures = iter(["critical", "ample"])
    held = _run(
        rs.hold_heavy_command(
            "pytest",
            poll_secs=10.0,
            probe_fn=lambda: _status(next(postures)),
            sleep=clock.sleep,
            clock=clock,
            cfg=_cfg(gate=True),
        )
    )
    assert held == pytest.approx(10.0)


def test_the_gate_switch_is_read_from_the_loaded_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no *cfg* passed, the hold consults the same loaded config the start
    gate does, so one ``admission_gate: false`` switches both off."""
    monkeypatch.setattr(rs, "_load_config", lambda: _cfg(gate=False))

    def probe_fn() -> rs.ResourceStatus:
        raise AssertionError("a disabled gate must not read the host")

    assert _run(rs.hold_heavy_command("pytest", probe_fn=probe_fn)) == 0.0
