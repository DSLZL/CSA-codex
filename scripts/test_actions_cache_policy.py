#!/usr/bin/env python3
"""Tests for the bounded GitHub Actions cache policy."""

from __future__ import annotations

import unittest

from actions_cache_policy import PolicyError, classify, plan_cleanup


DEFAULT_REF = "refs/heads/main"


def cache(
    cache_id: int,
    key: str,
    size: int,
    accessed: str,
    ref: str = DEFAULT_REF,
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

    def test_stays_idle_below_high_water(self) -> None:
        inventory = {
            "actions_caches": [
                cache(1, "/sccache/a", 40, "2026-09-01T00:00:00Z"),
                cache(2, "unrelated", 40, "2026-09-01T00:00:00Z"),
            ]
        }
        result = plan_cleanup(
            inventory, default_ref=DEFAULT_REF, high_water_bytes=100, low_water_bytes=60
        )
        self.assertEqual(result["result"], "within_limit")
        self.assertEqual(result["deletions"], [])

    def test_prunes_non_default_then_oldest_default_to_low_water(self) -> None:
        inventory = {
            "actions_caches": [
                cache(1, "/sccache/main-old", 50, "2026-09-01T00:00:00Z"),
                cache(2, "/sccache/main-new", 40, "2026-09-03T00:00:00Z"),
                cache(
                    3,
                    "/sccache/branch",
                    30,
                    "2026-09-04T00:00:00Z",
                    "refs/heads/topic",
                ),
                cache(4, "unknown", 20, "2026-09-01T00:00:00Z"),
            ]
        }
        result = plan_cleanup(
            inventory, default_ref=DEFAULT_REF, high_water_bytes=100, low_water_bytes=60
        )
        self.assertEqual(result["result"], "prune")
        self.assertEqual([item["id"] for item in result["deletions"]], [3, 1])
        self.assertEqual(result["projected_bytes"], 60)

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
                ]
            },
        ]
        result = plan_cleanup(
            inventory,
            default_ref=DEFAULT_REF,
            high_water_bytes=100,
            low_water_bytes=60,
            purge_legacy=True,
        )
        self.assertEqual(result["result"], "prune")
        self.assertEqual([item["id"] for item in result["deletions"]], [1])
        self.assertEqual(result["projected_bytes"], 30)

    def test_reports_blocked_when_preserved_entries_exceed_low_water(self) -> None:
        inventory = {
            "actions_caches": [
                cache(1, "/sccache/a", 10, "2026-09-01T00:00:00Z"),
                cache(2, "unrelated", 100, "2026-09-01T00:00:00Z"),
            ]
        }
        result = plan_cleanup(
            inventory, default_ref=DEFAULT_REF, high_water_bytes=100, low_water_bytes=60
        )
        self.assertEqual(result["result"], "blocked")
        self.assertEqual([item["id"] for item in result["deletions"]], [1])
        self.assertEqual(result["projected_bytes"], 100)

    def test_rejects_duplicate_or_malformed_entries(self) -> None:
        duplicate = {
            "actions_caches": [
                cache(1, "/sccache/a", 1, "2026-09-01T00:00:00Z"),
                cache(1, "/sccache/b", 1, "2026-09-01T00:00:00Z"),
            ]
        }
        with self.assertRaises(PolicyError):
            plan_cleanup(
                duplicate, default_ref=DEFAULT_REF, high_water_bytes=100, low_water_bytes=60
            )
        malformed = {
            "actions_caches": [cache(1, "/sccache/a", -1, "not-a-timestamp")]
        }
        with self.assertRaises(PolicyError):
            plan_cleanup(
                malformed, default_ref=DEFAULT_REF, high_water_bytes=100, low_water_bytes=60
            )


if __name__ == "__main__":
    unittest.main()
