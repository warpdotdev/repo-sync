"""Shared error types for repo-sync subprocess wrappers."""

from __future__ import annotations

import subprocess


class VerboseCalledProcessError(subprocess.CalledProcessError):
    """CalledProcessError that includes stdout/stderr in its string representation."""

    def __str__(self) -> str:
        base = super().__str__()
        parts = [base]
        if self.stderr:
            stderr = (
                self.stderr
                if isinstance(self.stderr, str)
                else self.stderr.decode("utf-8", errors="replace")
            )
            stderr = stderr.strip()[:2000]
            if stderr:
                parts.append(f"stderr: {stderr}")
        if self.stdout:
            stdout = (
                self.stdout
                if isinstance(self.stdout, str)
                else self.stdout.decode("utf-8", errors="replace")
            )
            stdout = stdout.strip()[:2000]
            if stdout:
                parts.append(f"stdout: {stdout}")
        return "\n".join(parts)
