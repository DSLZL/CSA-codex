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

from diagnose_compiler_cache import CONSUMER, pe_identity, probe_result, run_probe, summarize_log


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
                return subprocess.CompletedProcess(command, 0, "probe output", "")

            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_TEMP": str(root)}), patch("diagnose_compiler_cache.sys.platform", "win32"), patch("diagnose_compiler_cache.subprocess.run", side_effect=cargo):
                report = run_probe(source, output, log)
            self.assertEqual(len(calls), 2)
            self.assertEqual(report["status"], "proc_macro_instability_reproduced")
            self.assertEqual(native.read_bytes(), b"native output")
            self.assertNotEqual((output / "probe-1.dll").read_bytes(), (output / "probe-2.dll").read_bytes())


if __name__ == "__main__":
    unittest.main()
