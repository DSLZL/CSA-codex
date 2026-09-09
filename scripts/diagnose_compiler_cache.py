#!/usr/bin/env python3
"""Summarize Rust cache misses and probe Windows proc-macro reproducibility."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from collections import Counter
from pathlib import Path


TARGET = "x86_64-pc-windows-msvc"
CONSUMER = "csa_cache_probe_consumer"
CACHE_EVENT = re.compile(r"\[([^\]\r\n]+)\]: (get_cached_or_compile: |Hash key: |Cache hit in |Cache miss in |Compiled in )([^\r\n]+)")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_arguments(body: str) -> list[str]:
    """sccache logs Debug<Vec<OsString>>, whose Rust escapes are not all JSON escapes."""
    def rust_escape(match: re.Match[str]) -> str:
        token = match[0]
        if token.startswith(r"\u{"):
            return json.dumps(chr(int(token[3:-1], 16)))[1:-1]
        if token == r"\0":
            return r"\u0000"
        if token == r"\'":
            return "'"
        return token

    # Consume escaped backslashes too, so a literal path such as \\u{200e} stays literal.
    body = re.sub(r"\\(?:[\"'\\bfnrt/]|0|u\{[0-9a-fA-F]{1,6}\})", rust_escape, body)
    arguments = json.loads(body)
    if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
        raise ValueError("invalid argument vector")
    return arguments


def write_events(path: Path, text: str) -> None:
    """Retain only compiler cache events, excluding unrelated logs and environment dumps."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for match in CACHE_EVENT.finditer(text):
            stream.write(match[0] + "\n")


def pe_identity(data: bytes) -> dict:
    """Keep section hashes and PE timestamps; never interpret arbitrary file data as text."""
    result = {"sha256": digest(data), "size": len(data)}
    if len(data) < 64 or data[:2] != b"MZ":
        raise ValueError("proc-macro output is not a PE file")
    pe = struct.unpack_from("<I", data, 60)[0]
    if pe + 24 > len(data) or data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("invalid PE header")
    count = struct.unpack_from("<H", data, pe + 6)[0]
    result["coff_timestamp"] = struct.unpack_from("<I", data, pe + 8)[0]
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    table = pe + 24 + optional_size
    if table + count * 40 > len(data):
        raise ValueError("truncated PE section table")
    sections = []
    for index in range(count):
        offset = table + index * 40
        name = data[offset:offset + 8].rstrip(b"\0").decode("ascii", errors="replace")
        size, pointer = struct.unpack_from("<II", data, offset + 16)
        if pointer + size > len(data):
            raise ValueError("truncated PE section")
        sections.append({"name": name, "size": size, "sha256": digest(data[pointer:pointer + size])})
    result["sections"] = sections
    return result


def summarize_log(text: str, roots: list[Path], *, collect_dlls: bool = True) -> dict:
    """The logger identifies crates, not requests: retain variants without guessing pairings."""
    crates: dict[str, dict] = {}
    dlls: dict[str, dict] = {}
    malformed = 0
    errors: Counter[str] = Counter()
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    for match in CACHE_EVENT.finditer(text):
        name, event, body = match.groups()
        row = crates.setdefault(name, {"crate": name, "hits": 0, "misses": 0, "keys": set(), "commands": {}, "compile_seconds": 0.0})
        if event == "get_cached_or_compile: ":
            try:
                arguments = decode_arguments(body)
                if "--crate-name" not in arguments:
                    continue
                if arguments[arguments.index("--crate-name") + 1] != name:
                    raise ValueError("crate name differs")
                fingerprint = digest(json.dumps(arguments, ensure_ascii=True).encode())
                externs = []
                for index, argument in enumerate(arguments[:-1]):
                    if argument != "--extern":
                        continue
                    _, separator, filename = arguments[index + 1].partition("=")
                    path = Path(filename)
                    if not collect_dlls or not separator or path.suffix.lower() != ".dll":
                        continue
                    path = path.resolve(strict=True)
                    if not any(path.is_relative_to(root) for root in roots):
                        raise ValueError("extern DLL outside isolated build roots")
                    key = str(path)
                    if key not in dlls:
                        dlls[key] = pe_identity(path.read_bytes())
                    externs.append(key)
                row["commands"][fingerprint] = {"arguments_sha256": fingerprint, "proc_macro_dlls": sorted(set(externs))}
            except (ValueError, IndexError, OSError, struct.error) as error:
                malformed += 1
                # JSON errors omit the input itself; OS errors omit the potentially sensitive path.
                reason = error.msg if isinstance(error, json.JSONDecodeError) else error.strerror if isinstance(error, OSError) else str(error)
                errors[f"{type(error).__name__}: {reason}"] += 1
        elif event == "Hash key: " and re.fullmatch(r"[0-9a-f]{64}", body):
            row["keys"].add(body)
        elif event == "Cache hit in ":
            row["hits"] += 1
        elif event == "Cache miss in ":
            row["misses"] += 1
        elif event == "Compiled in ":
            duration = re.fullmatch(r"([0-9.]+) s, storing in cache", body)
            if duration:
                row["compile_seconds"] += float(duration[1])
    rows = []
    for row in crates.values():
        if not row["commands"]:
            continue
        row["commands"] = list(row["commands"].values())
        row["keys"] = sorted(row["keys"])
        row["compile_seconds"] = round(row["compile_seconds"], 3)
        rows.append(row)
    return {"crates": sorted(rows, key=lambda row: row["compile_seconds"], reverse=True), "proc_macro_dlls": dlls, "unparsed_invocations": malformed, "parse_errors": dict(errors), "dll_capture": collect_dlls}


def read_log(path: Path, offset: int = 0) -> str:
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise ValueError("compiler log exceeds diagnostic limit")
    return data.decode("utf-8", errors="replace")


def probe_result(passes: list[dict]) -> dict:
    if len(passes) != 2:
        return {"status": "incomplete"}
    first, second = passes
    if first["inputs_sha256"] != second["inputs_sha256"]:
        return {"status": "probe_inputs_changed"}
    same_dll = first["dll"]["sha256"] == second["dll"]["sha256"]
    consumer = next((row for row in second["cache"]["crates"] if row["crate"] == CONSUMER), None)
    if not consumer:
        return {"status": "incomplete", "reason": "consumer cache event missing"}
    first_consumer = next((row for row in first["cache"]["crates"] if row["crate"] == CONSUMER), None)
    if not first_consumer:
        return {"status": "incomplete", "reason": "first consumer cache event missing"}
    if any(sample["cache"].get("unparsed_invocations", 0) for sample in passes) or any(row["hits"] + row["misses"] != 1 for row in (first_consumer, consumer)):
        return {"status": "incomplete", "reason": "ambiguous consumer cache events"}
    if {row["arguments_sha256"] for row in first_consumer["commands"]} != {row["arguments_sha256"] for row in consumer["commands"]}:
        return {"status": "probe_arguments_changed"}
    changed_sections = [a["name"] for a, b in zip(first["dll"]["sections"], second["dll"]["sections"]) if a != b]
    if first["dll"]["sections"] != second["dll"]["sections"] and not changed_sections:
        changed_sections = ["section table changed"]
    if not same_dll and consumer["misses"]:
        status = "proc_macro_instability_reproduced"
    elif same_dll and consumer["hits"] and not consumer["misses"]:
        status = "minimal_probe_stable"
    elif same_dll and consumer["misses"]:
        status = "other_cache_input_difference"
    else:
        status = "incomplete"
    return {"status": status, "same_dll": same_dll, "coff_timestamp_changed": first["dll"]["coff_timestamp"] != second["dll"]["coff_timestamp"], "changed_sections": changed_sections, "consumer_hits": consumer["hits"], "consumer_misses": consumer["misses"]}


def run_probe(source: Path, output: Path, log: Path) -> dict:
    if sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("the Rust probe must run in the Windows GitHub build shard")
    runner_temp = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    probe = runner_temp / "c" / "cache-probe"
    # A fresh, fixed short path preserves the command/path identity between passes.
    probe.mkdir(parents=True, exist_ok=False)
    target = probe / "target"
    manifest = (source / "codex-rs/Cargo.toml").read_text(encoding="utf-8")
    profile = re.search(r"(?ms)^\[profile\.release\]\n.*?(?=^\[|\Z)", manifest)
    if not profile:
        raise ValueError("exact upstream release profile missing")
    files = {
        "Cargo.toml": '[workspace]\nmembers = ["macro", "consumer"]\nresolver = "2"\n\n' + profile[0],
        "macro/Cargo.toml": '[package]\nname = "csa-cache-probe-macro"\nversion = "0.0.0"\nedition = "2021"\n[lib]\nproc-macro = true\n',
        "macro/src/lib.rs": 'extern crate proc_macro;\n#[proc_macro]\npub fn passthrough(input: proc_macro::TokenStream) -> proc_macro::TokenStream { input }\n',
        "consumer/Cargo.toml": '[package]\nname = "csa-cache-probe-consumer"\nversion = "0.0.0"\nedition = "2021"\n[dependencies]\ncsa-cache-probe-macro = { path = "../macro" }\n',
        "consumer/src/lib.rs": 'csa_cache_probe_macro::passthrough! { pub fn value() -> u64 { 42 } }\n',
        ".cargo/config.toml": (source / "codex-rs/.cargo/config.toml").read_text(encoding="utf-8"),
        "rust-toolchain.toml": (source / "codex-rs/rust-toolchain.toml").read_text(encoding="utf-8"),
    }
    for name, content in files.items():
        path = probe / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    command = ["cargo", "build", "--package", "csa-cache-probe-consumer", "--offline", "--release", "--timings", "--target", TARGET, "--target-dir", str(target)]
    passes = []
    for number in (1, 2):
        if number == 2:
            resolved = target.resolve(strict=True)
            if resolved != runner_temp / "c/cache-probe/target" or resolved.parent != probe.resolve(strict=True):
                raise ValueError("probe cleanup escaped its isolated target directory")
            shutil.rmtree(resolved)
        offset = log.stat().st_size
        completed = subprocess.run(command, cwd=probe, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        (output / f"probe-{number}.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
        events = read_log(log, offset)
        write_events(output / f"probe-{number}-cache-events.log", events)
        if completed.returncode:
            return {"status": "incomplete", "failed_pass": number, "exit_code": completed.returncode, "passes": passes}
        dlls = list((target / "release/deps").glob("csa_cache_probe_macro-*.dll"))
        if len(dlls) != 1:
            raise ValueError("expected one probe proc-macro DLL")
        data = dlls[0].read_bytes()
        (output / f"probe-{number}.dll").write_bytes(data)
        inputs = {name: digest((probe / name).read_bytes()) for name in (*files, "Cargo.lock")}
        for name in ("SOURCE_DATE_EPOCH", "CARGO_INCREMENTAL", "CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER", "RUSTC_WRAPPER", "RUSTUP_TOOLCHAIN", "CC", "CXX", "LIB", "INCLUDE", "PATH"):
            inputs["env:" + name] = digest(os.environ.get(name, "").encode())
        passes.append({"inputs_sha256": digest(json.dumps(inputs, sort_keys=True).encode()), "input_fingerprints": inputs, "dll": pe_identity(data), "cache": summarize_log(events, [probe.resolve(strict=True)])})
    return {**probe_result(passes), "passes": passes}


def write_report(output: Path, report: dict) -> None:
    (output / "cache-diagnosis.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    native = report.get("native", {})
    lines = ["### Windows x64 compiler cache diagnosis", "", f"- Diagnostic status: `{report['status']}`", f"- Reproducibility probe: `{report.get('probe', {}).get('status', 'not run')}`", f"- Unparsed invocations: {native.get('unparsed_invocations', 0)}", "", "| Rust crate | Hits | Misses | Cache-miss compilation seconds (cumulative) |", "| --- | ---: | ---: | ---: |"]
    for row in native.get("crates", [])[:25]:
        if row["misses"]:
            lines.append(f"| `{row['crate']}` | {row['hits']} | {row['misses']} | {row['compile_seconds']:.3f} |")
    if report.get("replay"):
        lines += ["", "Events-only replay: no compiler was run and no DLL files were read. DLL reproducibility is not verified by this report."]
    for reason, count in native.get("parse_errors", {}).items():
        lines += ["", f"Parse failure ({count} invocations): {reason}"]
    lines += ["", "The JSON report contains per-crate keys, argument fingerprints and native proc-macro DLL section hashes. Concurrent invocations of the same crate are grouped; keys are not guessed to belong to particular argument variants.", "", "The small probe uses the reviewed release profile and Windows configuration. It rebuilds only its own target directory with identical source/path settings. A stable probe does not establish that every Codex proc macro or cache input is stable. A reproduced difference identifies a mechanism, not how many native misses it caused."]
    probe = report.get("probe", {})
    explanations = {
        "proc_macro_instability_reproduced": "Identical tracked inputs produced different proc-macro DLL bytes, followed by a consumer cache miss. Proc-macro-dependent cache invalidation was reproduced.",
        "minimal_probe_stable": "The repeated DLL was byte-identical and the consumer hit the cache. This small case did not reproduce the native misses; compare the recorded native input fingerprints across builds.",
        "other_cache_input_difference": "The DLL was byte-identical but the consumer still missed. The cache-key difference needs investigation beyond this DLL.",
        "probe_inputs_changed": "The probe's source/configuration/environment fingerprints changed. Do not attribute the result to nondeterministic DLL output.",
        "probe_arguments_changed": "The probe's Rust compiler arguments changed. Inspect the command fingerprints before attributing the result to DLL output.",
        "incomplete": "The probe did not provide enough consistent evidence to identify a cause.",
    }
    if probe:
        lines += ["", explanations.get(probe["status"], "Probe was not completed.")]
        if probe.get("reason"):
            lines += ["", f"Probe detail: {probe['reason']}"]
        if probe.get("error"):
            lines += ["", f"Probe error: {probe['error']}"]
        if "same_dll" in probe:
            lines += [f"- DLL byte identity: {probe['same_dll']}", f"- COFF timestamp changed: {probe['coff_timestamp_changed']}", f"- Changed PE sections: {', '.join(probe['changed_sections']) or 'none'}", f"- Second consumer: {probe['consumer_hits']} hits / {probe['consumer_misses']} misses"]
    if "error" in report:
        lines += ["", f"Diagnostic error: {report['error']}"]
    text = "\n".join(lines) + "\n"
    (output / "cache-diagnosis.md").write_text(text, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--target-dir", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--probe", action="store_true")
    mode.add_argument("--replay", action="store_true", help="replay saved cache events without accessing build roots or running a compiler")
    args = parser.parse_args()
    if not args.replay and (args.source_root is None or args.target_dir is None):
        parser.error("--source-root and --target-dir are required unless --replay is used")
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"schema": 1, "status": "incomplete", "source_commit": os.environ.get("SOURCE_COMMIT"), "run_id": os.environ.get("GITHUB_RUN_ID"), "source_date_epoch": os.environ.get("SOURCE_DATE_EPOCH")}
    native_ok = False
    try:
        events = read_log(args.log)
        write_events(args.output / "native-cache-events.log", events)
        roots = [] if args.replay else [args.source_root.resolve(strict=True), args.target_dir.resolve(strict=True), Path(os.environ["CARGO_HOME"]).resolve(strict=True)]
        report["replay"] = args.replay
        report["native"] = summarize_log(events, roots, collect_dlls=not args.replay)
        if not report["native"]["crates"]:
            raise ValueError("native Rust cache events missing")
        native_ok = not report["native"]["unparsed_invocations"]
    except (OSError, ValueError, KeyError, struct.error, subprocess.SubprocessError) as error:
        report["error"] = str(error)
    # Native log failure must not suppress the independent, small reproducibility experiment.
    if args.probe:
        try:
            report["probe"] = run_probe(args.source_root, args.output, args.log)
        except (OSError, ValueError, KeyError, struct.error, subprocess.SubprocessError) as error:
            report["probe"] = {"status": "incomplete", "error": str(error)}
    report["status"] = "collected" if native_ok and report.get("probe", {}).get("status") != "incomplete" else "incomplete"
    write_report(args.output, report)
    print(json.dumps({"status": report["status"], "probe": report.get("probe", {}).get("status")}))
    return 0 if report["status"] == "collected" else 2


if __name__ == "__main__":
    raise SystemExit(main())
