"""Tests for the CLI entrypoint."""

from __future__ import annotations

from pathlib import Path

from repo_sync.strip.cli import main


def _write(root: Path, relpath: str, content: str) -> Path:
    """Create a file under *root* at *relpath* with the given content."""
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


class TestCli:
    """Tests for the repo-sync-strip CLI."""

    def test_strip_success(self, tmp_path: Path) -> None:
        """CLI returns 0 and strips private content on success."""
        _write(
            tmp_path,
            "code.rs",
            "pub\n// !repo-sync: private-start\npriv\n// !repo-sync: private-end\n",
        )
        rc = main([str(tmp_path)])
        assert rc == 0
        assert (tmp_path / "code.rs").read_text(encoding="utf-8") == "pub\n"

    def test_strip_failure(self, tmp_path: Path) -> None:
        """CLI returns 1 on marker errors."""
        _write(tmp_path, "bad.rs", "// !repo-sync: private-start\nno end\n")
        rc = main([str(tmp_path)])
        assert rc == 1

    def test_validate_only_success(self, tmp_path: Path) -> None:
        """CLI --validate-only returns 0 on valid markers."""
        _write(
            tmp_path,
            "code.rs",
            "// !repo-sync: private-start\npriv\n// !repo-sync: private-end\n",
        )
        rc = main(["--validate-only", str(tmp_path)])
        assert rc == 0

    def test_validate_only_failure(self, tmp_path: Path) -> None:
        """CLI --validate-only returns 1 on invalid markers."""
        _write(tmp_path, "bad.rs", "// !repo-sync: private-start\n")
        rc = main(["--validate-only", str(tmp_path)])
        assert rc == 1

    def test_validate_only_with_paths(self, tmp_path: Path) -> None:
        """CLI --validate-only with explicit paths validates only those files."""
        _write(
            tmp_path,
            "good.rs",
            "// !repo-sync: private-start\n// !repo-sync: private-end\n",
        )
        _write(tmp_path, "bad.rs", "// !repo-sync: private-start\n")
        # Only validate good.rs.
        rc = main(["--validate-only", str(tmp_path), "good.rs"])
        assert rc == 0
