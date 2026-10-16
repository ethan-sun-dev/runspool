"""The macOS keychain as a credentials source. No test touches a real keychain: the
``security`` program is replaced by an injected runner or a stand-in script."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from runspool.app import load_context
from runspool.cli import app
from runspool.core.credentials import LocalCredentials
from runspool.core.keychain import KeychainError, MacKeychain, run_security
from runspool.kernel.errors import StartupError
from runspool.runtime import run_until_idle

SECRET = "kc-S3CRET-value-91"


class FakeSecurity:
    """Stands in for ``/usr/bin/security``: ``items`` maps an account to its value."""

    def __init__(self, items=None, status=None, raises=None, out=None):
        self.items = dict(items or {})
        self.status = status
        self.raises = raises
        self.out = out
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if self.raises is not None:
            raise self.raises
        if self.status is not None:
            return self.status, self.out if self.out is not None else SECRET.encode()
        account = argv[argv.index("-a") + 1]
        if account not in self.items:
            return 44, b""
        return 0, self.items[account].encode() + b"\n"


@pytest.fixture
def security(tmp_path):
    path = tmp_path / "security"
    path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def keychain(security, fake, service="test-svc"):
    return MacKeychain(service, runner=fake, security=security, platform="darwin")


def creds(tmp_path, kc=None, env=None, environment=True):
    return LocalCredentials(
        env=env or {},
        user_file=tmp_path / "none.yaml",
        profile_env=None,
        home_env=None,
        environment=environment,
        keychain=kc,
    )


def test_a_value_is_read_from_the_keychain_with_only_names_as_arguments(tmp_path, security):
    fake = FakeSecurity({"API_TOKEN": SECRET})
    c = creds(tmp_path, keychain(security, fake))
    assert c.resolve("API_TOKEN") == SECRET
    info = c.describe("API_TOKEN")
    assert (info.configured, info.source) == (True, "keychain")
    argv, timeout = fake.calls[0]
    assert argv == [
        str(security),
        "find-generic-password",
        "-s",
        "test-svc",
        "-a",
        "API_TOKEN",
        "-w",
    ]
    assert timeout == 15.0
    assert all(SECRET not in a for a, _ in fake.calls)
    assert SECRET not in repr(info)


def test_the_keychain_comes_before_the_environment(tmp_path, security):
    c = creds(tmp_path, keychain(security, FakeSecurity({"T": SECRET})), env={"T": "from-env"})
    assert c.resolve("T") == SECRET


def test_a_missing_item_is_absent_and_the_next_source_is_asked(tmp_path, security):
    (tmp_path / "creds.yaml").write_text("FILE_ONLY: f\n", encoding="utf-8")
    fake = FakeSecurity()
    c = LocalCredentials(
        env={"ENV_ONLY": "e"},
        user_file=tmp_path / "creds.yaml",
        profile_env=None,
        home_env=None,
        keychain=keychain(security, fake),
    )
    assert c.describe("ENV_ONLY").source == "env"
    assert c.describe("FILE_ONLY").source == "credentials file"
    assert c.resolve("NOWHERE") is None
    check = c.check(["NOWHERE"])
    assert not check.ok and "not configured: NOWHERE" in check.detail


def test_an_empty_keychain_value_counts_as_absent(tmp_path, security):
    c = creds(tmp_path, keychain(security, FakeSecurity({"T": ""})), env={"T": "from-env"})
    assert c.describe("T").source == "env"


def test_the_environment_is_not_read_when_configured_off(tmp_path, security):
    env = {"API_TOKEN": "from-env"}
    assert creds(tmp_path, env=env).resolve("API_TOKEN") == "from-env"  # the default
    assert creds(tmp_path, env=env, environment=False).resolve("API_TOKEN") is None
    kc = keychain(security, FakeSecurity())
    off = creds(tmp_path, kc, env=env, environment=False)
    assert off.resolve("API_TOKEN") is None
    assert not off.describe("API_TOKEN").configured
    on = creds(
        tmp_path,
        keychain(security, FakeSecurity({"API_TOKEN": SECRET})),
        env=env,
        environment=False,
    )
    assert on.resolve("API_TOKEN") == SECRET


@pytest.mark.parametrize("status", [1, 36, 51, 128])
def test_a_security_error_is_raised_never_a_fallback_and_never_its_output(
    tmp_path, security, status
):
    fake = FakeSecurity(status=status, out=f"{SECRET}\n".encode())
    c = creds(tmp_path, keychain(security, fake), env={"API_TOKEN": "from-env"})
    with pytest.raises(KeychainError) as caught:
        c.resolve("API_TOKEN")
    message = str(caught.value)
    assert message == f"keychain lookup of API_TOKEN failed (security exit status {status})"
    with pytest.raises(KeychainError):
        c.describe("API_TOKEN")
    check = c.check(["API_TOKEN"], "service credentials")
    assert not check.ok and check.detail == message


@pytest.mark.parametrize(
    "raised, expected",
    [
        (
            subprocess.TimeoutExpired(["security"], 15, output=SECRET.encode()),
            "keychain lookup of API_TOKEN timed out after 15s",
        ),
        (PermissionError(13, f"denied {SECRET}"), "could not run"),
    ],
)
def test_a_timeout_or_an_unrunnable_security_is_an_error_without_context(
    tmp_path, security, raised, expected
):
    c = creds(tmp_path, keychain(security, FakeSecurity(raises=raised)))
    with pytest.raises(KeychainError) as caught:
        c.resolve("API_TOKEN")
    assert expected in str(caught.value) and SECRET not in str(caught.value)
    # Nothing chained: a traceback shows no partial output and no OS message.
    assert caught.value.__context__ is None and caught.value.__cause__ is None


def test_output_that_is_not_utf8_is_an_error(tmp_path, security):
    c = creds(tmp_path, keychain(security, FakeSecurity(status=0, out=b"\xff\xfe" + b"x" * 8)))
    with pytest.raises(KeychainError, match="not UTF-8 text") as caught:
        c.resolve("API_TOKEN")
    assert caught.value.__context__ is None


def test_off_macos_the_keychain_is_an_error_and_security_is_never_run(tmp_path, security):
    fake = FakeSecurity({"API_TOKEN": SECRET})
    kc = MacKeychain("svc", runner=fake, security=security, platform="linux")
    c = creds(tmp_path, kc, env={"API_TOKEN": "from-env"})
    with pytest.raises(KeychainError, match=r"not available on this platform \(linux\)"):
        c.resolve("API_TOKEN")
    assert fake.calls == []
    assert kc.problem() is not None


def test_without_security_the_keychain_is_an_error(tmp_path):
    fake = FakeSecurity({"API_TOKEN": SECRET})
    kc = MacKeychain("svc", runner=fake, security=tmp_path / "missing", platform="darwin")
    with pytest.raises(KeychainError, match="is not available"):
        creds(tmp_path, kc, env={"API_TOKEN": "from-env"}).resolve("API_TOKEN")
    assert fake.calls == []


@pytest.mark.parametrize("name", ["-w sk-live-123", "SK_LIVE_123\n", "sk-live-123"])
def test_a_name_that_is_not_a_credential_name_is_refused_without_echo(security, name):
    fake = FakeSecurity()
    with pytest.raises(KeychainError) as caught:
        keychain(security, fake).lookup(name)
    assert "123" not in str(caught.value) and fake.calls == []


# -- the real runner, against a stand-in for security --------------------------------


@pytest.fixture
def stand_in(tmp_path):
    """A ``security`` that records its arguments, standard input and environment,
    then prints the value stored for the account (exit 44 if there is none)."""
    record = tmp_path / "record.json"
    script = tmp_path / "fake-security"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "stdin = sys.stdin.read()\n"
        "seen = {'argv': argv, 'stdin': stdin, 'env': dict(os.environ)}\n"
        f"json.dump(seen, open({str(record)!r}, 'w'))\n"
        "sys.stderr.write('security: some diagnostic\\n')\n"
        f"items = {{'API_TOKEN': {SECRET!r}, '中文_NONE': ''}}\n"
        "account = argv[argv.index('-a') + 1]\n"
        "if account not in items:\n"
        "    sys.exit(44)\n"
        "print(items[account])\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, record


def test_the_default_runner_reads_stdout_and_passes_no_value_or_input(stand_in, capfd):
    script, record = stand_in
    kc = MacKeychain("svc", security=script, platform="darwin")
    assert kc.lookup("API_TOKEN") == SECRET
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert (seen["argv"], seen["stdin"]) == (
        ["find-generic-password", "-s", "svc", "-a", "API_TOKEN", "-w"],
        "",  # /dev/null
    )
    assert kc.lookup("OTHER") is None  # exit 44
    out, err = capfd.readouterr()
    assert SECRET not in out + err and "diagnostic" not in err  # stderr is discarded


# Fictitious values, in the process environment of the program that looks them up.
ENV_CREDENTIALS = {
    "API_TOKEN": "fake-env-api-token-0001",
    "GITHUB_TOKEN": "fake-env-gh-token-0002",
    "DEPLOY_KEY": "fake-env-deploy-key-0003",
    "AWS_SECRET_ACCESS_KEY": "fake-env-aws-secret-0004",
    "DB_SECRET": "fake-env-db-secret-0005",
    "SERVICE_PASSWORD": "fake-env-password-0006",
}


def test_security_gets_a_fixed_minimal_environment_never_the_parent_one(
    stand_in, tmp_path, monkeypatch
):
    script, record = stand_in
    for name, value in ENV_CREDENTIALS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    c = creds(
        tmp_path,
        MacKeychain("svc", security=script, platform="darwin"),
        env=os.environ,
        environment=False,
    )
    assert c.resolve("API_TOKEN") == SECRET  # the keychain's value, not the variable
    env = json.loads(record.read_text(encoding="utf-8"))["env"]
    # Only what the system adds by itself may appear besides the fixed PATH.
    assert env.get("PATH") == "/usr/bin:/bin:/usr/sbin:/sbin"
    assert set(env) <= {"PATH", "__CF_USER_TEXT_ENCODING", "LC_CTYPE"}
    shown = json.dumps(env)
    assert all(name not in env and value not in shown for name, value in ENV_CREDENTIALS.items())


def test_the_default_runner_reports_a_missing_program():
    with pytest.raises(OSError):
        run_security(["/nonexistent/security-x"], 1)


# -- through a profile and the command line ------------------------------------------

STEPS = """
from runspool.engine.step import Step, StepResult


class UsesToken(Step):
    name = "uses_token"

    def __init__(self, credentials):
        self.credentials = credentials

    def run(self, ctx):
        value = self.credentials.resolve("API_TOKEN")
        return StepResult(message="token configured" if value else "no token")


def register(ctx, config):
    ctx.steps.register(UsesToken(ctx.credentials))
    ctx.workflows.add_default("needs_token", ["uses_token"])

register.inject = ["steps", "workflows", "credentials"]
"""

cli = CliRunner()


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """A profile whose credentials come from the keychain, with ``security`` faked."""
    (tmp_path / "token_steps.py").write_text(STEPS, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "token_steps", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("API_TOKEN", "from-the-environment")
    monkeypatch.setattr(MacKeychain, "problem", lambda self: None)
    fake = FakeSecurity({"API_TOKEN": SECRET})
    monkeypatch.setattr("runspool.core.keychain.run_security", fake)

    def build(credentials_config):
        path = tmp_path / "工作 配置" / "config.yaml"
        path.parent.mkdir(exist_ok=True)
        patch = [{"insert": [{"id": "tokens", "plugin": "token_steps:register"}]}]
        if credentials_config is not None:
            patch.append({"id": "credentials", "config": credentials_config})
        path.write_text(
            f"workspace_root: {json.dumps(str(tmp_path / 'ws'))}\n"
            f"patch: {json.dumps(patch, ensure_ascii=False)}\n",
            encoding="utf-8",
        )
        return path, fake

    return build


KEYCHAIN_ONLY = {"keychain": {"service": "test-svc"}, "environment": False}


def test_a_profile_reads_the_keychain_and_never_shows_the_value(profile, caplog):
    path, fake = profile(KEYCHAIN_ONLY)
    caplog.set_level(logging.DEBUG)
    ctx = load_context(path)
    creds = ctx.service("credentials")
    assert creds.resolve("API_TOKEN") == SECRET
    assert creds.describe("API_TOKEN").source == "keychain"
    assert fake.calls[0][0][1:] == [
        "find-generic-password",
        "-s",
        "test-svc",
        "-a",
        "API_TOKEN",
        "-w",
    ]

    outputs = []
    tid = ctx.service("tasks").add("in.txt", workflow="needs_token")
    run_until_idle(ctx, notifier=outputs.append)
    assert ctx.repo.get_task(tid)["task_status"] == "completed"
    for args in (
        ["doctor"],
        ["doctor", "--json"],
        ["status"],
        ["status", "--json"],
        ["inspect", str(tid)],
        ["inspect", str(tid), "--json"],
        ["logs", str(tid), "--json"],
        ["plugins", "--json"],
    ):
        result = cli.invoke(app, ["-c", str(path), *args])
        assert result.exception is None or isinstance(result.exception, SystemExit), args
        outputs.append(result.output)
    doctor = json.loads(cli.invoke(app, ["-c", str(path), "doctor", "--json"]).output)
    keychain_check = next(c for c in doctor if c["name"] == "credentials keychain")
    assert keychain_check["ok"] and "test-svc" in keychain_check["detail"]
    events = json.dumps(ctx.log.list_for_task(tid), ensure_ascii=False, default=str)
    outputs += [events, caplog.text, "\n".join(map(str, fake.calls))]
    assert all(SECRET not in text for text in outputs)


def test_a_keychain_that_fails_fails_the_step_without_the_value(profile):
    path, fake = profile(KEYCHAIN_ONLY)
    fake.status, fake.out = 51, f"{SECRET}\n".encode()
    ctx = load_context(path)
    tid = ctx.service("tasks").add("in.txt", workflow="needs_token")
    run_until_idle(ctx, notifier=lambda m: None)
    task = ctx.repo.get_task(tid)
    assert task["last_error"] == (
        "KeychainError: keychain lookup of API_TOKEN failed (security exit status 51)"
    )
    shown = [
        cli.invoke(app, ["-c", str(path), c, str(tid), "--json"]).output
        for c in ("inspect", "logs")
    ]
    assert all(SECRET not in text for text in shown)
    assert "from-the-environment" not in json.dumps(ctx.log.list_for_task(tid), default=str)


def test_off_macos_a_keychain_profile_reports_it_in_doctor(profile, monkeypatch):
    path, fake = profile(KEYCHAIN_ONLY)
    monkeypatch.setattr(MacKeychain, "problem", lambda self: "the macOS keychain is not available")
    result = cli.invoke(app, ["-c", str(path), "doctor", "--json"])
    assert result.exit_code == 1
    checks = {c["name"]: c for c in json.loads(result.output)}
    assert not checks["credentials keychain"]["ok"]
    assert fake.calls == []


@pytest.mark.parametrize(
    "setting, field",
    [
        ({"service": ""}, "keychain.service"),
        ({"service": "svc-private\u0000x"}, "keychain.service"),
        ({"timeout_seconds": 0}, "keychain.timeout_seconds"),
    ],
)
def test_a_bad_keychain_setting_fails_credentials_naming_the_field(profile, caplog, setting, field):
    path, fake = profile({"keychain": setting, "environment": False})
    with pytest.raises(StartupError, match="no active plugin provides credentials"):
        load_context(path)
    assert f"invalid config: {field}" in caplog.text and "svc-private" not in caplog.text
    assert fake.calls == []


def test_by_default_nothing_changes_and_security_is_never_run(profile, monkeypatch):
    path, fake = profile(None)
    calls = []
    real_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or real_run(*a, **k))
    ctx = load_context(path)
    creds = ctx.service("credentials")
    assert creds.keychain is None and creds.environment is True
    assert creds.resolve("API_TOKEN") == "from-the-environment"
    assert creds.describe("API_TOKEN").source == "env"
    names = [c.name for c in ctx.service("doctor").run()]
    assert "credentials keychain" not in names and "credentials file" in names
    assert fake.calls == [] and not any("security" in str(a) for a in calls)


# -- a failing lookup through the real command line, with tracebacks showing locals ---

CLI_PLUGIN = """
import typer


def contribute(ctx, config):
    def show_token():
        value = ctx.credentials.resolve("API_TOKEN")
        typer.echo("configured" if value else "not configured")

    ctx.cli.register(show_token, name="show-token")


contribute.inject = ["cli", "credentials"]
"""

# Starts the CLI with the keychain's ``security`` replaced by a stand-in. Older
# Typer versions (allowed by the dependency range) show local variables in their
# tracebacks by default; ``force-locals`` turns that on whatever the version.
CLI_LAUNCHER = """
import sys
from pathlib import Path

if sys.argv.pop(1) == "force-locals":
    import typer

    _init = typer.Typer.__init__

    def _with_locals(self, *args, **kwargs):
        kwargs["pretty_exceptions_show_locals"] = True
        _init(self, *args, **kwargs)

    typer.Typer.__init__ = _with_locals

import runspool.core.keychain as keychain

keychain.MacKeychain.__init__.__kwdefaults__["security"] = Path(sys.argv.pop(1))
keychain.MacKeychain.problem = lambda self: None

from runspool.cli import main

sys.argv[0] = "runspool"
main()
"""


@pytest.mark.parametrize(
    "output, status",
    [
        (f"sys.stdout.buffer.write({SECRET.encode()!r} + b'\\xff\\n')", 0),  # not UTF-8
        (f"print({SECRET!r})", 51),  # an error status, with output
    ],
    ids=["not-utf8", "error-status"],
)
@pytest.mark.parametrize("locals_", ["force-locals", "version-default"])
def test_a_failing_lookup_never_shows_the_output_in_a_traceback(tmp_path, output, status, locals_):
    script = tmp_path / "fake-security"
    script.write_text(
        f"#!{sys.executable}\nimport sys\n{output}\nsys.exit({status})\n", encoding="utf-8"
    )
    script.chmod(0o755)
    (tmp_path / "token_cli.py").write_text(CLI_PLUGIN, encoding="utf-8")
    profile = tmp_path / "config.yaml"
    patch = [
        {"insert": [{"id": "token-cli", "plugin": "token_cli:contribute"}]},
        {"id": "credentials", "config": {"keychain": {"service": "svc"}, "environment": False}},
    ]
    profile.write_text(
        f"workspace_root: {json.dumps(str(tmp_path / 'ws'))}\npatch: {json.dumps(patch)}\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(tmp_path), *sys.path]),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "COLUMNS": "200",
    }
    env.pop("_TYPER_STANDARD_TRACEBACK", None)
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            CLI_LAUNCHER,
            locals_,
            str(script),
            "-c",
            str(profile),
            "show-token",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )
    assert done.returncode != 0
    assert "KeychainError" in done.stderr  # the failing path did run
    assert SECRET not in done.stdout + done.stderr
    assert "S3CRET" not in done.stdout + done.stderr  # nor a truncated part of it
