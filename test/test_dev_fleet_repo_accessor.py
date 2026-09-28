"""``MAIN_REPO`` reaches git and the filesystem only through ``_repo()``.

Dev Fleet represents "no main checkout found" as an empty string in
``MAIN_REPO``. That sentinel is fail-open at any call site that consumes the
global directly: ``git -C ""`` does not fail — it silently runs against the
backend process's working directory — and ``Path("")`` is ``Path(".")``, so an
unguarded consumer operates on an arbitrary directory and returns plausible
results. The ``_repo()`` accessor centralizes the guard: it returns the path or
raises ``RepoNotConfigured``, which the HMAC middleware converts to the 409
``repo_not_configured`` boundary.

Two enforcement tiers (same pattern as ``test_apps_instances_loop_offload.py``):

- Behavior tests: ``_repo()`` raises on the empty sentinel and returns the
  path otherwise, preserving the exception type the middleware boundary maps.
- AST ratchet: outside the accessor itself, a ``MAIN_REPO`` load may appear
  ONLY as a bare truthiness guard (``if MAIN_REPO:`` / ``not MAIN_REPO`` / a
  ``BoolOp`` operand). Any other load — a git argv element, a subprocess
  ``cwd=``, a ``Path(...)`` build, an f-string interpolation, a payload
  field — fails this test, so a future call site cannot silently reintroduce
  the fail-open shape.
"""

from __future__ import annotations

import ast
import inspect
import locale
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    http_api,
    live,
    repository,
    runtime,
    server,
    worktree_ops,
)

# The accessor is the ONLY function whose body may read the bare global: it IS
# the guard. The startup hook's discovery/re-resolve runs on a local and writes
# the global exactly once (a Store, which this ratchet ignores), so even the
# assignment site needs no exemption — and a git call added to startup, where
# MAIN_REPO is most often still unresolved, is caught like anywhere else.
_DEV_FLEET_MODULES = (
    runtime,
    repository,
    live,
    fleet_state,
    worktree_ops,
    http_api,
    server,
)
_ALLOWED_LOADS = {(repository.__name__, "_repo")}


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    cur: ast.AST | None = node
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = parents.get(cur)
    return None


def _is_bare_truthiness(node: ast.expr, parents: dict[ast.AST, ast.AST]) -> bool:
    """True when the load feeds a truthiness test and nothing else.

    Walking up from the Name, only ``BoolOp`` and ``not`` may intervene before
    the expression lands as the ``test`` of an ``if``/``while`` or a ternary.
    Any other intervening node (a call argument, a container literal, an
    f-string, an assignment value) means the VALUE escapes, which is exactly
    the shape the accessor exists to prevent.
    """
    child: ast.AST = node
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.BoolOp, ast.UnaryOp)):
            if isinstance(cur, ast.UnaryOp) and not isinstance(cur.op, ast.Not):
                return False
            child = cur
            cur = parents.get(cur)
            continue
        if isinstance(cur, (ast.If, ast.While)):
            return cur.test is child
        if isinstance(cur, ast.IfExp):
            return cur.test is child
        return False
    return False


def test_main_repo_loads_only_via_accessor_or_truthiness() -> None:
    violations: list[str] = []
    for module in _DEV_FLEET_MODULES:
        tree = ast.parse(inspect.getsource(module))
        parents = _parent_map(tree)
        for node in ast.walk(tree):
            is_main_repo = (isinstance(node, ast.Name) and node.id == "MAIN_REPO") or (
                isinstance(node, ast.Attribute) and node.attr == "MAIN_REPO"
            )
            if not is_main_repo or not isinstance(node.ctx, ast.Load):
                continue  # assignments (Store) stay on the global by design
            func = _enclosing_function(node, parents)
            if (module.__name__, func) in _ALLOWED_LOADS:
                continue
            if _is_bare_truthiness(node, parents):
                continue
            violations.append(
                f"{module.__name__}:{node.lineno}: MAIN_REPO load in "
                f"{func or '<module>'} — route it through repository._repo()"
            )
    assert not violations, (
        "MAIN_REPO's empty-string sentinel is fail-open when consumed "
        "directly (git -C '' runs against the process CWD). Use _repo():\n"
        + "\n".join(violations)
    )


def test_repo_accessor_raises_on_unresolved_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "MAIN_REPO", "")
    with pytest.raises(repository.RepoNotConfigured):
        repository._repo()


def test_repo_accessor_returns_resolved_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/kirocrew")
    assert repository._repo() == "/somewhere/kirocrew"


def test_primary_checkout_resolution_preserves_the_host_text_decoder(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Moving the startup probe must not reinterpret non-ASCII checkout paths."""
    primary = tmp_path / "primary"
    seen: dict[str, object] = {}

    def _run(_argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=str(primary / ".git"))

    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "git")
    monkeypatch.setattr(repository.subprocess, "run", _run)

    assert repository._resolve_primary_checkout(str(tmp_path / "linked")) == str(primary)
    assert seen["text"] is True
    assert seen["encoding"] == locale.getpreferredencoding(False)


# ---------------------------------------------------------------------------
# The base branch is the resolved checkout's own, not a hardcoded "main".
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name, ok",
    [
        ("main", True),
        ("trunk", True),
        ("release/2.0", True),
        ("feature.x", True),
        # A leading dash is parsed as a FLAG by git once the name is
        # interpolated into an argv, so it must never be accepted.
        ("--exec=touch /nowhere/pwn", False),
        ("-main", False),
        # ``..`` splits a rev range at the wrong place: ``origin/a..b..HEAD``.
        ("a..b", False),
        ("", False),
        ("main branch", False),
        ("main;rm", False),
    ],
)
def test_base_branch_names_are_constrained_before_reaching_an_argv(name: str, ok: bool) -> None:
    assert repository._plausible_branch_name(name) is ok


def test_every_local_base_candidate_survives_the_argv_constraint() -> None:
    """The fallback list and the argv guard must agree.

    A candidate the guard rejects would be published into ``BASE_BRANCH`` by the
    fallback loop without ever meeting ``_plausible_branch_name``, which only screens
    the remote's answer. Asserting over the tuple itself keeps a name added later
    from slipping past.
    """
    assert repository._LOCAL_BASE_CANDIDATES
    for candidate in repository._LOCAL_BASE_CANDIDATES:
        assert repository._plausible_branch_name(candidate) is True


def _stub_base_branch_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    remotes: str,
    published: dict[str, str],
    local: set[str],
    head: str | None = None,
) -> list[str]:
    """Wire the git reads ``_resolve_base_branch`` makes. Returns the ref probes."""
    probed: list[str] = []

    async def _git(_repo: str, *args: str, **_kw: object) -> str | None:
        if args[0] == "remote":
            return remotes
        if args[0] == "symbolic-ref":
            ref = args[-1]
            probed.append(ref)
            if ref == "HEAD":
                return head
            remote = ref.split("/")[2]
            published_head = published.get(remote)
            return f"{remote}/{published_head}" if published_head else None
        if args[0] == "rev-parse":
            name = args[-1].removeprefix("refs/heads/")
            return name if name in local else None
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/other-project")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    # Patched here so EVERY caller gets it restored. ``_resolve_base_branch`` assigns
    # this global for real, so a test that only patches ``BASE_BRANCH`` would leave
    # the verdict behind and decide a later test in another file -- which is how a
    # rebase test passed on leakage instead of on its own setup.
    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", False)
    monkeypatch.setattr(repository, "_git", _git)
    return probed


@pytest.mark.asyncio
async def test_base_branch_ignores_a_remote_sorted_before_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An alphabetically earlier remote must not decide the rebase base.

    ``archive`` publishes one default and ``origin`` another. Only ``origin`` may be
    consulted, because ``_upstream_remote`` resolves to it and the two answers are
    combined into a single rev range.
    """
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="archive\norigin\n",
        published={"archive": "legacy-default", "origin": "trunk"},
        local=set(),
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "trunk"
    assert probed == ["refs/remotes/origin/HEAD"]


@pytest.mark.asyncio
async def test_base_branch_reads_a_sole_remote_under_another_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One remote is unambiguous whatever it is called, so its answer is taken."""
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="kirocrew\n",
        published={"kirocrew": "release/3"},
        local=set(),
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "release/3"
    assert probed == ["refs/remotes/kirocrew/HEAD"]


@pytest.mark.asyncio
async def test_base_branch_falls_back_locally_when_no_remote_is_unambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Several remotes and no ``origin`` is ambiguous: ask the local branches."""
    local_default = repository._LOCAL_BASE_CANDIDATES[-1]
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="fork\nupstream\n",
        published={"fork": "a", "upstream": "b"},
        local={local_default},
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == local_default
    assert probed == []


@pytest.mark.asyncio
async def test_a_present_candidate_outranks_the_checked_out_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAD is the LAST tier, because a dev checkout sits on a feature branch.

    ``main`` exists here, so it is the base even though the checkout is parked on
    someone's branch -- taking HEAD would retarget every rebase and every
    ahead/behind reading at that branch for as long as it stays checked out.
    """
    candidate = repository._LOCAL_BASE_CANDIDATES[0]
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="origin\n",
        published={},
        local={candidate, "feature/some-work"},
        head="feature/some-work",
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == candidate
    assert "HEAD" not in probed


@pytest.mark.asyncio
async def test_an_implausible_head_is_refused_like_every_other_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last tier is validated too, or it becomes the way a bad name gets in."""
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="origin\n",
        published={},
        local=set(),
        head="--upload-pack=touch /nowhere/pwned",
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "main"
    assert probed == ["refs/remotes/origin/HEAD", "HEAD"]


@pytest.mark.asyncio
async def test_an_unresolved_checkout_asks_git_nothing_and_keeps_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No checkout means no spawn: ``git`` here would answer for the backend's own cwd."""

    async def _never(*_a, **_kw):
        raise AssertionError("no git may run while the checkout is unresolved")

    monkeypatch.setattr(repository, "MAIN_REPO", "")
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    monkeypatch.setattr(repository, "_git", _never)
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "main"


@pytest.mark.asyncio
async def test_a_stated_base_branch_is_told_apart_from_a_guessed_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two answering tiers are positive; the last-resort tier is not.

    The final tier publishes whatever branch is checked out, and its trigger is
    ordinary -- a dev box's checkout sits on a feature branch. That is a fine label
    for a row and a wrong base for a rebase, so the difference is recorded rather
    than left for each consumer to re-derive.
    """
    # A remote that publishes HEAD: the repository's own statement.
    _stub_base_branch_git(
        monkeypatch, remotes="origin\n", published={"origin": "trunk"}, local=set()
    )
    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", False)
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "trunk"
    assert repository._BASE_BRANCH_POSITIVE is True
    assert repository.base_branch_mutation_refusal() is None

    # No remote HEAD, but a conventional default exists here. It becomes the LABEL and
    # stays a guess: a name existing locally is not the repository stating anything, and
    # a `main` left behind by a rename to `trunk` is the ordinary residue of that
    # rename. Named from the module's own tuple rather than spelled out, so this
    # follows the candidate list if it ever changes.
    legacy = repository._LOCAL_BASE_CANDIDATES[1]
    _stub_base_branch_git(monkeypatch, remotes="origin\n", published={}, local={legacy})
    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", True)
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == legacy
    assert repository._BASE_BRANCH_POSITIVE is False
    assert repository.base_branch_mutation_refusal() is not None

    # Neither: the checked-out branch is published as a LABEL, not as a base.
    _stub_base_branch_git(
        monkeypatch, remotes="origin\n", published={}, local=set(), head="feature/x"
    )
    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", True)
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "feature/x"
    assert repository._BASE_BRANCH_POSITIVE is False
    refusal = repository.base_branch_mutation_refusal()
    assert refusal and "feature/x" in refusal


@pytest.mark.asyncio
async def test_a_rebase_refuses_a_guessed_base_before_it_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is fetched and nothing is rewritten while the base is a guess.

    A clean replay onto the wrong base returns ``ok`` and names no rollback, so this
    is the one operation here that cannot be undone from its own result -- the gate
    therefore sits before the fetch rather than after it.
    """
    calls: list[tuple[str, ...]] = []

    async def fake_git(_path, *args, **_kw):
        calls.append(tuple(args))
        return "" if args and args[0] == "status" else "ok"

    monkeypatch.setattr(repository, "_git", fake_git)

    async def _never(*_a, **_kw):
        raise AssertionError("no rebase may spawn while the base branch is a guess")

    monkeypatch.setattr(runtime, "_run_cmd", _never)

    # The rebase re-resolves before reading the gate, so the verdict under test has to
    # come from that resolution rather than from a value planted here.
    async def _resolve_to_a_guess() -> None:
        repository.BASE_BRANCH = "feature/x"
        repository._BASE_BRANCH_POSITIVE = False

    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", True)
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    monkeypatch.setattr(repository, "_resolve_base_branch", _resolve_to_a_guess)

    res = await worktree_ops._rebase_locked({"path": "/r"})

    assert res["ok"] is False
    assert "refusing to rebase" in res["error"]
    assert "feature/x" in res["error"], "the refusal names the base it refused"
    # The dirt gate above it still ran; the fetch below it did not.
    assert ("status", "--porcelain") in calls
    assert not [c for c in calls if c and c[0] == "fetch"]


@pytest.mark.asyncio
async def test_a_rebase_re_resolves_so_the_refusal_clears_without_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal tells the operator to record the remote's default. That must work.

    Discovery latches once per process, so a base resolved at startup would be the only
    answer this process ever holds -- and the remedy the refusal names would need a
    gateway restart to take effect. The rebase therefore re-resolves immediately before
    reading the gate.
    """
    resolved: list[str] = []

    async def fake_git(_path, *args, **_kw):
        return "" if args and args[0] == "status" else "ok"

    async def _resolve() -> None:
        # The operator has since run `git remote set-head`, so this attempt resolves a
        # STATED base where the previous one found only a guess.
        resolved.append("resolved")
        repository.BASE_BRANCH = "trunk"
        repository._BASE_BRANCH_POSITIVE = True

    monkeypatch.setattr(repository, "_git", fake_git)
    monkeypatch.setattr(repository, "_resolve_base_branch", _resolve)
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    monkeypatch.setattr(
        repository, "_git_info", AsyncMock(return_value={"head": "abc1234", "behind": 0})
    )
    # The stale state a latched discovery would have left behind.
    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", False)
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")

    res = await worktree_ops._rebase_locked({"path": "/r"})

    assert resolved == ["resolved"], "the rebase must re-resolve, not inherit"
    assert res["ok"] is True, f"a stated base must not be refused: {res}"


# ---------------------------------------------------------------------------
# A read leaves the repository byte-identical.
# ---------------------------------------------------------------------------


def test_optional_locks_are_off_for_every_git_this_handler_runs() -> None:
    """``git status`` rewrites the index unless optional locks are off.

    It is a read to its caller and a write to the repository: it refreshes the
    index's stat cache and saves it back under ``index.lock``. Every fleet render
    runs one per row, so without this the fleet contends with the operator's own git
    for the lock on the ordinary path. Pinned on the env chokepoint rather than per
    call site, which is what makes a read added later inherit it.
    """
    assert runtime._GIT_ENV_NEUTRALIZERS["GIT_OPTIONAL_LOCKS"] == "0"


def test_signature_verification_cannot_exec_a_repo_named_program() -> None:
    """Verification EXECS the program these keys name, and a READ can trigger it.

    ``[log] showSignature=true`` in an agent-writable ``.git/config`` makes every
    ``git log`` verify, and verification runs the named program. All four spellings
    are pinned because ``gpg.openpgp.program`` is a synonym that OVERRIDES the bare
    ``gpg.program``, so pinning one key leaves the other as an unpinned way in.
    """
    n = runtime._GIT_ENV_NEUTRALIZERS
    pinned = {n[f"GIT_CONFIG_KEY_{i}"]: n[f"GIT_CONFIG_VALUE_{i}"] for i in range(9)}
    for key in ("gpg.program", "gpg.openpgp.program", "gpg.ssh.program", "gpg.x509.program"):
        assert pinned[key] == "true", f"{key} must not be left to the repository"
    assert pinned["log.showSignature"] == "false", "the trigger must be pinned too"


@pytest.mark.asyncio
async def test_run_cmd_puts_the_neutralizers_in_the_child_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dict is only a guarantee if the spawn actually carries it.

    Asserted through the spawn preparation, because that is the last place the env
    can be read before the child exists, and an entry dropped anywhere earlier would
    leave the dict stating a pin nothing applies.
    """
    seen: dict[str, str] = {}

    def _prepare(cmd, _mode, env=None, **_kw):
        seen.update(env or {})
        return list(cmd), dict(env or {}), None

    monkeypatch.setattr(runtime, "sandboxed_spawn_argv", _prepare)
    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "/usr/bin/git")
    # Trusted helpers are APPENDED after the pins and legitimately raise
    # GIT_CONFIG_COUNT past the pinned value, so this states an empty helper set
    # rather than inherit whatever an earlier test left in the module global.
    # Without it the assertion below reads the helper path's count and fails on test
    # ORDER, not on a dropped pin.
    monkeypatch.setattr(runtime, "_GIT_TRUSTED_HELPERS", {})

    async def _off_loop(fn, executor=None):
        return fn()

    monkeypatch.setattr(runtime, "shielded_prepare_off_loop", _off_loop)

    async def _no_child(*_a, **_kw):
        raise AssertionError("the env is read before the child spawns")

    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", _no_child)
    with pytest.raises(AssertionError):
        await runtime._run_cmd(["git", "-C", "/somewhere/other-project", "status"])

    for key, val in runtime._GIT_ENV_NEUTRALIZERS.items():
        assert seen.get(key) == val, f"{key} did not reach the child env"


# ---------------------------------------------------------------------------
# A remote URL's query is a credential, and nothing derives from it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, locator",
    [
        ("https://h/o/r.git", "https://h/o/r.git"),
        ("https://h/o/r.git?access_token=SECRET", "https://h/o/r.git"),
        ("https://h/o/r.git#SECRET", "https://h/o/r.git"),
        ("git@h:o/r.git", "git@h:o/r.git"),
        ("  https://h/o/r.git?t=1  ", "https://h/o/r.git"),
        ("", ""),
    ],
)
def test_remote_url_locator_keeps_only_the_locator(url: str, locator: str) -> None:
    assert runtime.remote_url_locator(url) == locator


def test_no_derivation_carries_a_remote_url_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every derivation cuts the query, because each anchors on ``$``.

    A retained ``?access_token=...`` sits between a trailing ``.git`` and the end of
    the string, so the suffix a pattern means to strip survives AND the token rides
    into the result: one becomes an issue-link ``href``, the others an ``owner/repo``
    handed to ``gh --repo`` in child argv.
    """
    secret = "access_token=SECRET"
    url = f"https://h/o/r.git?{secret}"

    base = fleet_state._parse_html_repo_base(url)
    assert base == "https://h/o/r", "the href must not carry the token"
    assert secret not in (base or "")

    identity = repository._normalize_repo_identity(url)
    assert identity == ("h", "o/r"), "the identity must not carry the token"
    assert secret not in "".join(identity or ())

    # And the same repository written WITHOUT a query derives the same values, which
    # is the property a surviving query breaks: two spellings of one repo compared
    # as two, and a cache keyed on the difference.
    assert fleet_state._parse_html_repo_base("https://h/o/r.git") == base
    assert repository._normalize_repo_identity("https://h/o/r.git") == identity


# ---------------------------------------------------------------------------
# An unknown exit status is a failure, not a success.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_exit_status_is_reported_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``returncode`` is None when the child's status has not been reaped yet.

    ``or 0`` mapped that to 0, and 0 is what every caller here reads as "that
    worked": a row would report a clean tree, and a mutation's caller would go on to
    the next step, on the strength of an exit status nobody ever saw.
    """

    class _Proc:
        pid = 4321
        returncode = None

        async def communicate(self):
            return b"out", b""

    def _prepare(cmd, _mode, env=None, **_kw):
        return list(cmd), dict(env or {}), None

    monkeypatch.setattr(runtime, "sandboxed_spawn_argv", _prepare)
    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(runtime, "_GIT_TRUSTED_HELPERS", {})

    async def _off_loop(fn, executor=None):
        return fn()

    monkeypatch.setattr(runtime, "shielded_prepare_off_loop", _off_loop)

    async def _spawn(*_a, **_kw):
        return _Proc()

    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", _spawn)

    rc, stdout, _stderr = await runtime._run_cmd(["git", "-C", "/r", "status"])
    assert rc == -1, "an unreaped child must not be reported as success"
    assert stdout == "out"
