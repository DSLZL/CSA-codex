#!/usr/bin/env python3
"""Offline checks for cache evidence parsing; no Rust compiler is executed."""

import json
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from diagnose_compiler_cache import CONSUMER, decode_arguments, main, pe_identity, probe_result, run_probe, summarize_log


def pe(timestamp=123, payload=b"code"):
    data = bytearray(256 + len(payload))
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    data[128:132] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", data, 132, 0x8664, 1, timestamp, 0, 0, 0, 0)
    data[152:160] = b".text\0\0\0"
    struct.pack_into("<II", data, 168, len(payload), 256)
    data[256:] = payload
    return bytes(data)


class DiagnosticsTests(unittest.TestCase):
    def test_rust_debug_escapes_and_literal_windows_paths(self):
        # Rust Debug's Unicode escape was reproduced with the official Windows sccache 0.16.0 binary.
        body = r'["--crate-name", "demo", "src\u{200e}.rs", "D:\\u{200e}\\source.rs", "--cfg", "feature=\"default\""]'
        arguments = decode_arguments(body)
        self.assertEqual(arguments[2], "src\u200e.rs")
        self.assertEqual(arguments[3], r"D:\u{200e}\source.rs")
        self.assertEqual(arguments[-1], 'feature="default"')
        self.assertEqual(decode_arguments(r'["\0", "\\0", "\u{1f642}"]'), ["\0", r"\0", "\U0001f642"])
        log = '\x1b[36m[demo]: get_cached_or_compile: ' + body + '\x1b[0m\r\n[demo]: Cache hit in 0.001 s\r\n'
        report = summarize_log(log, [])
        self.assertEqual(report["unparsed_invocations"], 0)
        self.assertEqual(report["crates"][0]["hits"], 1)
        for invalid in (r'["\u{110000}"]', '["unterminated]', '{"not": "arguments"}'):
            with self.assertRaises(ValueError):
                decode_arguments(invalid)

    def test_groups_overlapping_crate_requests_without_inventing_key_pairings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            dll = root / "derive.dll"
            dll.write_bytes(pe())
            arguments = ["--crate-name", "codex_core", "src/lib.rs", "--extern", f"derive={dll}"]
            messages = [
                "get_cached_or_compile: " + json.dumps(arguments),
                "get_cached_or_compile: " + json.dumps([*arguments, "--cfg", 'feature="other"']),
                "Hash key: " + "a" * 64,
                "Hash key: " + "b" * 64,
                "Cache miss in 0.000 s",
                "Cache hit in 0.001 s",
                "Compiled in 12.345 s, storing in cache",
            ]
            log = "\n".join("[DEBUG sccache::compiler::compiler] [codex_core]: " + message for message in messages)
            log += '\n[DEBUG] unrelated environment SECRET=do-not-publish\n'
            report = summarize_log(log, [root])
            row = report["crates"][0]
            self.assertEqual((row["hits"], row["misses"], row["compile_seconds"]), (1, 1, 12.345))
            self.assertEqual(len(row["commands"]), 2)
            self.assertEqual(row["keys"], ["a" * 64, "b" * 64])
            self.assertEqual(report["proc_macro_dlls"][str(dll)]["coff_timestamp"], 123)
            self.assertNotIn("do-not-publish", json.dumps(report))
            rejected = summarize_log(log, [root / "unrelated"])
            self.assertEqual(rejected["unparsed_invocations"], 2)
            self.assertFalse(rejected["proc_macro_dlls"])

    def test_distinguishes_reproduced_instability_from_stable_or_incomplete_probe(self):
        def sample(timestamp, hit=0, miss=1, inputs="same", arguments="same"):
            return {"inputs_sha256": inputs, "dll": pe_identity(pe(timestamp)), "cache": {"crates": [{"crate": CONSUMER, "hits": hit, "misses": miss, "commands": [{"arguments_sha256": arguments}]}]}}
        first = sample(123)
        changed = probe_result([first, sample(456)])
        self.assertEqual(changed["status"], "proc_macro_instability_reproduced")
        self.assertTrue(changed["coff_timestamp_changed"])
        self.assertEqual(changed["changed_sections"], [])
        self.assertEqual(probe_result([first, sample(123, hit=1, miss=0)])["status"], "minimal_probe_stable")
        self.assertEqual(probe_result([first, sample(123)])["status"], "other_cache_input_difference")
        self.assertEqual(probe_result([first, sample(456, inputs="different")])["status"], "probe_inputs_changed")
        self.assertEqual(probe_result([first, sample(456, arguments="different")])["status"], "probe_arguments_changed")
        self.assertEqual(probe_result([first])["status"], "incomplete")

    def test_rejects_truncated_pe_evidence(self):
        for invalid in (b"not PE", pe()[:150], pe()[:-1]):
            with self.assertRaises(ValueError):
                pe_identity(invalid)
        report = summarize_log('[crate]: get_cached_or_compile: [invalid', [])
        self.assertEqual(report["unparsed_invocations"], 1)
        self.assertTrue(report["parse_errors"])

    def test_native_failure_keeps_events_and_still_runs_the_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            log = root / "debug.log"
            log.write_text('[broken]: get_cached_or_compile: [invalid\n[DEBUG server] SECRET=do-not-publish\n')
            output = root / "report"
            arguments = ["diagnose", "--log", str(log), "--source-root", str(root), "--target-dir", str(root), "--output", str(output), "--probe"]
            with patch.dict(os.environ, {"CARGO_HOME": str(root), "GITHUB_STEP_SUMMARY": str(root / "summary.md")}), patch("sys.argv", arguments), patch("diagnose_compiler_cache.run_probe", return_value={"status": "minimal_probe_stable"}) as probe:
                self.assertEqual(main(), 2)
            probe.assert_called_once()
            report = json.loads((output / "cache-diagnosis.json").read_text())
            self.assertEqual(report["status"], "incomplete")
            self.assertEqual(report["probe"]["status"], "minimal_probe_stable")
            self.assertTrue(report["native"]["parse_errors"])
            events = (output / "native-cache-events.log").read_text()
            self.assertIn("[invalid", events)
            self.assertNotIn("do-not-publish", events)
            self.assertNotIn("do-not-publish", json.dumps(report))

    def test_replay_needs_no_build_roots_dll_files_or_compiler(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "events.log"
            body = json.dumps(["--crate-name", "demo", "--extern", "derive=Z:/unavailable/derive.dll"])
            log.write_text(f'[demo]: get_cached_or_compile: {body}\n[demo]: Cache miss in 0.001 s\n')
            output = root / "replay"
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}), patch("sys.argv", ["diagnose", "--replay", "--log", str(log), "--output", str(output)]), patch("diagnose_compiler_cache.subprocess.run") as compiler, patch("diagnose_compiler_cache.pe_identity") as dll:
                self.assertEqual(main(), 0)
            compiler.assert_not_called()
            dll.assert_not_called()
            report = json.loads((output / "cache-diagnosis.json").read_text())
            self.assertTrue(report["replay"])
            self.assertFalse(report["native"]["dll_capture"])
            self.assertEqual(report["native"]["crates"][0]["misses"], 1)

    def test_probe_cleans_only_its_own_outputs_and_keeps_the_exact_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source"
            (source / "codex-rs/.cargo").mkdir(parents=True)
            profile = '[profile.release]\nlto = "thin"\ncodegen-units = 4\n'
            (source / "codex-rs/Cargo.toml").write_text('[workspace]\n\n' + profile)
            (source / "codex-rs/.cargo/config.toml").write_text('[build]\njobs = 4\n')
            (source / "codex-rs/rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.95.0"\n')
            native = root / "c/t/native.exe"
            native.parent.mkdir(parents=True)
            native.write_bytes(b"native output")
            output = root / "report"
            output.mkdir()
            log = root / "debug.log"
            log.write_text("")
            calls = []
            failing = False

            def cargo(command, *, cwd, **kwargs):
                calls.append(command)
                target = Path(command[command.index("--target-dir") + 1])
                self.assertFalse(target.exists())
                self.assertEqual(command[command.index("--target") + 1], "x86_64-pc-windows-msvc")
                self.assertIn(profile, (cwd / "Cargo.toml").read_text())
                (cwd / "Cargo.lock").write_text("version = 4\n")
                dll = target / "release/deps/csa_cache_probe_macro-abc.dll"
                dll.parent.mkdir(parents=True)
                dll.write_bytes(pe(100 + len(calls)))
                arguments = ["--crate-name", CONSUMER, "--extern", f"probe={dll}"]
                with log.open("a") as stream:
                    stream.write(f'[{CONSUMER}]: get_cached_or_compile: {json.dumps(arguments)}\n')
                    stream.write(f'[{CONSUMER}]: Cache miss in 0.000 s\n')
                return subprocess.CompletedProcess(command, 1 if failing else 0, "probe output", "")

            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_TEMP": str(root)}), patch("diagnose_compiler_cache.sys.platform", "win32"), patch("diagnose_compiler_cache.subprocess.run", side_effect=cargo):
                report = run_probe(source, output, log)
            self.assertEqual(len(calls), 2)
            self.assertEqual(report["status"], "proc_macro_instability_reproduced")
            self.assertEqual(native.read_bytes(), b"native output")
            self.assertNotEqual((output / "probe-1.dll").read_bytes(), (output / "probe-2.dll").read_bytes())
            self.assertIn(CONSUMER, (output / "probe-1-cache-events.log").read_text())

            failing = True
            retry_root = root / "failed-run"
            retry_root.mkdir()
            failure_output = root / "failure-report"
            failure_output.mkdir()
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_TEMP": str(retry_root)}), patch("diagnose_compiler_cache.sys.platform", "win32"), patch("diagnose_compiler_cache.subprocess.run", side_effect=cargo):
                failed = run_probe(source, failure_output, log)
            self.assertEqual(failed["status"], "incomplete")
            self.assertEqual(failed["failed_pass"], 1)
            self.assertIn(CONSUMER, (failure_output / "probe-1-cache-events.log").read_text())


if __name__ == "__main__":
    unittest.main()
