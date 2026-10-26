"""Bounded, owner-only, atomic updates of the application's gitignored .env file.

The file is plaintext, not encryption. Reads use python-dotenv's real parser (no shell, no
interpolation) and never export values to os.environ. Writes replace only the bindings this
module is asked to change and keep every other byte of the file as it was.
"""

import io
import os
import re
import stat
import tempfile
import threading
from pathlib import Path

from dotenv.parser import parse_stream

MAX_FILE = 256 * 1024
MAX_VALUE = 16 * 1024
MAX_NAMES = 8
NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
LEAD = re.compile(r"\s*(?:export[^\S\r\n]+)?")
ENDING = re.compile(r"(?:\r\n|\n|\r)$")
BOM = "﻿"
_LOCK = threading.RLock()


class DotenvError(Exception):
    """Redacted failure: reasons never include file contents, names or values."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def default_env_path():
    """SPENDING_OPTIMISER_ENV, else .env in the directory the app was started from."""
    configured = os.environ.get("SPENDING_OPTIMISER_ENV")
    return Path(configured) if configured else Path.cwd() / ".env"


def quoted(value):
    """Single-quote form of python-dotenv: only backslash and quote need escaping."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def checked_value(value):
    if (
        not isinstance(value, str)
        or len(value) > MAX_VALUE
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise DotenvError("invalid_value")


def bindings(text):
    return list(parse_stream(io.StringIO(text)))


def parsed(text):
    """Last binding wins, as in every dotenv loader. Valueless keys are absent."""
    return {b.key: b.value for b in bindings(text) if b.key and b.value is not None}


def without(found, names):
    return {key: value for key, value in found.items() if key not in names}


def rewrite(text, assign, remove):
    """Replace or append `assign`, drop `remove`; every other binding is kept verbatim."""
    owned = {*assign, *remove}
    current = bindings(text)
    last = {b.key: i for i, b in enumerate(current) if b.key in owned}
    kept, written = [], set()
    for index, binding in enumerate(current):
        original = binding.original.string
        if binding.key not in owned:
            kept.append(original)
        elif binding.key in assign and last[binding.key] == index:
            ending = ENDING.search(original)
            kept.append(
                LEAD.match(original).group(0)
                + f"{binding.key}={quoted(assign[binding.key])}"
                + (ending.group(0) if ending else "\n")
            )
            written.add(binding.key)
        # Earlier duplicates and removed names are dropped.
    result = "".join(kept)
    if result and not result.endswith(("\n", "\r")):
        result += "\n"
    for name, value in assign.items():
        if name not in written:
            result += f"{name}={quoted(value)}\n"
    return (BOM if text.startswith(BOM) else "") + result


class DotenvFile:
    def __init__(self, path):
        self.path = Path(path)

    def _text(self):
        try:
            before = os.lstat(self.path)
        except FileNotFoundError:
            return ""
        except OSError:
            raise DotenvError("unreadable") from None
        if not stat.S_ISREG(before.st_mode) or (
            hasattr(os, "geteuid") and before.st_uid != os.geteuid()
        ):
            raise DotenvError("unsafe_path")
        if before.st_size > MAX_FILE:
            raise DotenvError("too_large")
        try:
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise DotenvError("unsafe_path")
                data = stream.read(MAX_FILE + 1)
                if os.name == "posix" and stat.S_IMODE(opened.st_mode) & 0o077:
                    os.fchmod(stream.fileno(), 0o600)  # Tighten a hand-edited file, never loosen.
            if len(data) > MAX_FILE:
                raise DotenvError("too_large")
            return data.decode("utf-8")
        except DotenvError:
            raise
        except (OSError, UnicodeDecodeError):
            raise DotenvError("unreadable") from None

    def values(self, names):
        """Only the requested names, so callers never hold unrelated settings."""
        with _LOCK:
            found = parsed(self._text())
        return {name: found[name] for name in names if name in found}

    def update(self, assign=None, remove=()):
        """One atomic read-modify-write. Any failure leaves the existing file untouched."""
        assign, remove = dict(assign or {}), tuple(remove)
        owned = {*assign, *remove}
        if (
            len(owned) > MAX_NAMES
            or len(owned) != len(assign) + len(remove)
            or not all(NAME.fullmatch(name) for name in owned)
        ):
            raise DotenvError("invalid_name")
        for value in assign.values():
            checked_value(value)
        with _LOCK:
            text = self._text()
            result = rewrite(text, assign, remove)
            if result == text:
                return
            data = result.encode("utf-8")
            if len(data) > MAX_FILE:
                raise DotenvError("too_large")
            # Refuse to write anything a real parse would read differently.
            before, after = parsed(text), parsed(result)
            if (
                without(before, owned) != without(after, owned)
                or any(after.get(name) != value for name, value in assign.items())
                or any(name in after for name in remove)
            ):
                raise DotenvError("write_failed")
            self._replace(data)

    def _replace(self, data):
        parent = self.path.parent
        temporary = None
        try:
            # Named .env.*.tmp so a leftover is still matched by the repository's .gitignore.
            descriptor, temporary = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=parent)
            with os.fdopen(descriptor, "wb") as stream:  # mkstemp creates the file as 0600.
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            raise DotenvError("write_failed") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        try:
            directory = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass  # The rename is complete; the directory sync is best effort.
