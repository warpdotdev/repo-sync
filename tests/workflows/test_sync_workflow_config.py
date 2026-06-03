"""Tests for reusable sync workflow configuration."""

from __future__ import annotations

from pathlib import Path


def test_sync_workflow_configures_lfs_for_both_checkouts() -> None:
    workflow = Path(".github/workflows/sync.yml").read_text(encoding="utf-8")

    assert "- name: Configure Git LFS" in workflow
    assert "git lfs install --local" in workflow
    assert "git -C peer lfs install --local" in workflow
    assert workflow.index("- name: Configure Git LFS") < workflow.index(
        "- name: Run sync"
    )
