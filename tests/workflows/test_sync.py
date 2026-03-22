"""Tests for repo_sync.workflows.sync."""

from __future__ import annotations

from repo_sync.workflows.sync import determine_direction


class TestDetermineDirection:
    """Tests for the sync direction helper."""

    def test_private_source(self) -> None:
        assert determine_direction(True) == "private-to-public"

    def test_public_source(self) -> None:
        assert determine_direction(False) == "public-to-private"
