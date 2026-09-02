#!/usr/bin/env python3
"""Report machine-readable sccache statistics and enforce basic cache health."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


class StatsError(RuntimeError):
    pass


def _integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise StatsError(f"{label} must be a non-negative integer")
    return value


def _optional_integer(value: object, label: str) -> int | None:
    return None if value is None else _integer(value, label)


def _sum_integers(value: object) -> int:
    if type(value) is int:
        return max(value, 0)
    if isinstance(value, dict):
        return sum(_sum_integers(item) for item in value.values())
    return 0


def _language_count(metric: object, language: str) -> int:
    if not isinstance(metric, dict):
        raise StatsError("per-language statistic must be an object")
    counts = metric.get("counts")
    if not isinstance(counts, dict):
        raise StatsError("per-language statistic is missing counts")
    if language in counts:
        return _integer(counts[language], f"{language} count")
    advanced = metric.get("adv_counts", {})
    return _sum_integers(advanced.get(language, {})) if isinstance(advanced, dict) else 0


def summarize(stats_document: object) -> dict[str, Any]:
    if not isinstance(stats_document, dict) or not isinstance(stats_document.get("stats"), dict):
        raise StatsError("sccache JSON must contain a stats object")
    stats = stats_document["stats"]
    compile_requests = _integer(stats.get("compile_requests"), "compile_requests")
    hits = _language_count(stats.get("cache_hits"), "Rust")
    misses = _language_count(stats.get("cache_misses"), "Rust")
    errors = _language_count(stats.get("cache_errors"), "Rust")
    read_errors = _integer(stats.get("cache_read_errors"), "cache_read_errors")
    writes = _integer(stats.get("cache_writes"), "cache_writes")
    write_errors = _integer(stats.get("cache_write_errors"), "cache_write_errors")
    cache_size = _optional_integer(stats_document.get("cache_size"), "cache_size")
    max_cache_size = _optional_integer(stats_document.get("max_cache_size"), "max_cache_size")
    rust_requests = hits + misses + errors
    hit_rate = hits * 100.0 / (hits + misses) if hits + misses else 0.0
    warnings = []
    if compile_requests == 0 or rust_requests == 0:
        warnings.append("Rust build recorded zero sccache compile requests")
    if read_errors or write_errors or errors:
        warnings.append("sccache recorded Rust or cache read/write errors")
    return {
        "schema": 1,
        "result": "reported",
        "compile_requests": compile_requests,
        "rust_requests": rust_requests,
        "rust_hits": hits,
        "rust_misses": misses,
        "rust_errors": errors,
        "rust_hit_rate": round(hit_rate, 2),
        "cache_writes": writes,
        "cache_size_bytes": cache_size,
        "max_cache_size_bytes": max_cache_size,
        "cache_read_errors": read_errors,
        "cache_write_errors": write_errors,
        "warnings": warnings,
    }


def _gib(value: int) -> str:
    return f"{value / 1024**3:.2f} GiB"


def append_github_summary(path: Path, profile: str, result: dict[str, Any]) -> None:
    lines = [f"### sccache: {profile}", ""]
    if result["result"] == "reported":
        cache_size = (
            "unavailable"
            if result["cache_size_bytes"] is None
            else _gib(result["cache_size_bytes"])
        )
        lines.extend(
            [
                "| Metric | Value |",
                "| --- | ---: |",
                f"| Rust hits | {result['rust_hits']} |",
                f"| Rust misses | {result['rust_misses']} |",
                f"| Rust errors | {result['rust_errors']} |",
                f"| Rust hit rate | {result['rust_hit_rate']:.2f}% |",
                f"| Cache writes | {result['cache_writes']} |",
                f"| Reported cache size | {cache_size} |",
            ]
        )
    else:
        lines.append("sccache statistics unavailable.")
    for warning in result["warnings"]:
        lines.extend(("", f"> WARNING: {warning}"))
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines) + "\n\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stats", type=Path, required=True)
    parser.add_argument("--profile", default="build")
    parser.add_argument("--github-step-summary", type=Path)
    parser.add_argument("--require-requests", action="store_true")
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    try:
        result = summarize(json.loads(args.stats.read_bytes()))
    except (OSError, json.JSONDecodeError, StatsError) as error:
        result = {"schema": 1, "result": "unavailable", "warnings": [str(error)]}
    result["profile"] = args.profile
    if args.github_step_summary is not None:
        try:
            append_github_summary(args.github_step_summary, args.profile, result)
        except OSError as error:
            result["warnings"].append(f"cannot write GitHub Step Summary: {error}")
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.require_requests and (
        result["result"] != "reported"
        or result["compile_requests"] == 0
        or result["rust_requests"] == 0
    ):
        return 2
    if args.require_clean and (
        result["result"] != "reported"
        or result["rust_errors"]
        or result["cache_read_errors"]
        or result["cache_write_errors"]
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
