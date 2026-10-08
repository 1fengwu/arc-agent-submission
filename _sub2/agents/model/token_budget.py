"""Cross-process token budget guard.

Why this exists
---------------
The ARC compile runs up to 4 OS-process shards per task (GitHub and Sheet are
each launched as a separate top-level `main.py`, which spawns 4 shard
subprocesses). Every shard calls the model independently, so a naive per-process
counter would let total spend blow up by 4-8x.

We therefore keep a SINGLE shared counter per task in a JSON file under the
task's `.parallel/` directory. Every LLM call (in any shard) atomically
increments it via an `fcntl.flock` lock. When the counter reaches the budget
limit, the next call raises `TokenBudgetExceeded`, which the workflow catches
to stop spawning new leaves and finalize (the parallel merge keeps whatever
additive artifacts were already produced -- partial work is never thrown away).

Budget is configured entirely through environment variables:
  * ARC_TOKEN_BUDGET        total tokens allowed for THIS task (across its shards)
  * ARC_TOKEN_BUDGET_FILE   path to the shared JSON counter (set by
                            parallel_compile.py so all 4 shards of a task share
                            one counter). When absent but ARC_TOKEN_BUDGET is set,
                            a single-process in-memory counter is used instead.

The default per-task budget targets ~VOLO-AI's leaderboard spend. At the
observed platform price (~0.2645 CNY / 1M tokens), 26M tokens ~= 6.88 CNY per
task, i.e. ~13.76 CNY for both tasks -- matching VOLO-AI's ~13.99 CNY row.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

try:  # fcntl is Unix-only; the grading platform is Linux.
    import fcntl

    _HAVE_FCNTL = True
except Exception:  # pragma: no cover - Windows dev boxes
    _HAVE_FCNTL = False

# ~VOLO-AI per-task target. Two tasks => ~52M total => ~13.8 CNY.
DEFAULT_PER_TASK_BUDGET = 26_000_000

_lock = threading.Lock()
_state: dict[str, Any] = {
    "mode": "off",  # off | mem | file
    "file": None,
    "limit": 0,
    "used": 0,
    "inited": False,
}


class TokenBudgetExceeded(RuntimeError):
    """Raised when the shared token budget is exhausted."""

    def __init__(self, used: int, limit: int) -> None:
        super().__init__(
            f"Token budget exceeded: used={used} limit={limit} "
            f"(set ARC_TOKEN_BUDGET higher to allow more, or 0 to disable)"
        )
        self.used = used
        self.limit = limit


def configure_from_env() -> None:
    """Read budget config from the environment exactly once per process."""
    if _state["inited"]:
        return
    with _lock:
        if _state["inited"]:
            return
        f = os.environ.get("ARC_TOKEN_BUDGET_FILE", "").strip()
        # v20m (user): the token budget CAP IS DELETED. We never stop early and
        # never skip leaves, so the limit is hard-pinned to 0 (unlimited) and the
        # mode is forced to "off". Env values (ARC_TOKEN_BUDGET / _FILE) are
        # intentionally ignored -- no env override can ever re-arm a cap. With
        # mode "off", record() is a no-op and get_usage() reports limit 0, so the
        # workflow's partial-leaf skip (core/workflow.py) and arc_main's
        # "stopping early" can never fire either.
        _state["mode"] = "off"
        _state["file"] = f or None
        _state["limit"] = 0
        _state["inited"] = True


def _ensure_file(path: str, limit: int) -> None:
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"used": 0, "limit": limit}, fh)
        except Exception:
            pass


def record(prompt_tokens: int, completion_tokens: int) -> None:
    """Record token usage; raise TokenBudgetExceeded if the budget is now exceeded."""
    configure_from_env()
    if _state["mode"] == "off":
        return
    add = (prompt_tokens or 0) + (completion_tokens or 0)
    if add <= 0:
        return

    if _state["mode"] == "mem":
        with _lock:
            _state["used"] += add
            used = _state["used"]
            limit = _state["limit"]
        if limit and used >= limit:
            raise TokenBudgetExceeded(used, limit)
        return

    # file mode: atomic read-modify-write under an exclusive lock
    path = _state["file"]
    limit = _state["limit"]
    _ensure_file(path, limit)
    for _ in range(20):
        try:
            with open(path, "a+", encoding="utf-8") as fh:
                if _HAVE_FCNTL:
                    fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    fh.seek(0)
                    try:
                        data = json.load(fh)
                    except Exception:
                        data = {"used": 0, "limit": limit}
                    used = int(data.get("used", 0)) + add
                    data["used"] = used
                    fh.seek(0)
                    fh.truncate()
                    json.dump(data, fh)
                finally:
                    if _HAVE_FCNTL:
                        fcntl.flock(fh, fcntl.LOCK_UN)
            if limit and used >= limit:
                raise TokenBudgetExceeded(used, limit)
            return
        except TokenBudgetExceeded:
            raise
        except Exception:
            # transient IO/lock failure -> retry; never block the run on it
            continue
    # if the file keeps failing, do not hard-fail the whole run
    return


def get_usage() -> dict[str, Any]:
    configure_from_env()
    if _state["mode"] == "file" and _state["file"]:
        try:
            with open(_state["file"], "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return {"mode": "file", "used": data.get("used", 0), "limit": data.get("limit", _state["limit"])}
        except Exception:
            pass
    with _lock:
        return {"mode": _state["mode"], "used": _state["used"], "limit": _state["limit"]}
