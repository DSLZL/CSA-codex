#!/usr/bin/env python3
"""Tests for sccache statistics validation."""

from __future__ import annotations

import unittest

from check_sccache_stats import StatsError, summarize


def stats(*, hits: int, misses: int, errors: int = 0) -> dict[str, object]:
    return {
        "stats": {
            "compile_requests": hits + misses + errors,
            "cache_hits": {"counts": {"Rust": hits}, "adv_counts": {}},
            "cache_misses": {"counts": {"Rust": misses}, "adv_counts": {}},
            "cache_errors": {"counts": {"Rust": errors}, "adv_counts": {}},
            "cache_read_errors": 0,
            "cache_writes": misses,
            "cache_write_errors": 0,
        },
        "cache_size": None,
        "max_cache_size": None,
    }


class SccacheStatsTests(unittest.TestCase):
    def test_reports_rust_requests_and_observational_hit_rate(self) -> None:
        result = summarize(stats(hits=3, misses=1))
        self.assertEqual(result["compile_requests"], 4)
        self.assertEqual(result["rust_requests"], 4)
        self.assertEqual(result["rust_hit_rate"], 75.0)
        self.assertEqual(result["cache_writes"], 1)
        self.assertEqual(result["warnings"], [])

    def test_surfaces_cache_errors_as_observational_warnings(self) -> None:
        document = stats(hits=1, misses=1, errors=1)
        document["stats"]["cache_read_errors"] = 2
        document["stats"]["cache_write_errors"] = 3
        result = summarize(document)
        self.assertEqual(result["rust_errors"], 1)
        self.assertEqual(result["cache_read_errors"], 2)
        self.assertEqual(result["cache_write_errors"], 3)
        self.assertEqual(
            result["warnings"],
            [
                "sccache recorded 1 Rust cache request errors",
                "sccache recorded 2 cache read errors",
                "sccache recorded 3 cache write errors",
            ],
        )

    def test_rejects_malformed_statistics(self) -> None:
        with self.assertRaises(StatsError):
            summarize({"stats": {"compile_requests": True}})


if __name__ == "__main__":
    unittest.main()
