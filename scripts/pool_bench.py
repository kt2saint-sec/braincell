# SPDX-License-Identifier: AGPL-3.0-or-later
"""Release baseline benchmark for connected-Project and explicit Pool reads.

The script builds disposable Project databases under a temporary XDG data home,
seeds deterministic local vectors, and times the same Store and explicit named
Pool query paths used by BrainCell. It does not require Ollama or hosted
embeddings.

Run:
  .venv/bin/python scripts/pool_bench.py --iterations 30 --notes 40 --chunks 40
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[index]


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "min_ms": round(min(values), 3),
        "median_ms": round(statistics.median(values), 3),
        "p95_ms": round(_percentile(values, 95), 3),
        "max_ms": round(max(values), 3),
    }


def _unit_vec(label: str, dim: int) -> np.ndarray:
    digest = hashlib.sha256(label.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big", signed=False)
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(dim).astype(np.float32)
    return vec / np.linalg.norm(vec)


def _children(pid: int) -> list[int]:
    try:
        raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8")
    except OSError:
        return []
    return [int(item) for item in raw.split() if item.strip()]


def _measure_command(
    argv: list[str],
    *,
    env: dict[str, str],
    stdin: bytes | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    start = time.perf_counter()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if stdin is not None and proc.stdin is not None:
        proc.stdin.write(stdin)
        proc.stdin.close()
        proc.stdin = None
    max_children = 0
    while proc.poll() is None:
        max_children = max(max_children, len(_children(proc.pid)))
        if time.perf_counter() - start > timeout:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=5)
            return {
                "argv": argv,
                "returncode": proc.returncode,
                "elapsed_ms": round((time.perf_counter() - start) * 1000.0, 3),
                "max_child_processes": max_children,
                "timed_out": True,
                "stdout_bytes": len(stdout),
                "stderr_bytes": len(stderr),
            }
        time.sleep(0.005)
    stdout, stderr = proc.communicate(timeout=5)
    return {
        "argv": argv,
        "returncode": proc.returncode,
        "elapsed_ms": round((time.perf_counter() - start) * 1000.0, 3),
        "max_child_processes": max_children,
        "timed_out": False,
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
    }


def _measure_long_running_command(
    argv: list[str],
    *,
    env: dict[str, str],
    port: int,
    token: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    start = time.perf_counter()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    max_children = 0
    ready_ms: float | None = None
    status_url = f"http://127.0.0.1:{port}/api/status"
    while proc.poll() is None and time.perf_counter() - start < timeout:
        max_children = max(max_children, len(_children(proc.pid)))
        try:
            request = urllib.request.Request(
                status_url, headers={"X-BrainCell-Token": token}
            )
            urllib.request.urlopen(request, timeout=0.2).close()
            ready_ms = (time.perf_counter() - start) * 1000.0
            break
        except (OSError, TimeoutError, urllib.error.URLError):
            time.sleep(0.05)
    proc.terminate()
    try:
        stdout, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
    return {
        "argv": argv,
        "returncode": proc.returncode,
        "ready_ms": round(ready_ms, 3) if ready_ms is not None else None,
        "max_child_processes": max_children,
        "stdout_bytes": len(stdout or ""),
        "stderr_bytes": len(stderr or ""),
        "safe_measurement": ready_ms is not None,
    }


async def _seed_project(root: Path, index: int, notes: int, chunks: int) -> str:
    from braincell import embed_spec
    from braincell.config import get_db_path, get_project_id
    from braincell.store import SqliteStore

    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    project_id = get_project_id(root)
    db = get_db_path(project_id)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.assert_schema_version()
    for note_index in range(notes):
        text = (
            f"pool baseline member {index} note {note_index} "
            f"release rollback guardrail latency cache"
        )
        await store.remember(
            text,
            "note",
            project_id,
            embedding=_unit_vec(f"note:{index}:{note_index}", embed_spec.DIM),
        )
    for chunk_index in range(chunks):
        text = (
            f"pool baseline member {index} chunk {chunk_index} "
            f"deployment rollback search latency cache"
        )
        await store.replace_document(
            project_id=project_id,
            doc_key=f"doc-{index}-{chunk_index}",
            title=f"Document {index}-{chunk_index}",
            content_hash=hashlib.sha256(text.encode("utf-8")).digest(),
            content_type="text/plain",
            chunks=[(text, _unit_vec(f"chunk:{index}:{chunk_index}", embed_spec.DIM))],
        )
    await store.aclose()
    return project_id


async def _measure_queries(
    *,
    projects: list[tuple[Path, str]],
    iterations: int,
    member_counts: list[int],
) -> dict[str, Any]:
    from braincell import embed_spec
    from braincell.config import get_db_path
    from braincell.federate import federated_recall, federated_search, plan_for_pool
    from braincell.project_registry import add_to_pool, create_pool
    from braincell.store import SqliteStore

    query = "release rollback latency cache"
    qvec = _unit_vec(f"query:{query}", embed_spec.DIM)
    connected_root, connected_project_id = projects[0]
    connected_store = SqliteStore(get_db_path(connected_project_id))
    try:
        cold_start = time.perf_counter()
        await connected_store.recall(qvec, connected_project_id, 10, qtext=query, rerank=False)
        connected_recall_cold = (time.perf_counter() - cold_start) * 1000.0

        recall_warm = []
        for _ in range(iterations):
            start = time.perf_counter()
            await connected_store.recall(qvec, connected_project_id, 10, qtext=query, rerank=False)
            recall_warm.append((time.perf_counter() - start) * 1000.0)

        cold_start = time.perf_counter()
        await connected_store.search(qvec, query, connected_project_id, 10, "hybrid", rerank=False)
        connected_search_cold = (time.perf_counter() - cold_start) * 1000.0

        search_warm = []
        for _ in range(iterations):
            start = time.perf_counter()
            await connected_store.search(qvec, query, connected_project_id, 10, "hybrid", rerank=False)
            search_warm.append((time.perf_counter() - start) * 1000.0)
    finally:
        await connected_store.aclose()

    pools: dict[str, Any] = {}
    for member_count in member_counts:
        pool_name = f"baseline-{member_count}"
        create_pool(pool_name)
        add_to_pool(pool_name, [project_id for _root, project_id in projects[:member_count]])
        plan = plan_for_pool(pool_name, connected_project_id)
        recall_times = []
        search_times = []
        for _ in range(iterations):
            start = time.perf_counter()
            await federated_recall(None, plan, qvec, 10, qtext=query)
            recall_times.append((time.perf_counter() - start) * 1000.0)
            start = time.perf_counter()
            await federated_search(None, plan, qvec, query, 10, "hybrid")
            search_times.append((time.perf_counter() - start) * 1000.0)
        pools[str(member_count)] = {
            "members": member_count,
            "ready_members": len(plan.targets),
            "recall": _summary(recall_times),
            "search": _summary(search_times),
        }

    return {
        "connected_project": {
            "project_id": connected_project_id,
            "path": str(connected_root),
            "recall_cold_ms": round(connected_recall_cold, 3),
            "recall_warm": _summary(recall_warm),
            "search_cold_ms": round(connected_search_cold, 3),
            "search_warm": _summary(search_warm),
        },
        "pools": pools,
    }


def _hook_evidence(repo_root: Path) -> dict[str, Any]:
    hook_path = repo_root / "braincell" / "family_hook.py"
    install_path = repo_root / "braincell" / "install.py"
    text = install_path.read_text(encoding="utf-8")
    installed_by_setup = "family_hook" in text and "_HOOK_MARKER" in text
    return {
        "status": "n/a",
        "reason": (
            "The retired hook entry point is compatibility-only and normal setup "
            "does not install runtime memory work."
        ),
        "source_files": [str(hook_path), str(install_path)],
        "compatibility_hook_module_exists": hook_path.is_file(),
        "install_module_mentions_legacy_marker": installed_by_setup,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--notes", type=int, default=40)
    parser.add_argument("--chunks", type=int, default=40)
    parser.add_argument("--members", type=int, default=16)
    parser.add_argument("--port", type=int, default=8976)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    workspace = Path(tempfile.mkdtemp(prefix="braincell-pool-bench-"))
    os.environ["XDG_DATA_HOME"] = str(workspace / "xdg")
    os.environ["BRAINCELL_DATA_NAMESPACE"] = "braincell_pool_bench"
    os.environ["BRAINCELL_RERANK"] = "off"

    async def run() -> dict[str, Any]:
        projects = []
        for index in range(args.members):
            root = workspace / f"project-{index:02d}"
            project_id = await _seed_project(root, index, args.notes, args.chunks)
            projects.append((root, project_id))
        timings = await _measure_queries(
            projects=projects,
            iterations=args.iterations,
            member_counts=[1, 4, 8, 16],
        )
        return {"projects": projects, "timings": timings}

    run_result = asyncio.run(run())
    connected_project_id = run_result["projects"][0][1]
    connected_project_path = str(run_result["projects"][0][0])
    env = os.environ.copy()
    env["PATH"] = f"{repo_root / '.venv' / 'bin'}:{env.get('PATH', '')}"
    env["BRAINCELL_PROJECT_ID"] = connected_project_id
    env["BRAINCELL_STORE"] = "sqlite"
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QTWEBENGINE_CHROMIUM_FLAGS"] = "--no-sandbox"

    console = {
        "braincell_help_cold": _measure_command(
            [str(repo_root / ".venv" / "bin" / "braincell"), "--help"], env=env
        ),
        "bound_mcp_startup": _measure_command(
            [str(repo_root / ".venv" / "bin" / "braincell-mcp")], env=env, stdin=b""
        ),
    }
    from braincell.gui import _resolve_gui_token

    console["native_gui_startup"] = _measure_long_running_command(
        [str(repo_root / ".venv" / "bin" / "braincell"), "start", connected_project_path, "--port", str(args.port)],
        env=env,
        port=args.port,
        token=_resolve_gui_token(),
    )

    payload = {
        "metadata": {
            "command": sys.argv,
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "workspace": str(workspace),
            "iterations": args.iterations,
            "notes_per_project": args.notes,
            "chunks_per_project": args.chunks,
            "members_seeded": args.members,
        },
        "console_and_processes": console,
        "query_timings": run_result["timings"],
        "hook_overhead": _hook_evidence(repo_root),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
