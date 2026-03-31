"""Tests for full-tree stripping logic.

Covers private directory removal, symlink handling, text/binary detection
at the tree level, UTF-8 handling, and the validate-only mode used by
the CI validation action.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from repo_sync.strip.tree import StrippingError, remove_private_directories, strip_tree


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(root: Path, relpath: str, content: str | bytes) -> Path:
    """Create a file under *root* at *relpath* with the given content."""
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Private directory removal
# ---------------------------------------------------------------------------


class TestRemovePrivateDirectories:
    """Tests for private/ directory removal."""

    def test_top_level_private(self, tmp_path: Path) -> None:
        """A top-level ``private/`` directory is removed."""
        _write(tmp_path, "private/secret.txt", "secret")
        _write(tmp_path, "public.txt", "public")
        remove_private_directories(str(tmp_path))
        assert not (tmp_path / "private").exists()
        assert (tmp_path / "public.txt").exists()

    def test_nested_private(self, tmp_path: Path) -> None:
        """Nested ``private/`` dirs at various depths are removed."""
        _write(tmp_path, "crates/private/lib.rs", "secret")
        _write(tmp_path, "src/a/b/private/deep.rs", "secret")
        _write(tmp_path, "keep.txt", "public")
        remove_private_directories(str(tmp_path))
        assert not (tmp_path / "crates" / "private").exists()
        assert not (tmp_path / "src" / "a" / "b" / "private").exists()
        assert (tmp_path / "keep.txt").exists()

    def test_substring_not_removed(self, tmp_path: Path) -> None:
        """Directories with ``private`` as substring are not removed."""
        _write(tmp_path, "private-utils/tool.py", "public")
        _write(tmp_path, "my_private/stuff.py", "public")
        remove_private_directories(str(tmp_path))
        assert (tmp_path / "private-utils" / "tool.py").exists()
        assert (tmp_path / "my_private" / "stuff.py").exists()

    def test_nested_private_inside_private(self, tmp_path: Path) -> None:
        """Nested private/sub/private/ -- outer removal handles everything."""
        _write(tmp_path, "private/sub/private/deep.txt", "secret")
        remove_private_directories(str(tmp_path))
        assert not (tmp_path / "private").exists()

    def test_files_inside_private_absent(self, tmp_path: Path) -> None:
        """Files inside removed private/ dirs do not appear afterward."""
        _write(tmp_path, "private/a.txt", "a")
        _write(tmp_path, "private/b.txt", "b")
        remove_private_directories(str(tmp_path))
        assert not (tmp_path / "private" / "a.txt").exists()
        assert not (tmp_path / "private" / "b.txt").exists()

    def test_binary_files_in_private_removed(self, tmp_path: Path) -> None:
        """Directories named private that contain only binary files are still removed."""
        _write(tmp_path, "private/image.png", b"\x89PNG\r\n\x1a\n\x00\x00")
        remove_private_directories(str(tmp_path))
        assert not (tmp_path / "private").exists()


# ---------------------------------------------------------------------------
# Symlink handling
# ---------------------------------------------------------------------------


class TestSymlinkHandling:
    """Tests for symlink validation during tree stripping."""

    def test_symlink_to_non_private_target_allowed(self, tmp_path: Path) -> None:
        """A symlink to a non-private target is kept in the snapshot."""
        _write(tmp_path, "real.txt", "content")
        (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
        strip_tree(str(tmp_path))
        assert (tmp_path / "link.txt").is_symlink()
        assert (tmp_path / "real.txt").exists()

    def test_symlink_with_relative_parent_traversal_allowed(self, tmp_path: Path) -> None:
        """A symlink using ``..`` that stays within the repo is allowed."""
        _write(tmp_path, "data/file.txt", "content")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "link.txt").symlink_to("../data/file.txt")
        strip_tree(str(tmp_path))
        assert (tmp_path / "src" / "link.txt").is_symlink()

    def test_symlink_inside_private_no_error(self, tmp_path: Path) -> None:
        """A symlink inside a private/ directory does not raise (it is removed)."""
        _write(tmp_path, "public.txt", "content")
        private_dir = tmp_path / "private"
        private_dir.mkdir()
        (private_dir / "link.txt").symlink_to(tmp_path / "public.txt")
        # Should not raise because the symlink is removed with the private dir.
        strip_tree(str(tmp_path))
        assert not private_dir.exists()

    def test_symlink_named_private_to_non_private_target(self, tmp_path: Path) -> None:
        """A symlink named ``private`` pointing to a non-private dir is allowed.

        It survives directory removal because it is a symlink, and its
        target is not under a ``private/`` directory.
        """
        target = tmp_path / "real_dir"
        target.mkdir()
        _write(tmp_path, "real_dir/file.txt", "content")
        (tmp_path / "private").symlink_to(target)
        strip_tree(str(tmp_path))
        assert (tmp_path / "private").is_symlink()

    def test_symlink_pointing_outside_repo(self, tmp_path: Path) -> None:
        """A symlink pointing outside the repo raises an error."""
        (tmp_path / "link.txt").symlink_to("/etc/hosts")
        with pytest.raises(StrippingError, match="escapes the repository root"):
            strip_tree(str(tmp_path))

    def test_symlink_pointing_to_private_dir(self, tmp_path: Path) -> None:
        """A symlink whose target resolves into a private/ directory raises an error.

        The actual private/ dir is removed first, but the symlink (at a
        different location) survives and its target path is checked.
        """
        _write(tmp_path, "private/secret.txt", "secret")
        (tmp_path / "sneaky").symlink_to(tmp_path / "private")
        with pytest.raises(StrippingError, match="resolves into a private/ directory"):
            strip_tree(str(tmp_path))

    def test_symlink_to_file_inside_private(self, tmp_path: Path) -> None:
        """A symlink targeting a file inside a private/ directory raises an error."""
        _write(tmp_path, "private/secret.txt", "secret")
        (tmp_path / "leak.txt").symlink_to("private/secret.txt")
        with pytest.raises(StrippingError, match="resolves into a private/ directory"):
            strip_tree(str(tmp_path))

    def test_symlink_to_nested_private_dir(self, tmp_path: Path) -> None:
        """A symlink targeting a path through a nested private/ directory errors."""
        _write(tmp_path, "crates/private/lib.rs", "secret")
        (tmp_path / "leak.txt").symlink_to("crates/private/lib.rs")
        with pytest.raises(StrippingError, match="resolves into a private/ directory"):
            strip_tree(str(tmp_path))

    def test_symlink_escapes_repo_via_parent_traversal(self, tmp_path: Path) -> None:
        """A relative symlink that escapes the repo via ``..`` raises an error."""
        (tmp_path / "link.txt").symlink_to("../../outside/file.txt")
        with pytest.raises(StrippingError, match="escapes the repository root"):
            strip_tree(str(tmp_path))

    def test_validate_only_detects_private_symlink(self, tmp_path: Path) -> None:
        """Validate-only mode reports symlinks targeting private/ directories."""
        _write(tmp_path, "private/secret.txt", "secret")
        (tmp_path / "leak.txt").symlink_to("private/secret.txt")
        errors = strip_tree(str(tmp_path), validate_only=True).errors
        assert any("private/ directory" in e for e in errors)

    def test_validate_only_allows_safe_symlink(self, tmp_path: Path) -> None:
        """Validate-only mode does not report symlinks to non-private targets."""
        _write(tmp_path, "real.txt", "content")
        (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
        errors = strip_tree(str(tmp_path), validate_only=True).errors
        assert errors == []


# ---------------------------------------------------------------------------
# Text vs. binary detection (tree level)
# ---------------------------------------------------------------------------


class TestBinaryDetectionTree:
    """Tests for binary file handling during tree stripping."""

    def test_binary_file_left_as_is(self, tmp_path: Path) -> None:
        """A binary file is left in the snapshot unchanged."""
        data = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        _write(tmp_path, "image.png", data)
        strip_tree(str(tmp_path))
        assert (tmp_path / "image.png").read_bytes() == data

    def test_text_file_processed(self, tmp_path: Path) -> None:
        """A text file with markers is processed."""
        content = (
            "public\n"
            "// !repo-sync: private-start\n"
            "private\n"
            "// !repo-sync: private-end\n"
        )
        _write(tmp_path, "code.rs", content)
        strip_tree(str(tmp_path))
        assert (tmp_path / "code.rs").read_text(encoding="utf-8") == "public\n"

    def test_binary_in_private_dir_removed(self, tmp_path: Path) -> None:
        """A binary file inside private/ is removed (directory removal first)."""
        _write(tmp_path, "private/font.woff", b"\x00woff")
        _write(tmp_path, "public.txt", "content")
        strip_tree(str(tmp_path))
        assert not (tmp_path / "private").exists()
        assert (tmp_path / "public.txt").exists()


# ---------------------------------------------------------------------------
# UTF-8 handling
# ---------------------------------------------------------------------------


class TestUtf8Handling:
    """Tests for UTF-8 decoding behavior."""

    def test_valid_utf8_with_markers(self, tmp_path: Path) -> None:
        """A valid UTF-8 file with markers is processed correctly."""
        content = (
            "café\n"
            "# !repo-sync: private-start\n"
            "privé\n"
            "# !repo-sync: private-end\n"
            "naïve\n"
        )
        _write(tmp_path, "text.py", content)
        strip_tree(str(tmp_path))
        assert (tmp_path / "text.py").read_text(encoding="utf-8") == "café\nnaïve\n"

    def test_non_utf8_file_treated_as_binary(self, tmp_path: Path) -> None:
        """A file that fails UTF-8 decoding is skipped with a warning."""
        data = "café".encode("latin-1")
        _write(tmp_path, "bad.txt", data)
        result = strip_tree(str(tmp_path))
        assert result.errors == []
        assert any("not valid UTF-8" in w for w in result.warnings)
        # The file is left untouched.
        assert (tmp_path / "bad.txt").read_bytes() == data

    def test_utf16_classified_as_binary(self, tmp_path: Path) -> None:
        """A UTF-16 file (contains null bytes) is classified as binary."""
        content = "hello\n".encode("utf-16")
        _write(tmp_path, "utf16.txt", content)
        # Should not raise -- null bytes mean binary, so no decoding attempted.
        strip_tree(str(tmp_path))
        assert (tmp_path / "utf16.txt").read_bytes() == content

    def test_bom_utf8_handled(self, tmp_path: Path) -> None:
        """A file with a UTF-8 BOM is handled correctly."""
        content = "\ufeff" + "public\n# !repo-sync: private-start\nprivate\n# !repo-sync: private-end\n"
        _write(tmp_path, "bom.py", content)
        strip_tree(str(tmp_path))
        result = (tmp_path / "bom.py").read_text(encoding="utf-8")
        assert "private" not in result
        assert result.startswith("\ufeff")


# ---------------------------------------------------------------------------
# Full tree stripping (integration)
# ---------------------------------------------------------------------------


class TestStripTreeIntegration:
    """Integration tests for the full strip_tree function."""

    def test_files_added_appear_in_snapshot(self, tmp_path: Path) -> None:
        """Files in the tree appear in the clean snapshot."""
        _write(tmp_path, "new_file.txt", "content")
        strip_tree(str(tmp_path))
        assert (tmp_path / "new_file.txt").exists()

    def test_mixed_public_private(self, tmp_path: Path) -> None:
        """A tree with private dirs and marked files is cleaned correctly."""
        _write(tmp_path, "private/secret.rs", "secret")
        _write(
            tmp_path,
            "lib.rs",
            "pub\n// !repo-sync: private-start\npriv\n// !repo-sync: private-end\npub2\n",
        )
        _write(tmp_path, "image.png", b"\x89PNG\x00")
        strip_tree(str(tmp_path))

        assert not (tmp_path / "private").exists()
        assert (tmp_path / "lib.rs").read_text(encoding="utf-8") == "pub\npub2\n"
        assert (tmp_path / "image.png").exists()

    def test_entirely_stripped_file_kept_empty(self, tmp_path: Path) -> None:
        """A file where all content is private becomes an empty file."""
        _write(
            tmp_path,
            "all_private.rs",
            "// !repo-sync: private-start\nsecret\n// !repo-sync: private-end\n",
        )
        strip_tree(str(tmp_path))
        assert (tmp_path / "all_private.rs").exists()
        assert (tmp_path / "all_private.rs").read_text(encoding="utf-8") == ""

    def test_marker_error_raises_stripping_error(self, tmp_path: Path) -> None:
        """A marker error in any file causes StrippingError."""
        _write(
            tmp_path,
            "bad.rs",
            "// !repo-sync: private-start\ncontent\n",
        )
        with pytest.raises(StrippingError, match="unterminated"):
            strip_tree(str(tmp_path))

    def test_binary_files_included_in_snapshot(self, tmp_path: Path) -> None:
        """Binary files (images, fonts, etc.) are included in the snapshot."""
        data = b"\x00\x01\x02\x03"
        _write(tmp_path, "font.woff", data)
        strip_tree(str(tmp_path))
        assert (tmp_path / "font.woff").read_bytes() == data

    def test_file_permissions_preserved(self, tmp_path: Path) -> None:
        """File permissions are preserved after stripping."""
        p = _write(
            tmp_path,
            "exec.sh",
            "#!/bin/bash\n# !repo-sync: private-start\nsecret\n# !repo-sync: private-end\npublic\n",
        )
        p.chmod(0o755)
        strip_tree(str(tmp_path))
        assert p.stat().st_mode & 0o777 == 0o755
        assert p.read_text(encoding="utf-8") == "#!/bin/bash\npublic\n"


# ---------------------------------------------------------------------------
# CI validation action (validate-only mode)
# ---------------------------------------------------------------------------


class TestValidateOnlyMode:
    """Tests for validate-only mode (CI validation action)."""

    def test_detects_unpaired_start(self, tmp_path: Path) -> None:
        """Detects unpaired private-start."""
        _write(tmp_path, "bad.rs", "// !repo-sync: private-start\ncontent\n")
        errors = strip_tree(str(tmp_path), validate_only=True).errors
        assert any("unterminated" in e for e in errors)

    def test_detects_unpaired_end(self, tmp_path: Path) -> None:
        """Detects unpaired private-end."""
        _write(tmp_path, "bad.rs", "content\n// !repo-sync: private-end\n")
        errors = strip_tree(str(tmp_path), validate_only=True).errors
        assert any("without matching" in e for e in errors)

    def test_detects_nested_markers(self, tmp_path: Path) -> None:
        """Detects nested markers."""
        _write(
            tmp_path,
            "bad.rs",
            "// !repo-sync: private-start\n"
            "// !repo-sync: private-start\n"
            "content\n"
            "// !repo-sync: private-end\n",
        )
        errors = strip_tree(str(tmp_path), validate_only=True).errors
        assert any("nested" in e for e in errors)

    def test_detects_symlink_to_private(self, tmp_path: Path) -> None:
        """Detects symlinks targeting a private/ directory."""
        _write(tmp_path, "private/secret.txt", "secret")
        (tmp_path / "leak.txt").symlink_to("private/secret.txt")
        errors = strip_tree(str(tmp_path), validate_only=True).errors
        assert any("private/ directory" in e for e in errors)

    def test_allows_safe_symlink(self, tmp_path: Path) -> None:
        """A symlink to a non-private target produces no errors."""
        _write(tmp_path, "real.txt", "content")
        (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
        errors = strip_tree(str(tmp_path), validate_only=True).errors
        assert errors == []

    def test_passes_clean_repo(self, tmp_path: Path) -> None:
        """Passes on a repo with no markers and no symlinks."""
        _write(tmp_path, "code.rs", "fn main() {}\n")
        errors = strip_tree(str(tmp_path), validate_only=True).errors
        assert errors == []

    def test_passes_valid_markers(self, tmp_path: Path) -> None:
        """Passes on a repo with correctly paired, non-nested markers."""
        _write(
            tmp_path,
            "code.rs",
            "pub\n// !repo-sync: private-start\npriv\n// !repo-sync: private-end\npub2\n",
        )
        errors = strip_tree(str(tmp_path), validate_only=True).errors
        assert errors == []

    def test_paths_filter(self, tmp_path: Path) -> None:
        """Respects the paths input filter (only validates specified files)."""
        _write(
            tmp_path,
            "good.rs",
            "// !repo-sync: private-start\n// !repo-sync: private-end\n",
        )
        _write(
            tmp_path,
            "bad.rs",
            "// !repo-sync: private-start\nno end\n",
        )
        # Only validate good.rs -- should pass.
        errors = strip_tree(
            str(tmp_path), validate_only=True, paths=["good.rs"]
        ).errors
        assert errors == []

        # Validate bad.rs -- should fail.
        errors = strip_tree(
            str(tmp_path), validate_only=True, paths=["bad.rs"]
        ).errors
        assert len(errors) > 0

    def test_validate_only_does_not_modify(self, tmp_path: Path) -> None:
        """Validate-only mode does not modify any files."""
        content = "pub\n// !repo-sync: private-start\npriv\n// !repo-sync: private-end\n"
        _write(tmp_path, "code.rs", content)
        strip_tree(str(tmp_path), validate_only=True)
        assert (tmp_path / "code.rs").read_text(encoding="utf-8") == content

    def test_validate_only_does_not_remove_private_dirs(self, tmp_path: Path) -> None:
        """Validate-only mode does not remove private/ directories."""
        _write(tmp_path, "private/secret.txt", "secret")
        strip_tree(str(tmp_path), validate_only=True)
        assert (tmp_path / "private" / "secret.txt").exists()

    def test_symlink_inside_private_ignored_in_validate_only(self, tmp_path: Path) -> None:
        """In validate-only mode, symlinks inside private/ are not flagged.

        Since private/ directories would be removed during stripping,
        symlinks inside them are not a concern.
        """
        _write(tmp_path, "public.txt", "content")
        private_dir = tmp_path / "private"
        private_dir.mkdir()
        (private_dir / "link.txt").symlink_to(tmp_path / "public.txt")
        errors = strip_tree(str(tmp_path), validate_only=True).errors
        assert errors == []

    def test_paths_glob_pattern(self, tmp_path: Path) -> None:
        """Glob patterns in paths are expanded correctly."""
        _write(
            tmp_path,
            "src/good.rs",
            "// !repo-sync: private-start\n// !repo-sync: private-end\n",
        )
        _write(
            tmp_path,
            "src/bad.rs",
            "// !repo-sync: private-start\nno end\n",
        )
        # Glob matching all .rs files under src/.
        errors = strip_tree(
            str(tmp_path), validate_only=True, paths=["src/*.rs"]
        ).errors
        assert any("unterminated" in e for e in errors)

    def test_paths_no_match_errors(self, tmp_path: Path) -> None:
        """A paths filter that matches zero files produces an error."""
        _write(tmp_path, "code.rs", "fn main() {}\n")
        errors = strip_tree(
            str(tmp_path), validate_only=True, paths=["nonexistent/*.xyz"]
        ).errors
        assert any("matched no files" in e for e in errors)
