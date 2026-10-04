"""``credentials``: secrets by name. Default implementation: ``credentials-local``.

Configuration never holds a secret, only its *name* (a :data:`CredentialRef`, e.g.
``appsecret: WECHAT_APPSECRET``). A plugin resolves the name when it needs the value
and should not keep it: every lookup re-reads the sources, so rotating a secret
needs no restart.

``credentials-local`` looks a name up in, in order:

1. the process environment;
2. the user's credentials file, ``$XDG_CONFIG_HOME/runspool/credentials.yaml``
   (default ``~/.config/runspool/credentials.yaml``), a ``NAME: value`` mapping that
   only its owner should be able to read;
3. a ``.env`` file next to the profile;
4. ``~/.env``.

An empty value counts as absent. :meth:`LocalCredentials.describe` reports whether
a name is configured and where, without the value, so it is safe to show (doctor
does). Values never go into logs or events.

The credentials service is a seam: a profile may disable this entry and mount
another provider of ``credentials`` (a keychain, a vault) with the same methods.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, StringConstraints

from runspool.core.doctor import Check
from runspool.kernel import Plugin

# Environment-variable style (upper case): a lower-case hex string is far more likely
# a secret pasted by mistake than a name, and must not be echoed back as one.
CREDENTIAL_NAME_PATTERN = r"^[A-Z_][A-Z0-9_]*$"
CredentialRef = Annotated[str, StringConstraints(pattern=CREDENTIAL_NAME_PATTERN)]
"""The name of a secret, as configuration refers to it."""


@dataclass(frozen=True)
class CredentialInfo:
    name: str
    configured: bool
    source: str | None  # "env", "credentials file", "profile .env", "~/.env"


def default_user_file() -> Path:
    # The XDG spec says a relative XDG_CONFIG_HOME is invalid and must be ignored.
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base / "runspool" / "credentials.yaml"


_EXPORT = re.compile(r"^export\s+")


def parse_dotenv(text: str) -> dict[str, str]:
    """``KEY=VALUE`` lines. Allowed: ``export`` prefixes, ``#`` comment lines, a
    quoted value (kept verbatim, ``#`` included), and a `` # comment`` after an
    unquoted value."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = _EXPORT.sub("", line)
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[key.strip()] = value
    return values


class LocalCredentials:
    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        user_file: Path | None = None,
        profile_env: Path | None = None,
        home_env: Path | None = None,
    ) -> None:
        self._env = env if env is not None else os.environ
        self.user_file = user_file if user_file is not None else default_user_file()
        self.profile_env = profile_env
        self.home_env = home_env
        # Why a source could not be read, without its content (YAML errors quote the
        # offending line, which may be a secret).
        self._problems: dict[Path, str] = {}

    def _lookup(self, name: str) -> tuple[str | None, str | None]:
        value = self._env.get(name)
        if value:
            return value, "env"
        for label, read in (
            ("credentials file", self._read_user_file),
            ("profile .env", lambda: self._read_dotenv(self.profile_env)),
            ("~/.env", lambda: self._read_dotenv(self.home_env)),
        ):
            value = read().get(name)
            if value:
                return str(value), label
        return None, None

    def resolve(self, name: str) -> str | None:
        """The secret's value, or ``None`` if no source has a non-empty one."""
        return self._lookup(name)[0]

    def describe(self, name: str) -> CredentialInfo:
        """Whether ``name`` is configured and where; never the value."""
        value, source = self._lookup(name)
        return CredentialInfo(name, value is not None, source)

    def check(self, names: Iterable[str], label: str = "credentials") -> Check:
        """A doctor check that the given names are all configured (no values shown)."""
        infos = [self.describe(name) for name in names]
        missing = [info.name for info in infos if not info.configured]
        if missing:
            return Check(label, False, f"not configured: {', '.join(missing)}")
        return Check(label, True, ", ".join(f"{i.name} ({i.source})" for i in infos))

    def file_problem(self) -> str | None:
        """Why the credentials sources are unsafe or unreadable, if they are."""
        problems = []
        try:
            mode = self.user_file.stat().st_mode
        except OSError:
            mode = 0
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            problems.append(
                f"{self.user_file} is accessible to other users; run: chmod 600 {self.user_file}"
            )
        self._read_user_file()
        self._read_dotenv(self.profile_env)
        self._read_dotenv(self.home_env)
        problems.extend(self._problems.values())
        return "; ".join(problems) or None

    def _read(self, path: Path | None, parse) -> dict[str, Any]:
        if path is None:
            return {}
        self._problems.pop(path, None)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError) as exc:
            self._problems[path] = f"{path} cannot be read ({type(exc).__name__})"
            return {}
        try:
            data = parse(text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f" near line {mark.line + 1}" if mark is not None else ""
            self._problems[path] = f"{path} is not valid YAML{where}; it was skipped"
            return {}
        return data if isinstance(data, dict) else {}

    def _read_user_file(self) -> dict[str, Any]:
        # BaseLoader keeps every value a string: "0123" stays "0123", "yes" stays "yes".
        data = self._read(self.user_file, lambda text: yaml.load(text, Loader=yaml.BaseLoader))
        return {k: v for k, v in data.items() if isinstance(v, str)}

    def _read_dotenv(self, path: Path | None) -> dict[str, str]:
        return self._read(path, parse_dotenv)


class LocalCredentialsConfig(BaseModel):
    user_file: Path | None = None  # default: $XDG_CONFIG_HOME/runspool/credentials.yaml
    profile_dotenv: bool = True  # read .env next to the profile
    home_dotenv: bool = True  # read ~/.env


def _apply(ctx: Any, config: LocalCredentialsConfig) -> None:
    base_dir = ctx.config.base_dir
    user_file = config.user_file
    if user_file is not None and not user_file.is_absolute() and base_dir is not None:
        user_file = base_dir / user_file  # relative to the profile, not the shell's cwd
    service = LocalCredentials(
        user_file=user_file,
        profile_env=(base_dir / ".env") if (config.profile_dotenv and base_dir) else None,
        home_env=(Path.home() / ".env") if config.home_dotenv else None,
    )
    ctx.provide("credentials", service)
    ctx.doctor.register(
        lambda: Check(
            "credentials file",
            service.file_problem() is None,
            service.file_problem() or f"{service.user_file} (owner-only or absent)",
        )
    )


plugin = Plugin(
    name="credentials-local",
    apply=_apply,
    Config=LocalCredentialsConfig,
    inject=["config", "doctor"],
)
