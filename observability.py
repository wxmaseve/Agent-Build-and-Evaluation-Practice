"""Observation — 에이전트 실행 기록(trace)을 남기는 콜백.

에이전트가 사용자 요청 1건을 처리하는 동안(LLM 호출 → tool 호출 → … → 최종 답변)
각 단계를 이벤트로 기록한다. 외부 서비스 없이도 동작하도록 로컬 JSONL 로 남기고,
LANGSMITH_TRACING=true + LANGSMITH_API_KEY 가 있으면 LangSmith 에도 자동으로 올라간다
(LangSmith 는 langchain 이 env 만으로 켜 주므로 여기서는 상태만 알린다).

기록 위치: <workspace>/traces/YYYY-MM-DD/<HHMMSS>_<trace_id8>.jsonl
  - 한 줄 = 한 이벤트(llm_end / llm_error / tool_end / tool_error / trace_end)
  - 마지막 줄 trace_end 에 요청 단위 요약이 들어간다:
      latency, LLM 호출 수, 토큰(입력/출력), 추정 비용, 도구별 호출 수·지연·오류,
      그리고 '작업 방식의 품질' 점검 플래그(같은 도구·인자 반복, 실패한 도구 재시도 등).

에이전트 자신도 `/traces/` 를 read_file/grep 으로 읽을 수 있고, meta-harness 는 이 폴더를
실행 산출물로 캡처해 baseline/variant 비교 근거로 쓴다.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

# 미리보기로 남길 최대 글자 수(입력/출력 전문을 다 남기면 trace 가 비대해진다).
_PREVIEW_CHARS = 400


def _preview(value: Any, limit: int = _PREVIEW_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit})"


def _usage_of(response) -> dict:
    """LLMResult 에서 토큰 사용량을 꺼낸다(모델/프로바이더마다 위치가 달라 둘 다 본다)."""
    try:
        msg = response.generations[0][0].message
        usage = getattr(msg, "usage_metadata", None)
        if usage:
            return {"input": int(usage.get("input_tokens") or 0),
                    "output": int(usage.get("output_tokens") or 0)}
    except (AttributeError, IndexError, TypeError):
        pass
    usage = (response.llm_output or {}).get("token_usage") or {}
    return {"input": int(usage.get("prompt_tokens") or 0),
            "output": int(usage.get("completion_tokens") or 0)}


def _price(name: str) -> float:
    try:
        return float(os.getenv(name, "0") or 0)
    except ValueError:
        return 0.0


class LocalTraceHandler(BaseCallbackHandler):
    """요청(루트 실행) 단위로 이벤트를 모아 JSONL 로 쓰는 콜백 핸들러.

    langgraph 는 노드/도구를 스레드·코루틴에서 섞어 돌리므로 모든 상태는 락으로 보호한다.
    루트 실행(parent_run_id 가 없는 chain)이 하나의 trace 가 된다.
    """

    # 동기 핸들러를 이벤트 루프 스레드에서 바로 실행(순서 보존, 실행기 오버헤드 없음).
    run_inline = True

    def __init__(self, trace_dir: Path):
        self.trace_dir = Path(trace_dir)
        self._lock = threading.Lock()
        self._parent: dict[UUID, UUID | None] = {}
        self._started: dict[UUID, float] = {}
        self._tool_meta: dict[UUID, dict] = {}
        self._traces: dict[UUID, dict] = {}
        self._price_in = _price("OBS_PRICE_IN_PER_M")    # USD / 1M input tokens
        self._price_out = _price("OBS_PRICE_OUT_PER_M")  # USD / 1M output tokens

    # ------------------------------------------------------------------ 내부 유틸
    def _root_of(self, run_id: UUID) -> UUID:
        cur = run_id
        for _ in range(200):  # 깊이 방어
            parent = self._parent.get(cur)
            if parent is None:
                return cur
            cur = parent
        return cur

    def _register(self, run_id: UUID, parent_run_id: UUID | None) -> None:
        self._parent[run_id] = parent_run_id
        self._started[run_id] = time.time()

    def _elapsed(self, run_id: UUID) -> float:
        return round(time.time() - self._started.pop(run_id, time.time()), 3)

    def _emit(self, run_id: UUID, event: dict) -> None:
        trace = self._traces.get(self._root_of(run_id))
        if trace is None:
            return
        trace["events"].append({"ts": datetime.now().isoformat(timespec="milliseconds"), **event})

    # ------------------------------------------------------------------ 루트(요청)
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        with self._lock:
            self._register(run_id, parent_run_id)
            if parent_run_id is None:
                query = ""
                msgs = inputs.get("messages") if isinstance(inputs, dict) else None
                if msgs:
                    last = msgs[-1]
                    query = last.get("content") if isinstance(last, dict) else getattr(last, "content", "")
                self._traces[run_id] = {
                    "trace_id": str(run_id),
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "t0": time.time(),
                    "query": _preview(query or "", 300),
                    "events": [],
                }

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
        if parent_run_id is None:
            self._finish(run_id, error=None)
        else:
            with self._lock:
                self._started.pop(run_id, None)

    def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        if parent_run_id is None:
            self._finish(run_id, error=f"{type(error).__name__}: {error}")
        else:
            with self._lock:
                self._started.pop(run_id, None)

    # ------------------------------------------------------------------ LLM
    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        with self._lock:
            self._register(run_id, parent_run_id)

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kwargs):
        with self._lock:
            self._register(run_id, parent_run_id)

    def on_llm_end(self, response, *, run_id, **kwargs):
        with self._lock:
            usage = _usage_of(response)
            tool_calls = []
            try:
                msg = response.generations[0][0].message
                tool_calls = [tc.get("name") for tc in (getattr(msg, "tool_calls", None) or [])]
            except (AttributeError, IndexError, TypeError):
                pass
            self._emit(run_id, {
                "type": "llm_end",
                "latency_s": self._elapsed(run_id),
                "tokens_in": usage["input"],
                "tokens_out": usage["output"],
                "requested_tools": tool_calls,
            })

    def on_llm_error(self, error, *, run_id, **kwargs):
        with self._lock:
            self._emit(run_id, {"type": "llm_error", "latency_s": self._elapsed(run_id),
                                "error": f"{type(error).__name__}: {_preview(str(error))}"})

    # ------------------------------------------------------------------ Tool
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, inputs=None, **kwargs):
        with self._lock:
            self._register(run_id, parent_run_id)
            name = (serialized or {}).get("name") or kwargs.get("name") or "?"
            args = inputs if inputs is not None else input_str
            self._tool_meta[run_id] = {
                "name": name,
                "args": args,
                "sig": f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)}",
            }

    def on_tool_end(self, output, *, run_id, **kwargs):
        with self._lock:
            meta = self._tool_meta.pop(run_id, {"name": "?", "args": None, "sig": "?"})
            text = getattr(output, "content", output)
            text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
            # deepagents 의 execute 등은 실패를 예외가 아니라 출력으로 돌려준다 → 출력으로도 판정.
            soft_error = text.lstrip().lower().startswith(("error", "traceback")) or "exit code: 1" in text.lower()
            self._emit(run_id, {
                "type": "tool_end",
                "tool": meta["name"],
                "latency_s": self._elapsed(run_id),
                "args": _preview(meta["args"]),
                "output": _preview(text),
                "output_chars": len(text),
                "soft_error": soft_error,
                "sig": meta["sig"],
            })

    def on_tool_error(self, error, *, run_id, **kwargs):
        with self._lock:
            meta = self._tool_meta.pop(run_id, {"name": "?", "args": None, "sig": "?"})
            self._emit(run_id, {
                "type": "tool_error",
                "tool": meta["name"],
                "latency_s": self._elapsed(run_id),
                "args": _preview(meta["args"]),
                "error": f"{type(error).__name__}: {_preview(str(error))}",
                "sig": meta["sig"],
            })

    # ------------------------------------------------------------------ 요약·기록
    def _finish(self, root_id: UUID, error: str | None) -> None:
        with self._lock:
            trace = self._traces.pop(root_id, None)
            self._started.pop(root_id, None)
            # 이 trace 에 속한 부모 맵 정리(메모리 누수 방지).
            for rid in [r for r in self._parent if self._root_of(r) == root_id]:
                self._parent.pop(rid, None)
        if trace is None:
            return
        summary = summarize(trace["events"], self._price_in, self._price_out)
        summary.update({
            "type": "trace_end",
            "trace_id": trace["trace_id"],
            "started_at": trace["started_at"],
            "query": trace["query"],
            "latency_s": round(time.time() - trace["t0"], 3),
            "error": error,
        })
        day = datetime.now().strftime("%Y-%m-%d")
        out_dir = self.trace_dir / day
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{datetime.now():%H%M%S}_{trace['trace_id'][:8]}.jsonl"
            with path.open("w", encoding="utf-8") as f:
                for ev in trace["events"]:
                    f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
                f.write(json.dumps(summary, ensure_ascii=False, default=str) + "\n")
        except OSError as e:  # 기록 실패가 에이전트 응답을 막아선 안 된다
            print(f"[observability] trace 기록 실패(무시): {e}")


def summarize(events: list[dict], price_in: float = 0.0, price_out: float = 0.0) -> dict:
    """이벤트 목록을 요청 단위 지표 + 작업 방식 점검 플래그로 요약한다."""
    llm = [e for e in events if e["type"] == "llm_end"]
    llm_err = [e for e in events if e["type"] == "llm_error"]
    tools = [e for e in events if e["type"] in ("tool_end", "tool_error")]

    tokens_in = sum(e["tokens_in"] for e in llm)
    tokens_out = sum(e["tokens_out"] for e in llm)
    by_tool: dict[str, dict] = {}
    for e in tools:
        s = by_tool.setdefault(e["tool"], {"calls": 0, "errors": 0, "latency_s": 0.0})
        s["calls"] += 1
        s["latency_s"] = round(s["latency_s"] + e["latency_s"], 3)
        if e["type"] == "tool_error" or e.get("soft_error"):
            s["errors"] += 1

    # 작업 방식의 품질 점검(Day2 Observation 관찰 포인트를 규칙으로 옮김)
    sig_count: dict[str, int] = {}
    for e in tools:
        sig_count[e["sig"]] = sig_count.get(e["sig"], 0) + 1
    repeated = {s.split(":", 1)[0]: n for s, n in sig_count.items() if n >= 2}
    failed_sigs = {e["sig"] for e in tools if e["type"] == "tool_error" or e.get("soft_error")}
    retried_failures = sum(1 for s in failed_sigs if sig_count.get(s, 0) >= 2)
    first_tool_names = [e["tool"] for e in tools]
    planned_first = bool(first_tool_names) and first_tool_names[0] == "write_todos"

    flags = []
    if repeated:
        flags.append(f"같은 도구·인자 반복 호출: {repeated}")
    if retried_failures:
        flags.append(f"실패한 도구 호출을 같은 인자로 재시도: {retried_failures}건")
    if llm_err:
        flags.append(f"LLM 호출 오류 {len(llm_err)}건")
    if by_tool.get("task", {}).get("calls", 0) > 3:
        flags.append(f"subagent(task) 위임 과다: {by_tool['task']['calls']}회")

    return {
        "llm_calls": len(llm),
        "llm_latency_s": round(sum(e["latency_s"] for e in llm), 3),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "est_cost_usd": round(tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out, 6),
        "tool_calls": len(tools),
        "tool_errors": sum(s["errors"] for s in by_tool.values()),
        "tools": by_tool,
        "planned_with_todos": planned_first,
        "repeated_calls": repeated,
        "retried_failures": retried_failures,
        "flags": flags,
    }


def build_callbacks(workspace: Path) -> list:
    """하네스에 붙일 콜백 목록. OBSERVABILITY=0 이면 로컬 trace 를 끈다."""
    callbacks: list = []
    if os.getenv("OBSERVABILITY", "1").strip().lower() not in {"0", "false", "off", "no"}:
        callbacks.append(LocalTraceHandler(Path(workspace) / "traces"))
        print(f"[observability] 로컬 trace 활성화 → {Path(workspace) / 'traces'}")
    tracing = (os.getenv("LANGSMITH_TRACING") or os.getenv("LANGCHAIN_TRACING_V2") or "").lower() == "true"
    if tracing and (os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")):
        project = os.getenv("LANGSMITH_PROJECT") or os.getenv("LANGCHAIN_PROJECT") or "default"
        print(f"[observability] LangSmith tracing 활성화 (project={project})")
    return callbacks
