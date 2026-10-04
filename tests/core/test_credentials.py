"""Credentials by name, looked up in a chain of sources."""

from __future__ import annotations

import os

import pytest
from pydantic import BaseModel, ValidationError

from runspool.app import load_context
from runspool.core.credentials import CredentialRef, LocalCredentials, parse_dotenv


@pytest.fixture
def files(tmp_path):
    user = tmp_path / "credentials.yaml"
    profile_env = tmp_path / "profile.env"
    home_env = tmp_path / "home.env"
    return user, profile_env, home_env


def make(files, env=None):
    user, profile_env, home_env = files
    return LocalCredentials(
        env=env or {}, user_file=user, profile_env=profile_env, home_env=home_env
    )


def test_the_chain_prefers_env_then_user_file_then_profile_then_home(files):
    user, profile_env, home_env = files
    home_env.write_text("TOKEN=home\nONLY_HOME=h\n", encoding="utf-8")
    profile_env.write_text("TOKEN=profile\nONLY_PROFILE=p\n", encoding="utf-8")
    user.write_text("TOKEN: user\nONLY_USER: u\n", encoding="utf-8")
    creds = make(files, env={"TOKEN": "env"})
    assert creds.resolve("TOKEN") == "env"
    assert make(files).resolve("TOKEN") == "user"
    assert [make(files).describe(n).source for n in ("ONLY_USER", "ONLY_PROFILE", "ONLY_HOME")] == [
        "credentials file",
        "profile .env",
        "~/.env",
    ]


def test_empty_values_count_as_absent(files):
    user, profile_env, _ = files
    user.write_text("TOKEN: ''\n", encoding="utf-8")
    profile_env.write_text("TOKEN=from-profile\n", encoding="utf-8")
    assert make(files, env={"TOKEN": ""}).resolve("TOKEN") == "from-profile"


def test_describe_never_carries_the_value(files):
    creds = make(files, env={"SECRET": "s3cr3t"})
    info = creds.describe("SECRET")
    assert (info.configured, info.source) == (True, "env")
    assert "s3cr3t" not in repr(info)
    check = creds.check(["SECRET", "MISSING"], "wechat")
    assert not check.ok and "MISSING" in check.detail and "s3cr3t" not in check.detail


def test_each_lookup_rereads_the_sources(files):
    user, _, _ = files
    creds = make(files)
    user.write_text("TOKEN: one\n", encoding="utf-8")
    assert creds.resolve("TOKEN") == "one"
    user.write_text("TOKEN: two\n", encoding="utf-8")
    assert creds.resolve("TOKEN") == "two"


def test_dotenv_parsing():
    text = "# comment\nexport A=1\nB=\"two words\"\nC='3'\nnot a pair\n\nD = 4\n"
    assert parse_dotenv(text) == {"A": "1", "B": "two words", "C": "3", "D": "4"}


def test_a_credentials_file_others_can_read_is_reported(files):
    user, _, _ = files
    user.write_text("TOKEN: x\n", encoding="utf-8")
    os.chmod(user, 0o644)
    assert "chmod 600" in make(files).file_problem()
    os.chmod(user, 0o600)
    assert make(files).file_problem() is None


def test_credential_refs_are_names_not_values():
    class Config(BaseModel):
        appsecret: CredentialRef

    assert Config(appsecret="WECHAT_APPSECRET").appsecret == "WECHAT_APPSECRET"
    with pytest.raises(ValidationError):
        Config(appsecret="sk-live 1234")


def test_boot_provides_credentials_and_reads_the_profile_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("PROFILE_ONLY_TOKEN", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"workspace_root: {tmp_path / 'ws'}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("PROFILE_ONLY_TOKEN=abc\n", encoding="utf-8")
    ctx = load_context(cfg)
    creds = ctx.service("credentials")
    assert creds.resolve("PROFILE_ONLY_TOKEN") == "abc"
    names = {c.name for c in ctx.service("doctor").run()}
    assert "credentials file" in names
