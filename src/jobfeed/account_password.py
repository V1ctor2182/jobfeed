"""Adapter to the shared password the account program types on employer sites.

The password goes one way. The dashboard can save it and can learn whether one
is set. No route, log line or error here returns it. It reaches the pipeline on
standard input, so no other process can list it as an argument.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

_REFUSED = 2


class AccountPasswordState(BaseModel):
    """Whether a shared password is set, and how many accounts already use it."""

    password_set: bool
    accounts: int


class AccountPasswordUpdate(BaseModel):
    """A new shared password typed by the user."""

    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=128)


class AccountPasswordError(Exception):
    """A password command failure with an API error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AccountPasswordService:
    """Reads the password's status and saves a new one through the pipeline's CLI."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _run(self, command: str, stdin: str | None) -> dict[str, object]:
        try:
            result = subprocess.run(
                [
                    str(self.root / ".venv/bin/python"),
                    "-m",
                    "pipeline.cli",
                    "--root",
                    str(self.root),
                    "accounts",
                    command,
                ],
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            # A refused password is reported on standard error.
            body = json.loads(
                result.stderr if result.returncode == _REFUSED else result.stdout
            )
            if not isinstance(body, dict):
                raise ValueError("Invalid account password response")
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            raise AccountPasswordError(
                "unavailable", "The shared password could not be reached."
            ) from None
        if result.returncode:
            raise AccountPasswordError(
                "invalid", str(body.get("error", "The password was not saved."))
            )
        return body

    def state(self) -> AccountPasswordState:
        """Report whether a shared password is set.

        Returns:
            Whether one is set and how many accounts were made with it.
        """
        body = self._run("list", None)
        accounts = body.get("accounts")
        return AccountPasswordState(
            password_set=bool(body.get("password_set")),
            accounts=len(accounts) if isinstance(accounts, list) else 0,
        )

    def save(self, update: AccountPasswordUpdate) -> AccountPasswordState:
        """Save a new shared password.

        Args:
            update: The password the user typed.

        Returns:
            The status after saving.
        """
        self._run("save-password", update.password)
        return self.state()
