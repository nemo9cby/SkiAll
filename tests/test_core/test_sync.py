"""Tests for the sync module."""

from __future__ import annotations

import pytest

from skiall.core.sync import SyncAction, classify_items


class TestClassifyItems:
    def test_remote_only(self):
        repo = {"a.txt": b"hello"}
        local = {}
        result = classify_items(repo, local)
        assert result == {"a.txt": SyncAction.REMOTE_ONLY}

    def test_local_only(self):
        repo = {}
        local = {"b.txt": b"world"}
        result = classify_items(repo, local)
        assert result == {"b.txt": SyncAction.LOCAL_ONLY}

    def test_identical(self):
        repo = {"c.txt": b"same"}
        local = {"c.txt": b"same"}
        result = classify_items(repo, local)
        assert result == {"c.txt": SyncAction.IDENTICAL}

    def test_conflict(self):
        repo = {"d.txt": b"repo version"}
        local = {"d.txt": b"local version"}
        result = classify_items(repo, local)
        assert result == {"d.txt": SyncAction.CONFLICT}

    def test_mixed(self):
        repo = {"a": b"1", "c": b"same", "d": b"repo"}
        local = {"b": b"2", "c": b"same", "d": b"local"}
        result = classify_items(repo, local)
        assert result == {
            "a": SyncAction.REMOTE_ONLY,
            "b": SyncAction.LOCAL_ONLY,
            "c": SyncAction.IDENTICAL,
            "d": SyncAction.CONFLICT,
        }
