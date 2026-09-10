#!/usr/bin/env python3
"""Run pinned official/candidate executables against isolated native histories.

This command never builds, polls GitHub, accepts a candidate or repairs history.
"""
from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import hashlib
import http.server
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import tomllib
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import compat_catalog as catalog

TARGET = "x86_64-pc-windows-msvc"
MODEL = "test-gpt-5.1-codex"
SOURCES = ["cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview", "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown"]


class ValidationError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def strict_json(data: str | bytes) -> Any:
    def constant(value: str) -> None:
        raise ValidationError(f"non-JSON numeric constant: {value}")

    try:
        return json.loads(data, object_pairs_hook=catalog._reject_duplicate_keys, parse_constant=constant)
    except catalog.CatalogError as error:
        raise ValidationError(str(error)) from error


def file_record(path: Path) -> dict[str, Any]:
    return {"sha256": catalog.sha256_file(path), "size": path.stat().st_size}


def binary_identity(path: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
    result = subprocess.run([str(path), "--version"], env=env, capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
    require(result.returncode == 0, f"absolute executable version check failed: {path}")
    return {**file_record(path), "version": result.stdout.strip()}


def timeline_key(row: dict[str, Any]) -> tuple[int, int, str]:
    # Native timeline cursors page backwards, with stable ties at one rollout ordinal.
    kind = {"turnStarted": 0, "item": 1, "realtime": 2, "turnCompleted": 3}[row["type"]]
    return row["position"], kind, row["item"]["id"] if kind in (1, 2) else row["turnId"]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def fresh_root(path: Path, allowed_parent: Path) -> Path:
    require(path.is_absolute(), "run root must be absolute")
    require(not path.exists() and not path.is_symlink(), "run root must not already exist")
    require(path.parent.resolve() == allowed_parent.resolve(), "run root must be a direct child of the isolated validation directory")
    path.mkdir(parents=True, exist_ok=False)
    return path.resolve()


def read_canonical(home: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for directory in (home / "sessions", home / "archived_sessions"):
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or not (path.name.endswith(".jsonl") or path.name.endswith(".jsonl.zst")):
                continue
            require(not path.is_symlink() and path.resolve().is_relative_to(home.resolve()), "canonical path escapes the fixture")
            data = path.read_bytes()
            if path.name.endswith(".zst"):
                from compression import zstd

                data = zstd.decompress(data)
            lines = data.decode("utf-8", errors="strict").splitlines()
            require(bool(lines) and all(line.strip() for line in lines), f"empty canonical record in {path}")
            rows = [strict_json(line) for line in lines]
            require(all(isinstance(row, dict) and isinstance(row.get("type"), str) for row in rows), f"invalid canonical record in {path}")
            result[path.relative_to(home).as_posix()] = {**file_record(path), "count": len(rows), "rows": rows}
    require(bool(result), "native canonical history is missing")
    return result


def assert_canonical_preserved(before: dict[str, Any], after: dict[str, Any]) -> None:
    require(set(before) <= set(after), "canonical history disappeared")
    for path, previous in before.items():
        current = after[path]
        require(current["rows"][:previous["count"]] == previous["rows"], f"official resume changed prior canonical records: {path}")
        if current["count"] == previous["count"]:
            require(current["sha256"] == previous["sha256"], f"official read rewrote canonical bytes: {path}")


def verify_persisted_joins(canonical: dict[str, Any], parent: str, joins: list[dict[str, Any]], wait_ids: set[str] | None = None) -> list[dict[str, Any]]:
    histories = [entry["rows"] for entry in canonical.values() if entry["rows"][0].get("type") == "session_meta" and entry["rows"][0].get("payload", {}).get("id") == parent]
    require(len(histories) == 1, "expected exactly one canonical parent rollout")
    rows = histories[0]
    results = []
    joins_by_id = {join["join_call_id"]: join for join in joins}
    wait_ids = wait_ids or set()
    require(len(joins_by_id) == len(joins) and not (set(joins_by_id) & wait_ids), "native wait ledger repeats a call ID")
    for identifier in [*joins_by_id, *sorted(wait_ids)]:
        calls = [row["payload"] for row in rows if row["type"] == "response_item" and row["payload"].get("type") == "function_call" and row["payload"].get("call_id") == identifier]
        outputs = [row["payload"] for row in rows if row["type"] == "response_item" and row["payload"].get("type") == "function_call_output" and row["payload"].get("call_id") == identifier]
        waits = [row["payload"]["item"] for row in rows if row["type"] == "event_msg" and row["payload"].get("type") == "item_completed" and row["payload"].get("item", {}).get("id") == identifier]
        require(len(calls) == len(outputs) == len(waits) == 1, "canonical Join call/result/Wait is missing or duplicated")
        wait = waits[0]
        require(wait.get("type") == "CollabAgentToolCall" and wait.get("tool") == "wait" and wait.get("status") == "completed" and wait.get("sender_thread_id") == parent, "canonical Wait changed its native identity or completion")
        if identifier in wait_ids:
            require(calls[0]["name"] == "wait_agent" and strict_json(calls[0]["arguments"]) == {"timeout_ms": 1000} and wait.get("receiver_thread_ids") == [] and isinstance(outputs[0].get("output"), str), "canonical event wait differs from its actual fixture call")
            results.append({"call": calls[0], "result": outputs[0], "wait": wait})
            continue
        join = joins_by_id[identifier]
        runs, children, terminal = join["runs"], join["thread_ids"], join["terminal"]
        require(len(runs) == len(children) == len(terminal) and bool(runs), "canonical Join ledger has inconsistent run counts")
        batch = len(runs) > 1
        expected_arguments = {"runs": list(reversed(runs))} if batch else runs[0]
        expected_result = {"results": terminal} if batch else terminal[0]
        require(calls[0]["name"] == ("join_agents" if batch else "join_agent") and strict_json(calls[0]["arguments"]) == expected_arguments, "canonical Join changed its tool, exact runs or ordering")
        require(strict_json(outputs[0]["output"]) == expected_result, "canonical Join changed its complete terminal result")
        require(wait.get("receiver_thread_ids") == (list(reversed(children)) if batch else children), "canonical Wait changed its child order")
        results.append({"call": calls[0], "result": outputs[0], "wait": wait})
    return results


def database_snapshot(home: Path) -> dict[str, Any]:
    result = {}
    for path in sorted(home.rglob("*.sqlite")):
        require(not path.is_symlink() and path.resolve().is_relative_to(home.resolve()), "database path escapes the fixture")
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            schema = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name").fetchall()
            tables = {row[1] for row in schema if row[0] == "table"}
            require("_sqlx_migrations" in tables, f"native migration history missing: {path.name}")
            migrations = connection.execute("SELECT * FROM _sqlx_migrations ORDER BY version").fetchall()
            migrations = [[{"hex": value.hex()} if isinstance(value, bytes) else value for value in row] for row in migrations]
            result[path.relative_to(home).as_posix()] = {"schema": schema, "migrations": migrations}
    require(bool(result), "native databases were not opened")
    return result


def response_events(items: list[dict[str, Any]]) -> bytes:
    identifier = "cu-" + uuid.uuid4().hex
    events = [{"type": "response.created", "response": {"id": identifier}}]
    events.extend({"type": "response.output_item.done", "item": item} for item in items)
    events.append({"type": "response.completed", "response": {"id": identifier, "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}})
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


def message(text: str) -> list[dict[str, Any]]:
    return [{"type": "message", "role": "assistant", "id": "cu-" + uuid.uuid4().hex, "content": [{"type": "output_text", "text": text}]}]


def tool_call(call_id: str, name: str, arguments: dict[str, Any], namespace: str | None = None) -> list[dict[str, Any]]:
    item = {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(arguments)}
    if namespace is not None:
        item["namespace"] = namespace
    return [item]


def tool_result(body: dict[str, Any], call_id: str) -> Any:
    rows = [item for item in body.get("input", []) if item.get("type") == "function_call_output" and item.get("call_id") == call_id]
    require(len(rows) == 1, f"expected exactly one native result for {call_id}")
    output = rows[0]["output"]
    if isinstance(output, list):
        require(len(output) == 1 and output[0].get("type") in {"input_text", "text"}, f"unexpected result content for {call_id}")
        output = output[0]["text"]
    require(isinstance(output, str), f"non-text native tool result for {call_id}")
    return strict_json(output) if output.strip() else None


class ResponsesFixture:
    def __init__(self, evidence: Path):
        self.evidence = evidence
        self.plans: list[tuple[str, bool, collections.deque]] = []
        self.requests: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.lock = threading.Lock()
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:
                try:
                    require(self.path == "/v1/responses", f"unexpected provider endpoint: {self.path}")
                    size = int(self.headers.get("Content-Length", "0"))
                    require(0 < size <= 16 * 1024 * 1024, "invalid provider request length")
                    raw = self.rfile.read(size)
                    encoding = self.headers.get("Content-Encoding", "identity")
                    if encoding == "zstd":
                        from compression import zstd

                        raw = zstd.decompress(raw)
                    else:
                        require(encoding == "identity", "unsupported provider request encoding")
                    body = strict_json(raw)
                    is_child = bool(self.headers.get("x-codex-parent-thread-id"))
                    text = raw.decode("utf-8")
                    with fixture.lock:
                        index = len(fixture.requests)
                        request = {"index": index, "received_at": utc_now(), "is_child": is_child, "parent_thread_id": self.headers.get("x-codex-parent-thread-id"), "body": body}
                        fixture.requests.append(request)
                        write_json(evidence / f"provider-{index:04d}.json", request)
                        matches = [(marker, actions) for marker, child, actions in reversed(fixture.plans) if child == is_child and marker in text]
                        require(bool(matches), "provider request has no registered fixture marker")
                        marker, actions = matches[0]
                        require(bool(actions), f"unexpected extra model request for {marker}")
                        action = actions.popleft()
                    items = action(body) if callable(action) else action
                    data = response_events(items)
                    (evidence / f"provider-{index:04d}.sse").write_bytes(data)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    self.wfile.flush()
                except Exception as error:
                    with fixture.lock:
                        fixture.errors.append(f"{type(error).__name__}: {error}")
                    self.send_error(500, "fixture request failed; see evidence")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def plan(self, marker: str, actions: list[Any], *, child: bool = False) -> None:
        with self.lock:
            require(all(marker != existing for existing, _, _ in self.plans), "duplicate fixture marker")
            self.plans.append((marker, child, collections.deque(actions)))

    def prepend(self, marker: str, actions: list[Any]) -> None:
        with self.lock:
            plan = next(actions_queue for existing, _, actions_queue in self.plans if existing == marker)
            plan.extendleft(reversed(actions))

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        write_json(self.evidence / "provider-result.json", {"errors": self.errors, "requests": len(self.requests), "unconsumed": {marker: len(actions) for marker, _, actions in self.plans if actions}})
        require(not self.thread.is_alive(), "fixture listener did not stop")
        require(not self.errors, f"provider fixture failed: {self.errors}")
        require(all(not actions for _, _, actions in self.plans), "expected model requests were not observed")


class AppServer:
    def __init__(self, executable: Path, home: Path, workspace: Path, evidence: Path, provider_url: str, role: str, mode: str, label: str, *, timeout: float = 45, sqlite_home: Path | None = None):
        self.timeout = timeout
        self.evidence = evidence
        self.label = label
        self.home = home
        self.workspace = workspace
        self.mode = mode
        self.pending: list[dict[str, Any]] = []
        self.messages: queue.Queue = queue.Queue()
        self.counter = 0
        self.closed = False
        self.stream_error: Exception | None = None
        home.mkdir(parents=True, exist_ok=True)
        workspace.mkdir(parents=True, exist_ok=True)
        evidence.mkdir(parents=True, exist_ok=True)
        self.stderr = (evidence / f"{label}.stderr.txt").open("wb")
        self.rpc = (evidence / f"{label}.rpc.jsonl").open("w", encoding="utf-8")
        self.log_lock = threading.Lock()
        system_environment = {"PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "USERNAME", "USERDOMAIN", "PROGRAMFILES", "PROGRAMW6432", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "LANG", "LC_ALL"}
        env = {key: value for key, value in os.environ.items() if key.upper() in system_environment}
        env.update(CODEX_HOME=str(home), CSA_COLD_UNPLUG_TEST_KEY="loopback-fixture-only", NO_PROXY="127.0.0.1,localhost")
        config = [
            'model_provider="cold_unplug"', f'model="{MODEL}"',
            'model_providers.cold_unplug={name="Cold unplug fixture",base_url=' + json.dumps(provider_url) + ',wire_api="responses",env_key="CSA_COLD_UNPLUG_TEST_KEY"}',
            'approval_policy="never"', 'sandbox_mode="danger-full-access"',
            "features.collab=true", "features.multi_agent_v2=true", "features.plugins=false",
        ]
        if sqlite_home is not None:
            config.append("sqlite_home=" + json.dumps(str(sqlite_home)))
        argv = [str(executable), "app-server"]
        for value in config:
            argv.extend(["-c", value])
        self.record = {"id": f"{mode}-{label}", "role": role, "mode": mode, **binary_identity(executable, env), "started_at": utc_now(), "status": "NOT VERIFIED", "normal_shutdown": False, "evidence": [f"{label}.rpc.jsonl", f"{label}.stderr.txt"]}
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, cwd=workspace, env=env)
        self.record["pid"] = self.process.pid
        self.record["argv"] = argv

        def read_stdout() -> None:
            try:
                for raw in self.process.stdout:
                    row = strict_json(raw)
                    require(isinstance(row, dict), "app-server returned a non-object RPC record")
                    self.log("receive", row)
                    self.messages.put(row)
            except Exception as error:
                self.stream_error = error
                self.messages.put(error)
            finally:
                self.messages.put(None)

        self.reader = threading.Thread(target=read_stdout, daemon=True)
        self.reader.start()

    def log(self, direction: str, value: Any) -> None:
        with self.log_lock:
            self.rpc.write(json.dumps({"at": utc_now(), "direction": direction, "message": value}, ensure_ascii=False) + "\n")
            self.rpc.flush()

    def send(self, message: dict[str, Any]) -> None:
        self.log("send", message)
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def receive(self, deadline: float) -> dict[str, Any]:
        try:
            row = self.messages.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty as error:
            raise ValidationError(f"{self.label}: native response timed out") from error
        require(row is not None, f"{self.label}: app-server closed before the expected response")
        if isinstance(row, Exception):
            raise ValidationError(f"{self.label}: invalid native stream: {row}") from row
        if "method" in row and "id" in row:
            self.send({"id": row["id"], "error": {"code": -32601, "message": "Unexpected client request in isolated fixture"}})
            raise ValidationError(f"unexpected native client request: {row['method']}")
        return row

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.counter += 1
        identifier = self.counter
        self.send({"id": identifier, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            row = self.receive(deadline)
            if row.get("id") == identifier:
                require("error" not in row, f"native {method} failed: {row.get('error')}")
                require(isinstance(row.get("result"), dict), f"native {method} returned no object result")
                return row["result"]
            self.pending.append(row)

    def initialize(self) -> None:
        result = self.call("initialize", {"clientInfo": {"name": "cold_unplug_validation", "version": "1"}, "capabilities": {"experimentalApi": True}})
        require(isinstance(result.get("codexHome"), str) and Path(result["codexHome"]).resolve() == self.home.resolve(), "native initialization selected another CODEX_HOME")
        self.send({"method": "initialized", "params": {}})

    def start(self) -> dict[str, Any]:
        return self.call("thread/start", {"model": MODEL, "cwd": str(self.workspace), "approvalPolicy": "never", "sandbox": "danger-full-access", "ephemeral": False, "historyMode": self.mode})["thread"]

    def resume(self, thread_id: str) -> dict[str, Any]:
        thread = self.call("thread/resume", {"threadId": thread_id})["thread"]
        require(thread["id"] == thread_id, "native resume replaced the thread identity")
        return thread

    def turn(self, thread_id: str, prompt: str) -> dict[str, Any]:
        turn = self.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": prompt, "text_elements": []}]})["turn"]
        deadline = time.monotonic() + self.timeout
        while True:
            match = next((index for index, row in enumerate(self.pending) if row.get("method") == "turn/completed" and row.get("params", {}).get("threadId") == thread_id and row["params"]["turn"]["id"] == turn["id"]), None)
            if match is not None:
                completed = self.pending.pop(match)["params"]["turn"]
                require(completed["status"] == "completed", f"native turn failed: {completed}")
                return completed
            self.pending.append(self.receive(deadline))

    def pages(self, method: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        cursors: set[str] = set()
        cursor = None
        while True:
            result = self.call(method, {**params, "limit": 2, "cursor": cursor})
            require(isinstance(result.get("data"), list), f"{method}: missing page data")
            page = result["data"]
            if method == "thread/timeline/list":
                keys = [timeline_key(row) for row in page]
                require(keys == sorted(set(keys)), "native timeline page order or identity differs")
                require(not rows or not page or keys[-1] < timeline_key(rows[0]), "native timeline pages overlap or move forwards")
                rows = page + rows
            else:
                rows.extend(page)
            cursor = result.get("nextCursor")
            if cursor is None:
                return rows
            require(bool(page), f"{method}: empty page has a continuation cursor")
            require(isinstance(cursor, str) and cursor and cursor not in cursors, f"{method}: pagination did not progress")
            cursors.add(cursor)

    def close(self) -> dict[str, Any]:
        if self.closed:
            return self.record
        self.closed = True
        normal = False
        forced = False
        try:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
            code = self.process.wait(timeout=self.timeout)
            normal = code == 0
        except subprocess.TimeoutExpired:
            forced = True
            # Only this owned process tree is eligible for forced cleanup; it fails the gate.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True, check=False)
            else:
                self.process.kill()
            code = self.process.wait(timeout=10)
        finally:
            self.reader.join(timeout=5)
            self.stderr.close()
            if not self.reader.is_alive():
                self.rpc.close()
            complete = normal and not self.reader.is_alive() and self.stream_error is None
            self.record.update(ended_at=utc_now(), exit_code=self.process.returncode, normal_shutdown=complete, forced_cleanup=forced, status="passed" if complete else "failed", stream_error=str(self.stream_error) if self.stream_error else None)
            write_json(self.evidence / f"{self.label}.process.json", self.record)
        require(self.record["normal_shutdown"], f"{self.label}: persistence shutdown was not normal")
        return self.record


def assert_history_preserved(before: dict[str, Any], after: dict[str, Any], *, official_legacy_wait_ids: set[str] | None = None) -> None:
    require(before["id"] == after["id"], "native history identity changed")
    old = before.get("turns", [])
    new = after.get("turns", [])
    require(len({turn["id"] for turn in new}) == len(new), "native history repeated a turn")
    positions = {turn["id"]: index for index, turn in enumerate(new)}
    require(all(turn["id"] in positions for turn in old), "native reader dropped a turn")
    require([positions[turn["id"]] for turn in old] == sorted(positions[turn["id"]] for turn in old), "native reader reordered prior turns")
    for turn in old:
        observed = new[positions[turn["id"]]]
        items = turn["items"]
        if official_legacy_wait_ids:
            # Stock 0.153.2's public Legacy reader filters Wait before replay.
            # Canonical Join calls/results remain independently checked and preserved.
            items = [item for item in items if not (item.get("id") in official_legacy_wait_ids and item.get("type") == "collabAgentToolCall" and item.get("tool") == "wait" and item.get("status") == "completed")]
        require(items == observed["items"], f"native reader changed or dropped items in turn {turn['id']}")
        require(turn["status"] == observed["status"], f"native reader changed terminal status in turn {turn['id']}")


def read_thread(server: AppServer, identifier: str) -> dict[str, Any]:
    result = server.call("thread/read", {"threadId": identifier, "includeTurns": True})["thread"]
    require(result["id"] == identifier, "native reader returned another thread")
    require(bool(result.get("turns")), f"native reader returned no persisted turns for {identifier}")
    return result


def spawn_thread_id(thread: dict[str, Any], call_id: str) -> str:
    items = [item for turn in thread["turns"] for item in turn["items"] if item["id"] == call_id and item["type"] == "subAgentActivity" and item["kind"] == "started"]
    require(len(items) == 1, f"native spawn history missing for {call_id}")
    identifier = items[0]["agentThreadId"]
    uuid.UUID(identifier)
    return identifier


def check_paginated_readers(server: AppServer, thread: dict[str, Any]) -> dict[str, Any]:
    identifier = thread["id"]
    turns = server.pages("thread/turns/list", {"threadId": identifier, "sortDirection": "asc", "itemsView": "full"})
    assert_history_preserved(thread, {"id": identifier, "turns": turns})
    require(len(turns) == len(thread["turns"]), "turn pages contain an unexpected or duplicated turn")
    items = server.pages("thread/items/list", {"threadId": identifier, "sortDirection": "asc"})
    # Async child completion may land in an earlier turn after a later turn starts.
    # Preserve global reader order while accounting for every item within its turn.
    grouped = {turn["id"]: [] for turn in thread["turns"]}
    for row in items:
        require(row["turnId"] in grouped, "native item page refers to an unknown turn")
        grouped[row["turnId"]].append(row["item"])
    require(grouped == {turn["id"]: turn["items"] for turn in thread["turns"]}, "native item pages differ from the full-turn reader")
    timeline = server.pages("thread/timeline/list", {"threadId": identifier})
    timeline_items = [{"turnId": row["turnId"], "item": row["item"]} for row in timeline if row["type"] == "item"]
    require(timeline_items == items, "native timeline dropped, changed or reordered persisted items")
    keys = [timeline_key(row) for row in timeline]
    require(keys == sorted(set(keys)), "native timeline entry identities repeat or regress")
    return {"turns": turns, "items": items, "timeline": timeline}


def project_sentinel(home: Path, identifier: str, *, seed: bool = False) -> list[Any]:
    paths = list(home.glob("state_*.sqlite"))
    require(len(paths) == 1, "expected one actually opened native state database")
    path = paths[0]
    with sqlite3.connect(path if seed else path.resolve().as_uri() + "?mode=ro", uri=not seed) as connection:
        if seed:
            connection.execute(
                "INSERT INTO projects (id, name, position, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?)",
                (identifier, "cold-unplug preserved project", 999, 1, 1),
            )
        row = connection.execute("SELECT id, name, position, created_at_ms, updated_at_ms FROM projects WHERE id = ?", (identifier,)).fetchone()
    require(row is not None, "native project sentinel was lost")
    return list(row)


def assert_databases_preserved(before: dict[str, Any], after: dict[str, Any]) -> None:
    require(set(before) <= set(after), "native database disappeared")
    for name, database in before.items():
        require(database == after[name], f"native schema or complete migration history changed: {name}")


def wait_for_agent_plan(provider: ResponsesFixture, marker: str, target: str, expected_output: str, *, followup: str | None = None, release: threading.Event | None = None) -> set[str]:
    """Observe native completion status; never join a stale spawn run after follow-up."""
    state = {"attempt": 0, "list_call": "cu-list-" + uuid.uuid4().hex}
    followup_id = "cu-followup-" + uuid.uuid4().hex
    wait_ids: set[str] = set()

    def list_status(body: dict[str, Any]) -> list[dict[str, Any]]:
        if state["attempt"] == 0 and followup is not None:
            require(tool_result(body, followup_id) is None, "native follow-up returned unexpected content")
            if release is not None:
                release.set()
        return tool_call(state["list_call"], "list_agents", {}, "collaboration")

    def inspect_status(body: dict[str, Any]) -> list[dict[str, Any]]:
        result = tool_result(body, state["list_call"])
        rows = [row for row in result["agents"] if row["agent_name"] == target]
        require(len(rows) == 1, "original child disappeared from native agent listing")
        status = rows[0]["agent_status"]
        if isinstance(status, dict) and status.get("completed") == expected_output:
            return message(marker + "_observed_native_completion")
        require(status in ("pending_init", "running") or isinstance(status, dict) and "completed" in status, f"child did not remain runnable: {status}")
        state["attempt"] += 1
        require(state["attempt"] <= 6, "native child did not reach the required completion")
        state["list_call"] = "cu-list-" + uuid.uuid4().hex
        provider.prepend(marker, [list_status, inspect_status])
        wait_id = "cu-wait-" + uuid.uuid4().hex
        wait_ids.add(wait_id)
        return tool_call(wait_id, "wait_agent", {"timeout_ms": 1000}, "collaboration")

    actions = []
    if followup is not None:
        actions.append(tool_call(followup_id, "followup_task", {"target": target, "message": followup}, "collaboration"))
    actions.extend([list_status, inspect_status])
    provider.plan(marker, actions)
    return wait_ids


def assert_native_discovery(listing: list[dict[str, Any]], children: list[dict[str, Any]], expected_ids: list[str]) -> None:
    # Native root listing excludes descendants; discover children through parent edges.
    require(len(listing) == len({row["id"] for row in listing}) and set(expected_ids[:2]) <= {row["id"] for row in listing}, "official native listing lost or duplicated a parent")
    require(len(children) == len(expected_ids[2:]) and set(expected_ids[2:]) == {row["id"] for row in children}, "official native parent edges changed")


def check_native_diagnostics(evidence: Path, labels: list[str]) -> None:
    pattern = re.compile(r"(?:failed|unable) to (?:parse|deserialize|project|read).*?(?:rollout|history|record)|skipp(?:ed|ing).*?(?:rollout|history|record)|migration \d+ was previously applied but has been modified", re.IGNORECASE)
    for label in labels:
        text = (evidence / f"{label}.stderr.txt").read_text(encoding="utf-8", errors="strict")
        require(pattern.search(text) is None, f"native persistence diagnostic in {label}; see preserved stderr")


def spawn_turn(server: AppServer, provider: ResponsesFixture, parent_id: str, marker: str, tasks: list[dict[str, Any]], join_kind: str | None) -> dict[str, Any]:
    calls = ["cu-spawn-" + uuid.uuid4().hex for _ in tasks]
    join_id = "cu-join-" + uuid.uuid4().hex
    runs: list[dict[str, str]] = []
    terminal: list[dict[str, Any]] = []

    def spawn(index: int) -> list[dict[str, Any]]:
        args = {key: tasks[index][key] for key in ("task_name", "message", "agent_type", "fork_turns") if key in tasks[index]}
        return tool_call(calls[index], "spawn_agent", args, "collaboration")

    actions: list[Any] = [spawn(0)]
    for index in range(len(tasks)):
        def capture(body: dict[str, Any], index: int = index) -> list[dict[str, Any]]:
            result = tool_result(body, calls[index])
            require(isinstance(result, dict) and isinstance(result.get("task_name"), str) and isinstance(result.get("run_id"), str), "native spawn did not return an exact-run receipt")
            runs.append({"target": result["task_name"], "run_id": result["run_id"]})
            if index + 1 < len(tasks):
                return spawn(index + 1)
            if join_kind == "single":
                require(len(runs) == 1, "single Join fixture must have one child")
                return tool_call(join_id, "join_agent", runs[0])
            if join_kind == "batch":
                require(len(runs) >= 2, "batch Join fixture requires at least two children")
                return tool_call(join_id, "join_agents", {"runs": list(reversed(runs))})
            return message(marker + "_spawned")

        actions.append(capture)
    if join_kind is not None:
        def joined(body: dict[str, Any]) -> list[dict[str, Any]]:
            result = tool_result(body, join_id)
            results = [result] if join_kind == "single" else result["results"]
            expected_runs = runs if join_kind == "single" else list(reversed(runs))
            expected_tasks = tasks if join_kind == "single" else list(reversed(tasks))
            require(len(results) == len(expected_runs), "Join dropped an exact child run")
            for observed, run, task in zip(results, expected_runs, expected_tasks, strict=True):
                require(all(observed.get(key) == value for key, value in run.items()), "Join changed run identity or ordering")
                require(observed.get("status") == "completed" and observed.get("output") == task["expected_output"], "Join did not return the exact completed child result")
            terminal.extend(results)
            return message(marker + "_joined")

        actions.append(joined)
    provider.plan(marker, actions)
    server.turn(parent_id, marker)
    history = read_thread(server, parent_id)
    children = [spawn_thread_id(history, call) for call in calls]
    if join_kind is not None:
        joins = [item for turn in history["turns"] for item in turn["items"] if item.get("id") == join_id and item.get("type") == "collabAgentToolCall"]
        require(len(joins) == 1 and joins[0].get("tool") == "wait", "Join history did not use native Wait presentation")
    return {"runs": runs, "thread_ids": children, "spawn_call_ids": calls, "join_call_id": join_id if join_kind else None, "terminal": terminal}


def run_mode(mode: str, root: Path, evidence: Path, binaries: dict[str, Path], provider: ResponsesFixture, processes: list[dict[str, Any]]) -> dict[str, Any]:
    home = root / mode / "home"
    workspace = root / mode / "workspace"
    output = evidence / mode
    output.mkdir(parents=True)
    servers: list[AppServer] = []
    positive_labels: list[str] = []
    observations: dict[str, Any] = {"mode": mode, "started_at": utc_now()}
    worker_started = threading.Event()
    worker_release = threading.Event()
    private = f"CU_PARENT_PRIVATE_{mode}"
    worker_marker = f"CU_WORKER_HANDOFF_{mode}"
    worker_initial = f"CU_WORKER_INITIAL_{mode}"
    worker_busy = f"CU_WORKER_BUSY_RESULT_{mode}"
    worker_completed = f"CU_WORKER_COMPLETED_RESULT_{mode}"
    worker_cold = f"CU_WORKER_COLD_RESULT_{mode}"
    fork_marker = f"CU_FULL_FORK_{mode}"

    def open_phase(role: str, label: str, *, sqlite_home: Path | None = None) -> AppServer:
        server = AppServer(binaries[role], home, workspace, output, provider.url, role, mode, label, timeout=90, sqlite_home=sqlite_home)
        servers.append(server)
        positive_labels.append(label)
        server.initialize()
        return server

    def simple_turn(server: AppServer, identifier: str, marker: str) -> None:
        provider.plan(marker, [message(marker + "_reply")])
        server.turn(identifier, marker)

    try:
        official = open_phase("official", "official-seed")
        a = official.start()["id"]
        simple_turn(official, a, f"CU_OFFICIAL_SEED1_{mode}")
        simple_turn(official, a, f"CU_OFFICIAL_SEED2_{mode}")
        a_seed = read_thread(official, a)
        official.close()
        sentinel_id = "cold-unplug-" + uuid.uuid4().hex
        sentinel = project_sentinel(home, sentinel_id, seed=True)
        database_before = database_snapshot(home)
        observations.update(official_parent=a, official_seed=a_seed, databases_before=database_before, sentinel=sentinel)

        candidate = open_phase("candidate", "candidate-work")
        assert_history_preserved(a_seed, candidate.resume(a))
        simple_turn(candidate, a, f"CU_CANDIDATE_APPEND_{mode}")
        b = candidate.start()["id"]
        simple_turn(candidate, b, private)
        explorer_marker = f"CU_EXPLORER_HANDOFF_{mode}"
        findings = f"CU_EXPLORER_FINDINGS_{mode}: native history remains the only source of truth"
        provider.plan(explorer_marker, [message(findings)], child=True)
        explorer = spawn_turn(candidate, provider, b, f"CU_EXPLORE_{mode}", [{"task_name": "inspect_history", "agent_type": "explorer", "message": explorer_marker + " QUESTION: inspect native history. SCOPE: this fixture. CONSTRAINTS: read only. RETURN: findings and evidence.", "expected_output": findings}], "single")

        def busy_worker(_body: dict[str, Any]) -> list[dict[str, Any]]:
            worker_started.set()
            require(worker_release.wait(timeout=60), "busy-worker barrier was not released by native follow-up")
            return message(worker_initial)

        provider.plan(worker_marker, [busy_worker, message(worker_busy), message(worker_completed), message(worker_cold)], child=True)
        handoff = worker_marker + " GOAL: verify persistence. OWNERSHIP: fixture only. KNOWN FINDINGS: " + explorer["terminal"][0]["output"] + " CONSTRAINTS: preserve other work. VALIDATION: report observations. RETURN: result and checks."
        worker = spawn_turn(candidate, provider, b, f"CU_SPAWN_WORKER_{mode}", [{"task_name": "original_worker", "agent_type": "worker", "message": handoff}], None)
        require(worker_started.wait(timeout=20), "worker never entered the controlled running state")
        target = worker["runs"][0]["target"]
        busy_prompt = f"CU_BUSY_FOLLOWUP_{mode}"
        busy_wait_ids = wait_for_agent_plan(provider, busy_prompt, target, worker_busy, followup=busy_prompt + " Continue the original task with this finding.", release=worker_release)
        candidate.turn(b, busy_prompt)
        completed_prompt = f"CU_COMPLETED_FOLLOWUP_{mode}"
        completed_wait_ids = wait_for_agent_plan(provider, completed_prompt, target, worker_completed, followup=completed_prompt + " Address the omitted check on this same worker.")
        candidate.turn(b, completed_prompt)

        batch_tasks = []
        for index in range(2):
            marker = f"CU_BATCH_CHILD_{mode}_{index}"
            reply = marker + "_result"
            provider.plan(marker, [message(reply)], child=True)
            batch_tasks.append({"task_name": f"batch_{index}", "agent_type": "worker", "message": marker + " Inspect this isolated fixture and return the result.", "expected_output": reply})
        batch = spawn_turn(candidate, provider, b, f"CU_BATCH_JOIN_{mode}", batch_tasks, "batch")
        fork_output = fork_marker + "_initial_result"
        fork_official = fork_marker + "_official_resume_result"
        provider.plan(fork_marker, [message(fork_output), message(fork_official)], child=True)
        fork = spawn_turn(candidate, provider, b, f"CU_SPAWN_FULL_FORK_{mode}", [{"task_name": "full_fork", "agent_type": "default", "fork_turns": "all", "message": fork_marker + " Continue with the explicitly inherited native history.", "expected_output": fork_output}], "single")
        a_candidate = read_thread(candidate, a)
        candidate.close()
        assert_databases_preserved(database_before, database_snapshot(home))
        require(project_sentinel(home, sentinel_id) == sentinel, "candidate changed the project sentinel")

        cold = open_phase("candidate", "candidate-cold-restore")
        cold.resume(b)
        cold_prompt = f"CU_COLD_FOLLOWUP_{mode}"
        cold_wait_ids = wait_for_agent_plan(provider, cold_prompt, target, worker_cold, followup=cold_prompt + " Reopen the same original worker and verify the persisted result.")
        cold.turn(b, cold_prompt)
        expected_ids = [a, b, *explorer["thread_ids"], *worker["thread_ids"], *batch["thread_ids"], *fork["thread_ids"]]
        histories = {identifier: read_thread(cold, identifier) for identifier in expected_ids}
        cold.close()
        require(worker["thread_ids"][0] in histories, "cold restore replaced the original worker")
        canonical_before = read_canonical(home)
        event_wait_ids = busy_wait_ids | completed_wait_ids | cold_wait_ids
        persisted_joins = verify_persisted_joins(canonical_before, b, [explorer, batch, fork], event_wait_ids)
        official_legacy_wait_ids = {row["wait"]["id"] for row in persisted_joins} if mode == "legacy" else set()
        inventory = [path.relative_to(home).as_posix() for path in sorted(home.rglob("*")) if path.is_file()]
        require(not any("csa" in PurePosixPath(path).name.lower() for path in inventory), "unclassified CSA-named persistent state requires explicit ownership review")
        observations.update(candidate_parent=b, explorer=explorer, worker=worker, batch=batch, full_fork=fork, before_unplug=histories, canonical_before=canonical_before, persisted_joins=persisted_joins, optional_state={"inventory": inventory, "positively_identified": [], "removed": []})
        config_path = home / "config.toml"
        native_config = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        require("subagent_live_mouse" not in native_config.get("tui", {}), "candidate persisted the removed private TUI setting")
        observations["config_before_official_reopen"] = {"exists": config_path.exists(), "private_tui_key_absent": True, "file": file_record(config_path) if config_path.exists() else None}

        reopened = open_phase("official", "official-reopen")
        listing = reopened.pages("thread/list", {"sourceKinds": SOURCES, "modelProviders": []})
        children = reopened.pages("thread/list", {"parentThreadId": b, "sourceKinds": SOURCES, "modelProviders": []})
        assert_native_discovery(listing, children, expected_ids)
        pages = {}
        for identifier, before in histories.items():
            after = read_thread(reopened, identifier)
            assert_history_preserved(before, after, official_legacy_wait_ids=official_legacy_wait_ids)
            if mode == "paginated":
                pages[identifier] = check_paginated_readers(reopened, after)
        assert_history_preserved(a_candidate, reopened.resume(a), official_legacy_wait_ids=official_legacy_wait_ids)
        simple_turn(reopened, a, f"CU_OFFICIAL_FINAL_{mode}")
        reopened.resume(b)
        fork_prompt = f"CU_OFFICIAL_FORK_FOLLOWUP_{mode}"
        wait_for_agent_plan(provider, fork_prompt, fork["runs"][0]["target"], fork_official, followup=fork_prompt + " Resume this existing native fork and preserve its own history.")
        reopened.turn(b, fork_prompt)
        after_fork = read_thread(reopened, fork["thread_ids"][0])
        assert_history_preserved(histories[fork["thread_ids"][0]], after_fork, official_legacy_wait_ids=official_legacy_wait_ids)
        latest_histories = {identifier: read_thread(reopened, identifier) for identifier in expected_ids}
        reopened.close()
        canonical_after = read_canonical(home)
        assert_canonical_preserved(canonical_before, canonical_after)
        require(verify_persisted_joins(canonical_after, b, [explorer, batch, fork], event_wait_ids) == persisted_joins, "official resume changed canonical Join records")
        database_after = database_snapshot(home)
        assert_databases_preserved(database_before, database_after)
        require(project_sentinel(home, sentinel_id) == sentinel, "official reopen changed the project sentinel")
        observations.update(official_listing=listing, official_parent_edges=children, official_histories=latest_histories, pages=pages, databases_after=database_after, canonical_after=canonical_after, canonical_join_verified=True)

        child_requests = [request for request in provider.requests if request["is_child"]]
        for marker in (explorer_marker, worker_marker):
            requests = [row for row in child_requests if marker in json.dumps(row["body"]) and fork_marker not in json.dumps(row["body"])]
            require(bool(requests), f"missing child model input for {marker}")
            require(all(private not in json.dumps(row["body"]) for row in requests), "fresh/follow-up child inherited unrelated parent history")
            if marker == worker_marker:
                require(len(requests) == 4, "original worker did not execute all four expected submissions")
                for request in requests[1:]:
                    require(worker_initial in json.dumps(request["body"]), "original worker lost its own initial transcript")
        fork_requests = [row for row in child_requests if fork_marker in json.dumps(row["body"])]
        require(len(fork_requests) == 2 and private in json.dumps(fork_requests[0]["body"]), "explicit all fork did not inherit the expected native context")
        require(fork_output in json.dumps(fork_requests[1]["body"]), "official fork resume lost its prior result")

        if mode == "paginated":
            require(len(database_before) == 6 and len(database_after) == 6, "both binaries must actually exercise all six native databases")
            original = read_canonical(home)
            rebuilt = {}
            for index, (identifier, before) in enumerate(latest_histories.items()):
                projection = open_phase("official", f"official-reproject-{index}", sqlite_home=root / mode / "rebuilt-databases")
                # Discover canonical children before loading their owner's metadata.
                listing = projection.pages("thread/list", {"sourceKinds": SOURCES, "modelProviders": []})
                children = projection.pages("thread/list", {"parentThreadId": b, "sourceKinds": SOURCES, "modelProviders": []})
                assert_native_discovery(listing, children, expected_ids)
                if identifier in expected_ids[2:]:
                    projection.resume(b)
                # One child per process respects native V2 resident-thread capacity.
                projection.resume(identifier)
                after = read_thread(projection, identifier)
                assert_history_preserved(before, after)
                rebuilt[identifier] = check_paginated_readers(projection, after)
                projection.close()
            require(original == read_canonical(home), "native reprojection changed canonical bytes or records")
            observations["rebuilt_pages"] = rebuilt

        checksum_failures = {}
        for role in ("official", "candidate"):
            negative_home = root / mode / f"checksum-negative-{role}"
            shutil.copytree(home, negative_home)
            label = "checksum-negative-" + role
            control = AppServer(binaries[role], negative_home, workspace, output, provider.url, role, mode, label + "-control")
            try:
                control.initialize()
            finally:
                control.close()
            assert_databases_preserved(database_after, database_snapshot(negative_home))
            require(project_sentinel(negative_home, sentinel_id) == sentinel, "checksum control changed unrelated sentinel data")
            state_paths = list(negative_home.glob("state_*.sqlite"))
            require(len(state_paths) == 1, "checksum fixture requires one native state DB")
            with sqlite3.connect(state_paths[0]) as connection:
                version = connection.execute("SELECT min(version) FROM _sqlx_migrations").fetchone()[0]
                connection.execute("UPDATE _sqlx_migrations SET checksum = ? WHERE version = ?", (b"\x91" * 48, version))
            corrupted = database_snapshot(negative_home)
            # Deliberate corruption can reject startup before RPC initialization.
            # Its failed process is evidence for H, not a positive persistence phase.
            negative = AppServer(binaries[role], negative_home, workspace, output, provider.url, role, mode, label)
            native_error = ""
            try:
                negative.initialize()
                negative.start()
            except (ValidationError, BrokenPipeError) as error:
                native_error = str(error)
            finally:
                try:
                    negative.close()
                except ValidationError as error:
                    native_error += "\n" + str(error)
            require(negative.record["exit_code"] in (0, 1) and not negative.record["forced_cleanup"] and not negative.reader.is_alive() and negative.stream_error is None, "checksum fixture did not finish without forced or invalid-stream cleanup")
            diagnostics = (output / f"{label}.stderr.txt").read_text(encoding="utf-8") + native_error
            # This CLI truncates the migration cause at the native state-runtime error.
            # The same-home successful control and sole checksum change bind that failure.
            startup_rejected = negative.record["exit_code"] == 1 and f"failed to initialize sqlite state runtime under {negative_home}" in diagnostics and f"failed to initialize state runtime at {negative_home}" in diagnostics
            require("was previously applied but has been modified" in diagnostics or startup_rejected, "native checksum validation failure was not observed")
            assert_databases_preserved(corrupted, database_snapshot(negative_home))
            require(project_sentinel(negative_home, sentinel_id) == sentinel, "checksum error changed unrelated sentinel data")
            checksum_failures[role] = {"version": version, "native_error_observed": True, "migration_rows_preserved": True, "sentinel_preserved": True, "same_home_control": control.record, "process": negative.record, "diagnostic": diagnostics}
        observations["checksum_failures"] = checksum_failures
        check_native_diagnostics(output, positive_labels)
        observations["status"] = "passed"
        cases = {case: {"status": "passed", "evidence": [f"{mode}/observations.json"]} for case in catalog.COLD_UNPLUG_CASES}
        cases["G"].update(presentation="official-native" if mode == "legacy" else "native-wait", canonical_join_verified=True)
        return cases
    finally:
        worker_release.set()
        cleanup_errors = []
        for server in servers:
            try:
                server.close()
            except Exception as error:
                cleanup_errors.append(str(error))
            record = {**server.record, "evidence": [f"{mode}/{path}" for path in server.record["evidence"]]}
            processes.append(record)
        observations.update(ended_at=utc_now(), cleanup_errors=cleanup_errors)
        write_json(output / "observations.json", observations)
        require(not cleanup_errors, f"owned process cleanup failed: {cleanup_errors}")


def prepare_binaries(args: argparse.Namespace, root: Path, evidence: Path) -> tuple[dict[str, Any], dict[str, Path], dict[str, Any]]:
    from compat_release import verify_target_bundle

    require(os.name == "nt", "this binary/terminal acceptance command requires Windows")
    paths = {}
    for name in ("repository", "manifest", "official_binary", "official_archive", "candidate_binary", "build_receipt", "native_source"):
        value = getattr(args, name)
        require(value.is_absolute(), f"{name} must be absolute")
        paths[name] = value.resolve(strict=True)
    repository = paths["repository"]
    manifest, _, _ = catalog.load_manifest(repository, paths["manifest"])
    resolution = catalog.resolve(repository, manifest["compat_id"], TARGET)
    require((resolution["codex_version"], resolution["upstream_commit"]) in {
        ("0.153.2", "657a993cbee87acf52d14b758ce49dbd46d1b8eb"),
        ("0.154.0", "6b9826e3aa83b1a5947db50f4332cb9c65f1b340"),
    }, "official Legacy presentation requires an exact reviewed upstream version and commit")
    require((repository / resolution["manifest_path"]).resolve() == paths["manifest"], "explicit manifest differs from the catalog route")
    require(paths["official_binary"] != paths["candidate_binary"], "official and candidate executable paths must differ")
    receipt = strict_json(paths["build_receipt"].read_bytes())
    require(receipt.get("status") == "passed", "a verified completed GitHub build receipt is required; dispatch acceptance is insufficient")
    run_id = receipt.get("workflow_run_id")
    require(type(run_id) is int and run_id > 0, "build receipt must bind an exact positive run ID")
    require(receipt.get("builder_repository") == "DSLZL/CSA-codex-windows-x64" and receipt.get("runner") == "windows-2025" and receipt.get("target") == TARGET, "build receipt differs from the fixed Windows shard")
    catalog.require_string(receipt.get("recipe_commit"), "recipe commit", pattern=catalog.LOWER_SHA1)
    bundle = paths["candidate_binary"].parent.parent
    verified = verify_target_bundle(
        paths["manifest"], bundle, request_id=receipt["request_id"], source_commit=receipt["source_commit"],
        repository=receipt["builder_repository"], runner=receipt["runner"], target=TARGET, workflow_run_id=str(run_id),
    )
    require(Path(verified["artifact"]).resolve() == paths["candidate_binary"], "candidate path differs from the verified target bundle")
    (evidence / "build").mkdir()
    shutil.copy2(bundle / "target-record.json", evidence / "build/target-record.json")
    shutil.copy2(paths["build_receipt"], evidence / "build/github-result.json")
    runtime = catalog.load_json(repository / resolution["runtime_lock_path"])
    hasher = hashlib.sha512()
    with paths["official_archive"].open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    integrity = "sha512-" + base64.b64encode(hasher.digest()).decode("ascii")
    require(integrity == runtime["integrity"], "official archive differs from the pinned runtime integrity")
    executable_member = f"package/vendor/{TARGET}/bin/codex.exe"
    selected = set(runtime["required_files"]) | {executable_member}
    binaries = {role: root / "runtime" / role / executable_member for role in ("official", "candidate")}
    with tarfile.open(paths["official_archive"], "r:gz") as archive:
        members = [member for member in archive.getmembers() if member.name in selected]
        require(len(members) == len(selected) and {member.name for member in members} == selected, "official runtime archive has missing or duplicated required members")
        for member in members:
            catalog.validate_archive_member(member.name, "official archive member")
            require(member.isfile() and 0 < member.size <= 1024 * 1024 * 1024, "official runtime member must be a bounded regular file")
            destination = root / "runtime/official" / member.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, destination.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            candidate_destination = root / "runtime/candidate" / member.name
            candidate_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, candidate_destination)
    require(catalog.sha256_file(binaries["official"]) == catalog.sha256_file(paths["official_binary"]), "official executable does not match its pinned archive")
    shutil.copy2(paths["candidate_binary"], binaries["candidate"])
    identities = {}
    for role, path in binaries.items():
        identities[role] = binary_identity(path)
        require(identities[role]["version"] == f"codex-cli {resolution['codex_version']}", f"{role} absolute executable version differs")
    require(identities["candidate"]["sha256"] == verified["sha256"] and identities["candidate"]["size"] == verified["size"], "candidate changed while preparing its isolated runtime")
    identities["official"]["archive_integrity"] = integrity
    schema = paths["native_source"] / "codex-rs/core/config.schema.json"
    schema_sha = catalog.sha256_file(schema)
    require(schema_sha == manifest["preimage"]["codex-rs/core/config.schema.json"], "candidate source config schema differs from exact upstream")
    write_json(evidence / "preflight.json", {"resolution": resolution, "binaries": identities, "executable_paths": {role: str(path) for role, path in binaries.items()}, "verified_bundle": verified, "official_archive": file_record(paths["official_archive"]), "source_config_schema_sha256": schema_sha})
    build = {"provider": "github", "repository": receipt["builder_repository"], "source_commit": receipt["source_commit"], "workflow_run_id": run_id, "request_id": receipt["request_id"], "recipe_commit": receipt["recipe_commit"], "target_record": "build/target-record.json"}
    return resolution, binaries, {**identities, "build": build}


def import_terminal(path: Path, evidence: Path, candidate_sha256: str) -> dict[str, Any]:
    require(path.is_absolute(), "terminal receipt path must be absolute")
    value = strict_json(path.read_bytes())
    require(value.get("schema") == 1 and type(value.get("schema")) is int, "terminal receipt schema must be 1")
    require(value.get("candidate_sha256") == candidate_sha256, "terminal observations refer to another executable")
    files = value.get("files")
    require(isinstance(files, dict) and bool(files), "terminal observations need captured evidence files")
    for relative, record in files.items():
        catalog.validate_archive_member(relative, "terminal evidence path")
        require(PurePosixPath(relative).as_posix() == relative and ":" not in relative, "terminal evidence path must be normalized")
        source = path.parent / relative
        require(source.is_file() and not source.is_symlink() and source.resolve().is_relative_to(path.parent.resolve()), "terminal evidence escapes its receipt directory")
        require(file_record(source) == record, "terminal evidence hash or size differs")
        destination = evidence / "terminal" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    terminal = value["terminal"]
    require(isinstance(terminal.get("modes"), dict), "terminal mode observations are missing")
    for row in terminal["modes"].values():
        require(isinstance(row, dict) and isinstance(row.get("evidence"), list), "terminal mode has no evidence references")
        require(all(isinstance(reference, str) and reference in files for reference in row["evidence"]), "terminal mode references missing captured evidence")
        row["evidence"] = ["terminal/" + reference for reference in row["evidence"]]
    shutil.copy2(path, evidence / "terminal/receipt.json")
    return terminal


def run(args: argparse.Namespace) -> int:
    validation_parent = Path(os.environ.get("LOCALAPPDATA", "")) / "CSA-codex-validation"
    require(validation_parent.is_absolute(), "LOCALAPPDATA must identify the isolated validation parent")
    root = fresh_root(args.run_root, validation_parent)
    evidence = root / "evidence"
    evidence.mkdir()
    report: dict[str, Any] = {"schema": 1, "status": "NOT VERIFIED", "fixture_kind": "loopback-responses", "cases": {mode: {case: {"status": "NOT VERIFIED", "evidence": []} for case in catalog.COLD_UNPLUG_CASES} for mode in catalog.COLD_UNPLUG_MODES}, "processes": [], "terminal": {"method": None, "modes": {}}, "files": {}}
    errors = []
    provider = None
    try:
        resolution, binaries, identity = prepare_binaries(args, root, evidence)
        report.update({key: resolution[key] for key in ("compat_id", "codex_version", "upstream_commit", "manifest_sha256", "build_profile_sha256", "runtime_lock_sha256")})
        report.update(target=TARGET, **identity)
        provider = ResponsesFixture(evidence)
        for mode in catalog.COLD_UNPLUG_MODES:
            print(json.dumps({"phase": mode, "status": "starting", "run_root": str(root)}), flush=True)
            try:
                report["cases"][mode] = run_mode(mode, root, evidence, binaries, provider, report["processes"])
            except Exception as error:
                errors.append(f"{mode}: {type(error).__name__}: {error}")
        if args.terminal_evidence is None:
            errors.append("real PTY/ConPTY terminal evidence is NOT VERIFIED")
            for mode in catalog.COLD_UNPLUG_MODES:
                report["cases"][mode]["F"]["status"] = "NOT VERIFIED"
        else:
            report["terminal"] = import_terminal(args.terminal_evidence, evidence, report["candidate"]["sha256"])
            modes = report["terminal"]["modes"]
            if set(modes) != set(catalog.COLD_UNPLUG_MOUSE_MODES) or any(row.get("status") != "passed" for row in modes.values()):
                errors.append("real PTY/ConPTY terminal observations failed or are incomplete")
                for mode in catalog.COLD_UNPLUG_MODES:
                    report["cases"][mode]["F"]["status"] = "failed"
        for role, executable in binaries.items():
            require(file_record(executable) == {key: report[role][key] for key in ("sha256", "size")}, f"{role} executable changed during validation")
    except Exception as error:
        errors.append(f"{type(error).__name__}: {error}")
    finally:
        if provider is not None:
            try:
                provider.close()
            except Exception as error:
                errors.append(f"provider: {type(error).__name__}: {error}")
    write_json(evidence / "execution.json", {"errors": errors, "ended_at": utc_now(), "run_root": str(root)})
    report["files"] = {path.relative_to(evidence).as_posix(): file_record(path) for path in sorted(evidence.rglob("*")) if path.is_file()}
    if not errors:
        report["status"] = "passed"
        acceptance = {key: report[key] for key in ("compat_id", "target", "manifest_sha256", "build_profile_sha256", "runtime_lock_sha256")}
        acceptance.update(artifact_sha256=report["candidate"]["sha256"], artifact_size=report["candidate"]["size"], evidence={"cold_unplug": report})
        manifest, _, _ = catalog.load_manifest(args.repository, args.manifest)
        runtime = catalog.load_json(args.repository / resolution["runtime_lock_path"])
        try:
            catalog.validate_cold_unplug(acceptance, manifest, runtime, evidence_root=evidence)
        except Exception as error:
            errors.append(f"evidence gate: {type(error).__name__}: {error}")
            report["status"] = "failed"
            write_json(evidence / "execution.json", {"errors": errors, "ended_at": utc_now(), "run_root": str(root)})
            report["files"]["execution.json"] = file_record(evidence / "execution.json")
    write_json(evidence / "evidence.json", {"cold_unplug": report})
    print(json.dumps({"status": report["status"], "evidence": str(evidence / "evidence.json"), "errors": errors}, indent=2), flush=True)
    return 0 if not errors and report["status"] == "passed" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("repository", "manifest", "official-binary", "official-archive", "candidate-binary", "build-receipt", "native-source", "run-root"):
        parser.add_argument("--" + option, type=Path, required=True)
    parser.add_argument("--terminal-evidence", type=Path, help="Completed real PTY/ConPTY capture receipt; omission leaves the gate unverified and exits nonzero")
    args = parser.parse_args()
    try:
        return run(args)
    except (ValidationError, OSError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
