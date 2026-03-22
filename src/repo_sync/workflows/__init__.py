"""Workflow orchestration logic for repo-sync.

These modules contain all decision-making and data manipulation logic that
was previously embedded in the GitHub Actions YAML as shell scripts.  The
YAML workflows are now thin shells that call Python CLI entrypoints.

This package builds on top of the stack management library
(repo_sync.stack) for git/gh operations, trailer parsing, watermark
management, etc.
"""
