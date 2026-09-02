#!/usr/bin/env python3
"""Audit GitHub Actions caches and plan explicitly authorized legacy cleanup."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn


SCCACHE_PREFIXES = ("sccache/", "/sccache/")
LEGACY_PREFIXES = (
    "csa-cargo-downloads-v1-",
    "csa-cargo-home-v5-",
    "csa-sccache-local-v1-",
)
LEGACY_MBX = re.compile(r"^(?:darwin|linux|win32)-(?:arm64|x64)-mbx-")


class PolicyError(RuntimeError):
    pass


def fail(message: str) -> NoReturn:
    raise PolicyError(message)


@dataclass(frozen=True)
class CacheEntry:
    cache_id: int
    key: str
    ref: str
    size: int
    last_accessed_at: str
    last_accessed: dt.datetime
    kind: str


def _integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        fail(f"{label} must be a non-negative integer")
    return value


def _timestamp(value: object, label: str) -> tuple[str, dt.datetime]:
    if not isinstance(value, str) or not value:
        fail(f"{label} must be a non-empty UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PolicyError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        fail(f"{label} must include a timezone")
    return value, parsed.astimezone(dt.timezone.utc)


def classify(key: str) -> str:
    if key.startswith(SCCACHE_PREFIXES):
        return "sccache"
    if key.startswith(LEGACY_PREFIXES) or LEGACY_MBX.match(key):
        return "legacy"
    return "unknown"


def load_inventory(document: object) -> list[CacheEntry]:
    pages = [document] if isinstance(document, dict) else document
    if not isinstance(pages, list) or not pages:
        fail("cache inventory must be one API response or a non-empty list of responses")
    entries: list[CacheEntry] = []
    seen: set[int] = set()
    for page_index, page in enumerate(pages):
        if not isinstance(page, dict) or not isinstance(page.get("actions_caches"), list):
            fail(f"cache inventory page {page_index} has no actions_caches list")
        for entry_index, raw in enumerate(page["actions_caches"]):
            label = f"cache inventory page {page_index} entry {entry_index}"
            if not isinstance(raw, dict):
                fail(f"{label} must be an object")
            cache_id = _integer(raw.get("id"), f"{label} id")
            if cache_id < 1 or cache_id in seen:
                fail(f"{label} id must be unique and positive")
            key = raw.get("key")
            ref = raw.get("ref")
            if not isinstance(key, str) or not key:
                fail(f"{label} key must be a non-empty string")
            if not isinstance(ref, str) or not ref:
                fail(f"{label} ref must be a non-empty string")
            accessed_text, accessed = _timestamp(
                raw.get("last_accessed_at"), f"{label} last_accessed_at"
            )
            entries.append(
                CacheEntry(
                    cache_id=cache_id,
                    key=key,
                    ref=ref,
                    size=_integer(raw.get("size_in_bytes"), f"{label} size_in_bytes"),
                    last_accessed_at=accessed_text,
                    last_accessed=accessed,
                    kind=classify(key),
                )
            )
            seen.add(cache_id)
    return entries


def _deletion(entry: CacheEntry, reason: str) -> dict[str, Any]:
    return {
        "id": entry.cache_id,
        "key": entry.key,
        "ref": entry.ref,
        "size_in_bytes": entry.size,
        "last_accessed_at": entry.last_accessed_at,
        "reason": reason,
    }


def plan_cleanup(
    document: object,
    *,
    purge_legacy: bool = False,
) -> dict[str, Any]:
    entries = load_inventory(document)
    total = sum(entry.size for entry in entries)
    deletions: list[dict[str, Any]] = []
    projected = total

    if purge_legacy:
        for entry in sorted(
            (item for item in entries if item.kind == "legacy"),
            key=lambda item: (item.last_accessed, item.cache_id),
        ):
            deletions.append(_deletion(entry, "legacy-explicit-purge"))
            projected -= entry.size

    result = "legacy_purge" if deletions else "audit_only"
    counts = {
        kind: sum(1 for entry in entries if entry.kind == kind)
        for kind in ("sccache", "legacy", "unknown")
    }
    sizes = {
        kind: sum(entry.size for entry in entries if entry.kind == kind)
        for kind in ("sccache", "legacy", "unknown")
    }
    return {
        "schema": 1,
        "result": result,
        "purge_legacy": purge_legacy,
        "total_count": len(entries),
        "total_bytes": total,
        "projected_bytes": projected,
        "deletion_count": len(deletions),
        "deletion_bytes": total - projected,
        "deletions": deletions,
        "counts": counts,
        "bytes": sizes,
    }


def _gib(value: int) -> str:
    return f"{value / 1024**3:.2f} GiB"


def append_summary(path: Path, result: dict[str, Any]) -> None:
    lines = [
        "### GitHub Actions cache audit",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Result | `{result['result']}` |",
        f"| Current usage | {_gib(result['total_bytes'])} |",
        f"| Projected after explicit legacy purge | {_gib(result['projected_bytes'])} |",
        f"| Planned legacy exact-ID deletions | {result['deletion_count']} ({_gib(result['deletion_bytes'])}) |",
        f"| Managed sccache | {result['counts']['sccache']} ({_gib(result['bytes']['sccache'])}) |",
        f"| Recognized legacy | {result['counts']['legacy']} ({_gib(result['bytes']['legacy'])}) |",
        f"| Unknown, preserved | {result['counts']['unknown']} ({_gib(result['bytes']['unknown'])}) |",
    ]
    if result["deletions"]:
        lines.extend(("", "| Cache ID | Reason | Ref | Size |", "| ---: | --- | --- | ---: |"))
        for deletion in result["deletions"][:20]:
            lines.append(
                f"| {deletion['id']} | {deletion['reason']} | `{deletion['ref']}` | "
                f"{_gib(deletion['size_in_bytes'])} |"
            )
        if len(result["deletions"]) > 20:
            lines.extend(("", f"Only the first 20 of {len(result['deletions'])} deletions are shown."))
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines) + "\n\n")


def write_github_output(path: Path, result: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"result={result['result']}\n")
        stream.write(f"total_bytes={result['total_bytes']}\n")
        stream.write(f"projected_bytes={result['projected_bytes']}\n")
        stream.write(f"deletion_count={result['deletion_count']}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--purge-legacy", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--github-step-summary", type=Path)
    args = parser.parse_args()
    try:
        document = json.loads(args.inventory.read_text(encoding="utf-8"))
        result = plan_cleanup(
            document,
            purge_legacy=args.purge_legacy,
        )
        args.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if args.github_output:
            write_github_output(args.github_output, result)
        if args.github_step_summary:
            append_summary(args.github_step_summary, result)
    except (OSError, json.JSONDecodeError, PolicyError) as error:
        print(json.dumps({"schema": 1, "error": str(error)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
