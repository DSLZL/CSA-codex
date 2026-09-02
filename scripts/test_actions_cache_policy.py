#!/usr/bin/env python3
"""Tests for GitHub Actions cache audit and explicit legacy cleanup."""

from __future__ import annotations

import unittest

from actions_cache_policy import PolicyError, classify, plan_cleanup


def cache(
    cache_id: int,
    key: str,
    size: int,
    accessed: str,
    ref: str = "refs/heads/main",
) -> dict[str, object]:
    return {
        "id": cache_id,
        "key": key,
        "ref": ref,
        "size_in_bytes": size,
        "last_accessed_at": accessed,
    }


class CachePolicyTests(unittest.TestCase):
    def test_classifies_only_owned_and_recognized_legacy_keys(self) -> None:
        self.assertEqual(classify("/sccache/abcd"), "sccache")
        self.assertEqual(classify("sccache/abcd"), "sccache")
        self.assertEqual(classify("csa-cargo-downloads-v1-Linux-x64"), "legacy")
        self.assertEqual(classify("win32-x64-mbx-v1"), "legacy")
        self.assertEqual(classify("unrelated-cache"), "unknown")

    def test_audit_never_deletes_sccache_or_unknown_entries(self) -> None:
        inventory = {
            "actions_caches": [
                cache(1, "/sccache/a", 10_000, "2026-09-01T00:00:00Z"),
                cache(2, "unrelated", 20_000, "2026-09-01T00:00:00Z"),
            ]
        }
        result = plan_cleanup(inventory)
        self.assertEqual(result["result"], "audit_only")
        self.assertEqual(result["deletions"], [])
        self.assertEqual(result["projected_bytes"], 30_000)

    def test_explicit_legacy_purge_preserves_unknown_entries(self) -> None:
        inventory = [
            {
                "actions_caches": [
                    cache(1, "csa-sccache-local-v1-Linux", 20, "2026-09-01T00:00:00Z"),
                ]
            },
            {
                "actions_caches": [
                    cache(2, "unrelated", 30, "2026-09-01T00:00:00Z"),
                    cache(3, "/sccache/kept", 40, "2026-09-02T00:00:00Z"),
                ]
            },
        ]
        result = plan_cleanup(
            inventory,
            purge_legacy=True,
        )
        self.assertEqual(result["result"], "legacy_purge")
        self.assertEqual([item["id"] for item in result["deletions"]], [1])
        self.assertEqual(result["projected_bytes"], 70)

    def test_rejects_duplicate_or_malformed_entries(self) -> None:
        duplicate = {
            "actions_caches": [
                cache(1, "/sccache/a", 1, "2026-09-01T00:00:00Z"),
                cache(1, "/sccache/b", 1, "2026-09-01T00:00:00Z"),
            ]
        }
        with self.assertRaises(PolicyError):
            plan_cleanup(duplicate)
        malformed = {
            "actions_caches": [cache(1, "/sccache/a", -1, "not-a-timestamp")]
        }
        with self.assertRaises(PolicyError):
            plan_cleanup(malformed)


if __name__ == "__main__":
    unittest.main()
