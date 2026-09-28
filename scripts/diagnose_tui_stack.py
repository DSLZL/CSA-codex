"""Opt-in Windows stack diagnostics; these runs cannot certify a payload contract."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import subprocess

from run_patch_contract import environment, load_contract
from verify_patch_payload import _load_payload, _payload_file, verify


TEST = "command_center_attach_conflict_opens_read_only_and_retries"
QUALIFIED_TEST = f"app::agents_overview::tests::{TEST}"
FIXTURE = "codex-rs/tui/src/app/agents_overview_tests.rs"
START = f"#[tokio::test]\nasync fn {TEST}() -> Result<()> {{\n"
END = "\n#[tokio::test]\nasync fn command_center_refresh_failure_is_inline_and_clears_on_success()"
MARKER = "[csa-stack]"
STAGES = (
    ("make-app", "    let mut app = Box::pin(make_test_app()).await;"),
    ("start-owner", "    let mut owner = Box::pin(crate::start_embedded_app_server_for_picker(&app.config)).await?;"),
    ("resume-owner", "    Box::pin(owner.resume_thread(\n        &app.local_settings,\n        app.config.clone(),\n        thread_id,\n        crate::app_server_session::ResumeModelSettings::PreserveExistingThread,\n    ))\n    .await?;"),
    ("read-owner", "    let thread = owner\n        .thread_read(thread_id, /*include_turns*/ false)\n        .await?;"),
    ("start-server", "    let mut server = Box::pin(crate::start_embedded_app_server_for_picker(&app.config)).await?;"),
    ("select", "    Box::pin(app.handle_event(&mut tui, &mut server, event)).await?;"),
    ("escape", "    app.handle_key_event(&mut tui, &mut server, KeyCode::Esc.into())\n        .await;"),
    ("reselect", "    Box::pin(app.handle_event(\n        &mut tui,\n        &mut server,\n        AppEvent::SelectAgentsOverviewThread { thread_id },\n    ))\n    .await?;"),
    ("retry", "    Box::pin(app.handle_key_event(&mut tui, &mut server, KeyCode::Char('r').into())).await;"),
    ("stop-owner", "    owner.shutdown().await?;"),
    ("stop-server", "    server.shutdown().await?;"),
)
TUI_READY = "    let mut tui = crate::tui::test_support::make_test_tui()?;"


def instrument_fixture(source: str) -> str:
    """Add probes only to the pinned scenario, preserving its statements/assertions."""
    if MARKER in source or source.count(START) != 1 or source.count(END) != 1:
        raise ValueError("unexpected or already instrumented TUI fixture")
    start = source.index(START)
    end = source.index(END, start)
    body = source[start + len(START):end]
    for label, statement in STAGES:
        expected = 2 if label == "retry" else 1
        if body.count(statement) != expected:
            raise ValueError(f"unexpected {label} stage in TUI fixture")
        parts = body.split(statement)
        body = parts[0]
        for index, part in enumerate(parts[1:], 1):
            body += (
                f'    eprintln!("{MARKER} before-{label}-{index}");\n'
                f"{statement}\n"
                f'    eprintln!("{MARKER} after-{label}-{index}");' + part
            )
    if body.count(TUI_READY) != 1:
        raise ValueError("unexpected TUI setup in fixture")
    probes = [
        '    eprintln!("[csa-stack] app_bytes={} widget_bytes={} config_bytes={}", '
        'std::mem::size_of_val(&app), std::mem::size_of_val(&app.chat_widget), '
        'std::mem::size_of_val(&app.config));',
    ]
    for label, call in (
        ("key", "app.handle_key_event(tui, server, KeyCode::Esc.into())"),
        ("event", "app.handle_event(tui, server, AppEvent::SelectAgentsOverviewThread { thread_id })"),
        ("selection", "app.select_agents_overview_thread(tui, server, thread_id)"),
        ("resume", "app.resume_target_session(tui, server, SessionTarget { path: None, thread_id, cwd: None, history_mode: None })"),
    ):
        # Consume the mutable references, so the factory is FnOnce. Never call it.
        thread_binding = "_" if label == "key" else "thread_id"
        probes.append(
            "    let args = (&mut app, &mut tui, &mut server, thread_id);\n"
            f'    eprintln!("{MARKER} {label}_future_bytes={{}}", csa_stack_future_size(move || {{\n'
            f"        let (app, tui, server, {thread_binding}) = args;\n"
            f"        {call}\n"
            "    }));"
        )
    body = body.replace(TUI_READY, TUI_READY + "\n" + "\n".join(probes))
    wrapper = f'''fn csa_stack_future_size<F: std::future::Future>(_: impl FnOnce() -> F) -> usize {{
    std::mem::size_of::<F>()
}}

#[inline(never)]
fn csa_stack_box_scenario() -> std::pin::Pin<Box<impl std::future::Future<Output = Result<()>>>> {{
    Box::pin(csa_stack_scenario())
}}

{START}    eprintln!("{MARKER} scenario_future_bytes={{}}", csa_stack_future_size(csa_stack_scenario));
    eprintln!("{MARKER} before-scenario-construction");
    csa_stack_box_scenario().await
}}

async fn csa_stack_scenario() -> Result<()> {{
    eprintln!("{MARKER} entered-scenario");
'''
    return source[:start] + wrapper + body + source[end:]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def diagnose(manifest: Path, source: Path, evidence: Path, variant: str) -> int:
    evidence.mkdir(parents=True, exist_ok=False)
    payload = _load_payload(manifest)
    if payload.manifest["compat_id"] != "rust-v0.157.0-native-join-p16":
        raise ValueError("stack probe is pinned to the reviewed v0.157.0 scenario")
    contract = load_contract(_payload_file(payload, "test-contract.json"), str(payload.manifest["compat_id"]))
    verification = verify(manifest, source, apply=variant == "candidate", artifact_path=None)
    report = {
        "schema": 1,
        "diagnostic_only": True,
        "contract_eligible": False,
        "producer_commit": os.environ["GITHUB_SHA"],
        "upstream_commit": payload.manifest["upstream_commit"],
        "manifest_sha256": digest(manifest.read_bytes()),
        "variant": variant,
        "verification": verification,
        "test": QUALIFIED_TEST,
        "runs": [],
    }
    cargo_target = evidence.parent / "tests"
    cwd = source / "codex-rs"

    def run(name: str, argv: list[str], env: dict[str, str]) -> int:
        print(f"Stack diagnostic: {variant}/{name}", flush=True)
        log = evidence / f"{name}.log"
        with log.open("xb") as output:
            result = subprocess.run(argv, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, check=False)
        raw = log.read_bytes()
        report["runs"].append({
            "name": name, "argv": argv, "stack_bytes": int(env["RUST_MIN_STACK"]),
            "exit_code": result.returncode, "log": log.name, "log_sha256": digest(raw),
        })
        print(raw.decode("utf-8", "replace")[-12000:], flush=True)
        print(f"Stack diagnostic: {name} exit={result.returncode}", flush=True)
        return result.returncode

    try:
        for index, step in enumerate(contract["generation"], 1):
            env = environment(contract["common_env"], step.get("env", {}), source, cargo_target)
            if run(f"generation-{index}", step["argv"], env):
                return 1
        tui = next(step for step in contract["tests"] if step["name"] == "complete TUI library")
        env = environment(contract["common_env"], tui.get("env", {}), source, cargo_target)
        env["RUST_MIN_STACK"] = "8388608"
        command = ["cargo", "test", "-p", "codex-tui", "--lib", QUALIFIED_TEST, "--", "--exact"]
        if run("list", command + ["--list"], env):
            return 1
        if f"{QUALIFIED_TEST}: test" not in (evidence / "list.log").read_text(encoding="utf-8", errors="replace").splitlines():
            raise ValueError("selected stack diagnostic test was not discovered")
        command += ["--test-threads=1", "--nocapture"]
        run("original-8mib", command, env)
        fixture = source / FIXTURE
        before = fixture.read_bytes()
        after = instrument_fixture(before.decode("utf-8")).encode("utf-8")
        report["fixture"] = {"path": FIXTURE, "before_sha256": digest(before), "after_sha256": digest(after)}
        (evidence / "instrumentation.patch").write_text("".join(difflib.unified_diff(
            before.decode("utf-8").splitlines(keepends=True), after.decode("utf-8").splitlines(keepends=True),
            fromfile="a/" + FIXTURE, tofile="b/" + FIXTURE,
        )), encoding="utf-8")
        fixture.write_bytes(after)
        try:
            run("instrumented-8mib", command, env)
            env["RUST_MIN_STACK"] = "33554432"
            run("instrumented-32mib", command, env)
        finally:
            fixture.write_bytes(before)
        return int(any(item["exit_code"] for item in report["runs"]))
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        (evidence / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--variant", choices=("upstream", "candidate"), required=True)
    args = parser.parse_args()
    return diagnose(args.manifest.resolve(), args.source.resolve(), args.evidence.resolve(), args.variant)


if __name__ == "__main__":
    raise SystemExit(main())
