#!/usr/bin/env python
"""eval_suite: '질문-평가기준 세트'로 meta-harness variant 를 일괄 실행·채점·비교한다.

metaharness.py 는 질의 1건 단위로 variant 를 격리 실행한다. 이 스크립트는 그 위에서
세트(JSON)의 모든 사례를 같은 variant 로 실행하고(반복 가능), 각 실행을

  1) 룰 기반 검사(rules)  — 파일 존재, 정규식, 링크 근거성, 링크 수, Observation 지표 …
  2) LLM-as-a-Judge(judge) — 기준별 True/False + 근거 한 줄

로 채점해 사례별·세트 전체 점수를 남긴다. 두 variant 를 compare 하면 사례별 차이와
회귀를 표로 보여주고 판정을 '제안'한다(기본값 tie — 최종 판정은 실행기록을 읽고 내린다).

사용법 (workspace 의 execute 셸 또는 레포 루트에서):

  python skills/meta-harness/eval_suite.py run     --variant baseline --suite <set.json> [--cases core-01,bnd-01] [--repeat 2] [--jobs 3]
  python skills/meta-harness/eval_suite.py grade   --variant baseline --suite <set.json>     # 저장된 실행 재채점
  python skills/meta-harness/eval_suite.py report  --variant baseline --suite <set.json>
  python skills/meta-harness/eval_suite.py compare --a baseline --b v1 --suite <set.json>

--suite 를 생략하면 레포 루트의 evals/*.json 중 첫 번째를 쓴다.
결과는 meta-harness 홈의 suites/<세트이름>/<variant>/<case>/r<k>/ 에 쌓인다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metaharness as mh  # noqa: E402

URL_RE = re.compile(r"https?://[^\s)\]>\"'`|]+")
DEFAULT_JUDGE_MODEL = "z-ai/glm-5.3"


# ---------------------------------------------------------------------------
# 경로
# ---------------------------------------------------------------------------

def _suite_path(repo: Path, arg: str | None) -> Path:
    if arg:
        p = Path(arg)
        return p if p.is_absolute() else (Path.cwd() / p if (Path.cwd() / p).exists() else repo / p)
    found = sorted((repo / "evals").glob("*.json"))
    if not found:
        mh.die("평가 세트를 찾지 못했습니다. --suite 로 지정하세요.")
    return found[0]


def _load_suite(path: Path) -> dict:
    suite = json.loads(path.read_text(encoding="utf-8"))
    ids = [c["id"] for c in suite["cases"]]
    if len(ids) != len(set(ids)):
        mh.die("세트에 중복된 case id 가 있습니다.")
    return suite


def _suite_root(home: Path, suite: dict, variant: str) -> Path:
    return home / "suites" / suite["name"] / variant


def _pick_cases(suite: dict, arg: str | None) -> list[dict]:
    if not arg:
        return suite["cases"]
    want = [x.strip() for x in arg.split(",") if x.strip()]
    by_id = {c["id"]: c for c in suite["cases"]}
    missing = [w for w in want if w not in by_id]
    if missing:
        mh.die(f"세트에 없는 case: {missing}")
    return [by_id[w] for w in want]


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

def _run_one(repo: Path, vp: Path, case: dict, out: Path, timeout: int, rl: int) -> tuple[str, int]:
    if out.exists():
        mh.shutil.rmtree(out)
    out.mkdir(parents=True)
    qf = out / "query.txt"
    qf.write_text(case["query"], encoding="utf-8")
    rc, log = mh._run_headless_subprocess(repo, vp, out / "_ws", out, qf, rl, timeout)
    (out / "run.log").write_text(log, encoding="utf-8")
    # 격리 워크스페이스는 산출물(artifacts/)로 이미 캡처됐으므로 지워 용량을 아낀다.
    mh.shutil.rmtree(out / "_ws", ignore_errors=True)
    return f"{case['id']}/{out.name}", rc


def cmd_run(args) -> int:
    repo = mh.find_repo_root()
    home = mh.home_dir(repo, args.home)
    suite_file = _suite_path(repo, args.suite)
    suite = _load_suite(suite_file)
    cases = _pick_cases(suite, args.cases)
    vp = mh.variant_path(home, args.variant)
    if not vp.exists():
        if args.variant != "baseline":
            mh.die(f"variant '{args.variant}' 가 없습니다. metaharness.py init/fork 로 먼저 만드세요.")
        (home / "variants").mkdir(parents=True, exist_ok=True)
        (home / "runs").mkdir(parents=True, exist_ok=True)
        mh.make_variant(repo, home, "baseline", src=None, live=False)

    root = _suite_root(home, suite, args.variant)
    jobs = []
    for case in cases:
        for k in range(1, args.repeat + 1):
            jobs.append((case, root / case["id"] / f"r{k}"))
    print(f"[suite] {suite['name']} · variant={args.variant} · {len(cases)} cases × {args.repeat} = {len(jobs)} runs (jobs={args.jobs})")

    # 실행 중 에이전트가 메모리/스킬을 고쳐도 variant 소스가 오염되지 않게 전체 실행 뒤 원복.
    before = mh._source_files(vp)
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        futs = [ex.submit(_run_one, repo, vp, c, o, args.timeout, args.recursion_limit) for c, o in jobs]
        for f in futs:
            tag, rc = f.result()
            print(f"[suite] done {tag} rc={rc}")
    reverted = mh._restore_sources(vp, before)
    if reverted:
        print(f"[suite] 실행 중 바뀐 variant 소스를 되돌렸습니다: {reverted}")

    if not args.no_grade:
        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(lambda j: _grade_run(j[0], j[1], args.judge_model), jobs))
        _write_report(suite, args.variant, root)
        print((root / "report.md").read_text(encoding="utf-8"))
    return 0


# ---------------------------------------------------------------------------
# 채점 — 입력 로딩
# ---------------------------------------------------------------------------

def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _load_run(out: Path) -> dict:
    art = out / "artifacts"
    files = sorted(str(p.relative_to(art)) for p in art.rglob("*") if p.is_file()) if art.exists() else []
    briefings = sorted(f for f in files if re.match(r"^briefings/\d{4}-\d{2}-\d{2}\.md$", f))
    raw = sorted(f for f in files if f.startswith("briefings/raw/") and f.endswith(".json"))
    messages = json.loads(_read(out / "messages.json") or "[]")
    tool_text = "\n".join(mh._text_of_content(m.get("content")) for m in messages if m.get("role") == "tool")

    obs: dict = {"tool_calls": 0, "tool_errors": 0, "retried_failures": 0, "llm_calls": 0,
                 "tokens_in": 0, "tokens_out": 0, "est_cost_usd": 0.0, "latency_s": 0.0, "flags": []}
    for f in files:
        if f.startswith("traces/") and f.endswith(".jsonl"):
            lines = _read(art / f).strip().splitlines()
            end = json.loads(lines[-1]) if lines else {}
            if end.get("type") != "trace_end":
                continue
            for k in ("tool_calls", "tool_errors", "retried_failures", "llm_calls", "tokens_in", "tokens_out"):
                obs[k] += int(end.get(k) or 0)
            obs["est_cost_usd"] = round(obs["est_cost_usd"] + float(end.get("est_cost_usd") or 0), 6)
            obs["latency_s"] = round(obs["latency_s"] + float(end.get("latency_s") or 0), 3)
            obs["flags"] += end.get("flags") or []
    obs["has_trace"] = any(f.startswith("traces/") for f in files)

    return {
        "files": files,
        "briefing": _read(art / briefings[-1]) if briefings else "",
        "briefing_path": briefings[-1] if briefings else None,
        "raw": "\n".join(_read(art / r) for r in raw),
        "answer": _read(out / "final_answer.md").replace("(no final answer)", ""),
        "tool_text": tool_text,
        "summary": json.loads(_read(out / "summary.json") or "{}"),
        "obs": obs,
        "art_dir": art,
    }


def _target(run: dict, target: str) -> str:
    if target == "answer":
        return run["answer"]
    if target == "briefing":
        return run["briefing"]
    return run["briefing"] + "\n\n" + run["answer"]


def _clean_url(u: str) -> str:
    return u.rstrip(".,;:!?*_~")


# ---------------------------------------------------------------------------
# 채점 — 룰 기반
# ---------------------------------------------------------------------------

def _check_rule(rule: dict, run: dict) -> tuple[bool, str]:
    c = rule["check"]
    if c == "artifact_exists":
        hits = [f for f in run["files"] if re.search(rule["pattern"], f)]
        return bool(hits), f"매칭 {hits[:3]}" if hits else "해당 파일 없음"
    if c == "artifact_absent":
        hits = [f for f in run["files"] if re.search(rule["pattern"], f)]
        return not hits, f"발견 {hits[:3]}" if hits else "없음(정상)"
    if c == "artifact_regex":
        text = _read(run["art_dir"] / rule["path"])
        ok = bool(text) and re.search(rule["pattern"], text, re.M | re.S) is not None
        return ok, f"{rule['path']}: {text[:80]!r}" if text else f"{rule['path']} 없음"
    if c == "regex":
        ok = re.search(rule["pattern"], _target(run, rule["target"])) is not None
        return ok, "매칭" if ok else "매칭 없음"
    if c == "all_regex":
        text = _target(run, rule["target"])
        miss = [p for p in rule["patterns"] if not re.search(p, text)]
        return not miss, "모두 매칭" if not miss else f"누락 패턴 {miss}"
    if c == "not_regex":
        m = re.search(rule["pattern"], _target(run, rule["target"]))
        return m is None, f"금지 표현 발견: {m.group(0)!r}" if m else "금지 표현 없음"
    if c == "distinct_links":
        text = _target(run, rule["target"])
        links = {_clean_url(m.group(0)) for m in re.finditer(rule["pattern"], text)}
        n = len(links)
        ok = rule.get("min", 0) <= n <= rule.get("max", 10**9)
        return ok, f"{n}건 (기준 {rule.get('min', 0)}~{rule.get('max', '∞')})"
    if c == "heading_count":
        # 항목 수는 링크 수가 아니라 항목 제목 수로 센다(중복 발표를 한 항목에 묶으면 링크가 2개일 수 있다).
        text = _target(run, rule["target"])
        n = len(re.findall(rule.get("pattern", r"^###\s+\S"), text, re.M))
        ok = rule.get("min", 0) <= n <= rule.get("max", 10**9)
        return ok, f"항목 {n}개 (기준 {rule.get('min', 0)}~{rule.get('max', '∞')})"
    if c == "no_duplicate_links":
        text = _target(run, rule["target"])
        # 같은 항목에서 '[제목](url)' 과 'url' 을 같이 쓰는 경우는 중복이 아니므로 줄 단위로 센다.
        per_line = [{_clean_url(u) for u in URL_RE.findall(line)} for line in text.splitlines()]
        counts: dict[str, int] = {}
        for s in per_line:
            for u in s:
                counts[u] = counts.get(u, 0) + 1
        dups = [u for u, n in counts.items() if n > 1]
        return not dups, f"중복 {dups[:3]}" if dups else "중복 없음"
    if c == "links_grounded":
        text = _target(run, rule["target"])
        if not text.strip():
            return False, "검사할 텍스트 없음"
        corpus = run["tool_text"] + "\n" + run["raw"]
        links = {_clean_url(u) for u in URL_RE.findall(text)}
        bad = [u for u in links if u not in corpus and u.rstrip("/") not in corpus]
        return not bad, f"근거 없는 링크 {len(bad)}/{len(links)}: {bad[:3]}" if bad else f"{len(links)}개 모두 수집 결과에 존재"
    if c == "hangul_ratio":
        text = URL_RE.sub("", _target(run, rule["target"]))
        han = len(re.findall(r"[가-힣]", text))
        lat = len(re.findall(r"[A-Za-z]", text))
        ratio = han / (han + lat) if han + lat else 0.0
        return ratio >= rule["min"], f"한글 비율 {ratio:.2f}"
    if c == "obs_max":
        if not run["obs"]["has_trace"]:
            return False, "trace 없음(Observation 미동작)"
        v = run["obs"].get(rule["field"], 0)
        return v <= rule["max"], f"{rule['field']}={v} (최대 {rule['max']})"
    return False, f"알 수 없는 check: {c}"


# ---------------------------------------------------------------------------
# 채점 — LLM-as-a-Judge
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = """너는 AI 에이전트 실행 결과를 채점하는 엄격한 평가자다.
주어진 각 평가 기준에 대해 True/False 로만 판정하고 근거를 한 문장으로 적는다.
- 증거(최종 답변·브리핑 파일·수집 후보)에 실제로 보이는 것만 근거로 삼는다.
- 애매하면 False 로 판정한다(관대하게 채점하지 않는다).
- 반드시 아래 JSON 만 출력한다:
{"results": [{"id": "<기준 id>", "pass": true|false, "reason": "<한 문장>"}]}"""


def _candidates_digest(run: dict, limit: int = 24000) -> str:
    try:
        items = []
        for chunk in re.split(r"\n(?=\{)", run["raw"].strip()):
            if chunk.strip():
                items += json.loads(chunk).get("items", [])
        text = "\n".join(f"- [{i.get('source')}] {i.get('title')} | {i.get('url')} | {i.get('summary', '')[:400]}" for i in items)
    except (ValueError, AttributeError):
        text = run["tool_text"]
    if not text.strip():
        text = run["tool_text"]
    return text[:limit] or "(수집 결과 없음)"


def _judge(case: dict, run: dict, model_id: str) -> dict:
    criteria = case.get("judge") or []
    if not criteria:
        return {}
    import httpx
    from langchain.chat_models import init_chat_model

    llm = init_chat_model(
        model=model_id, model_provider="openai",
        api_key=os.getenv("OPENAI_API_KEY"), base_url="https://openrouter.ai/api/v1",
        temperature=0, max_tokens=16000,
        # 추론형 judge 는 추론에 토큰을 다 써서 빈 응답을 낼 수 있다(실측: 8000 토큰 전부 reasoning).
        reasoning_effort=os.getenv("EVAL_JUDGE_REASONING", "low"),
        http_client=httpx.Client(verify=False, timeout=180),
    )
    user = f"""# 사용자 질의
{case['query']}

# 에이전트 최종 답변
{run['answer'][:6000] or '(없음)'}

# 저장된 브리핑 파일 ({run['briefing_path'] or '없음'})
{run['briefing'][:15000] or '(없음)'}

# [수집 후보] (에이전트가 도구로 받은 원본 목록)
{_candidates_digest(run)}

# 평가 기준
""" + "\n".join(f"- {c['id']}: {c['criterion']}" for c in criteria)
    # 판정 불가는 실패가 아니라 '미검증(None)'으로 남긴다 → 비교 시 inconclusive 로 처리.
    got: dict = {}
    last_err = ""
    for _ in range(3):
        try:
            resp = llm.invoke([("system", _JUDGE_SYSTEM), ("user", user)])
            text = mh._text_of_content(resp.content)
            m = re.search(r"\{.*\}", text, re.S)
            data = json.loads(m.group(0)) if m else {}
            for r in data.get("results", []):
                if r.get("id") in {c["id"] for c in criteria} and isinstance(r.get("pass"), bool):
                    got[r["id"]] = (r["pass"], str(r.get("reason", "")))
            if len(got) == len(criteria):
                break
            last_err = f"응답에서 {len(got)}/{len(criteria)}개 기준만 파싱(finish={resp.response_metadata.get('finish_reason')})"
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
    return {c["id"]: got.get(c["id"], (None, f"unverified — judge 실패: {last_err[:200]}")) for c in criteria}


def _grade_run(case: dict, out: Path, judge_model: str) -> dict:
    run = _load_run(out)
    results = []
    for rule in case.get("rules", []):
        ok, why = _check_rule(rule, run)
        results.append({"id": rule["id"], "kind": "rule", "desc": rule.get("desc", ""), "pass": ok, "detail": why})
    for cid, (ok, why) in _judge(case, run, judge_model).items():
        crit = next(c["criterion"] for c in case["judge"] if c["id"] == cid)
        results.append({"id": cid, "kind": "judge", "desc": crit, "pass": ok, "detail": why})
    required = set(case.get("required", []))
    req_fail = [r["id"] for r in results if r["id"] in required and r["pass"] is False]
    unverified = [r["id"] for r in results if r["pass"] is None]
    grade = {
        "case": case["id"],
        "type": case.get("type"),
        "run": out.name,
        "error": run["summary"].get("error"),
        "passed": not req_fail and not unverified and not run["summary"].get("error"),
        "required_failed": req_fail,
        "unverified": unverified,
        "score": round(sum(r["pass"] is True for r in results) / len(results), 3) if results else 0.0,
        "results": results,
        "obs": {k: run["obs"][k] for k in ("llm_calls", "tool_calls", "tool_errors", "retried_failures",
                                           "tokens_in", "tokens_out", "est_cost_usd", "latency_s", "flags")},
        "judge_model": judge_model,
    }
    (out / "grade.json").write_text(json.dumps(grade, ensure_ascii=False, indent=2), encoding="utf-8")
    return grade


def cmd_grade(args) -> int:
    repo = mh.find_repo_root()
    home = mh.home_dir(repo, args.home)
    suite = _load_suite(_suite_path(repo, args.suite))
    root = _suite_root(home, suite, args.variant)
    todo = [(case, out) for case in _pick_cases(suite, args.cases) for out in sorted((root / case["id"]).glob("r*"))]
    with ThreadPoolExecutor(max_workers=6) as ex:  # judge 호출이 병목이라 병렬로 채점
        for g in ex.map(lambda j: _grade_run(j[0], j[1], args.judge_model), todo):
            print(f"[grade] {g['case']}/{g['run']}: passed={g['passed']} score={g['score']}")
    _write_report(suite, args.variant, root)
    print((root / "report.md").read_text(encoding="utf-8"))
    return 0


# ---------------------------------------------------------------------------
# 리포트 & 비교
# ---------------------------------------------------------------------------

def _grades(root: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for g in sorted(root.glob("*/r*/grade.json")):
        d = json.loads(g.read_text(encoding="utf-8"))
        out.setdefault(d["case"], []).append(d)
    return out


def _write_report(suite: dict, variant: str, root: Path) -> None:
    grades = _grades(root)
    lines = [f"# 평가 리포트 — `{suite['name']}` · variant `{variant}`", ""]
    lines += ["| case | type | 통과(필수기준) | 점수(평균) | 필수 실패 | 도구호출 | 토큰(in/out) | 추정비용$ | 시간s |",
              "|---|---|---|---|---|---|---|---|---|"]
    total_pass = total_runs = 0
    for case in suite["cases"]:
        gs = grades.get(case["id"])
        if not gs:
            continue
        npass = sum(g["passed"] for g in gs)
        total_pass += npass
        total_runs += len(gs)
        mean = sum(g["score"] for g in gs) / len(gs)
        fails = sorted({f for g in gs for f in g["required_failed"]}
                       | {f"{u}(미검증)" for g in gs for u in g.get("unverified", [])}
                       | ({"run_error"} if any(g["error"] for g in gs) else set()))
        o = [g["obs"] for g in gs]
        lines.append(
            f"| {case['id']} | {case.get('type')} | {npass}/{len(gs)} | {mean:.2f} | {', '.join(fails) or '-'} | "
            f"{'/'.join(str(x['tool_calls']) for x in o)} | "
            f"{'/'.join(str(x['tokens_in']) + '·' + str(x['tokens_out']) for x in o)} | "
            f"{'/'.join(format(x['est_cost_usd'], '.3f') for x in o)} | {'/'.join(str(round(x['latency_s'])) for x in o)} |"
        )
    lines += ["", f"**세트 통과율: {total_pass}/{total_runs}**", "", "## 기준별 상세", ""]
    for case in suite["cases"]:
        for g in grades.get(case["id"], []):
            lines.append(f"### {case['id']} / {g['run']} — {'PASS' if g['passed'] else 'FAIL'} (score {g['score']})")
            if g["error"]:
                lines.append(f"- ⚠️ run error: {g['error']}")
            for r in g["results"]:
                mark = {True: "✅", False: "❌", None: "⚠️"}[r["pass"]]
                lines.append(f"- {mark} `{r['id']}` ({r['kind']}) {r['detail']}")
            if g["obs"]["flags"]:
                lines.append(f"- 🔎 Observation flags: {g['obs']['flags']}")
            lines.append("")
    (root / "report.md").write_text("\n".join(lines), encoding="utf-8")


def cmd_report(args) -> int:
    repo = mh.find_repo_root()
    home = mh.home_dir(repo, args.home)
    suite = _load_suite(_suite_path(repo, args.suite))
    root = _suite_root(home, suite, args.variant)
    _write_report(suite, args.variant, root)
    print((root / "report.md").read_text(encoding="utf-8"))
    return 0


def cmd_compare(args) -> int:
    repo = mh.find_repo_root()
    home = mh.home_dir(repo, args.home)
    suite = _load_suite(_suite_path(repo, args.suite))
    ga = _grades(_suite_root(home, suite, args.a))
    gb = _grades(_suite_root(home, suite, args.b))
    lines = [f"# 세트 비교 — `{args.a}` vs `{args.b}` (`{suite['name']}`)", "",
             f"| case | type | {args.a} 통과 | {args.b} 통과 | {args.a} 점수 | {args.b} 점수 | 변화 |",
             "|---|---|---|---|---|---|---|"]
    improved, regressed, incomplete = [], [], []
    for case in suite["cases"]:
        a, b = ga.get(case["id"], []), gb.get(case["id"], [])
        if not a or not b:
            incomplete.append(case["id"])
            continue
        pa, pb = sum(g["passed"] for g in a) / len(a), sum(g["passed"] for g in b) / len(b)
        sa, sb = [g["score"] for g in a], [g["score"] for g in b]
        # '확실한' 개선/회귀: 통과율이 달라지거나, 점수 범위가 겹치지 않을 때(반복 실행 전부에서 우위)
        if pb > pa or (pb == pa and min(sb) > max(sa)):
            change = "▲ 개선"
            improved.append(case["id"])
        elif pb < pa or (pb == pa and max(sb) < min(sa)):
            change = "▼ 회귀"
            regressed.append(case["id"])
        else:
            change = "= 동등/겹침"
        lines.append(f"| {case['id']} | {case.get('type')} | {sum(g['passed'] for g in a)}/{len(a)} | "
                     f"{sum(g['passed'] for g in b)}/{len(b)} | {'/'.join(map(str, sa))} | {'/'.join(map(str, sb))} | {change} |")
        # 기준 단위 변화
        def rate(gs, cid):
            v = [r["pass"] for g in gs for r in g["results"] if r["id"] == cid]
            return f"{sum(v)}/{len(v)}" if v else "-"
        ids = [r["id"] for r in (a[0]["results"])]
        diffs = [f"`{cid}` {rate(a, cid)}→{rate(b, cid)}" for cid in ids if rate(a, cid) != rate(b, cid)]
        if diffs:
            lines.append(f"|  | ↳ 기준 변화 | {' · '.join(diffs)} |  |  |  |  |")

    reps = min([len(v) for v in list(ga.values()) + list(gb.values())] or [0])
    unverified = sorted({g["case"] for gs in list(ga.values()) + list(gb.values()) for g in gs if g.get("unverified")})
    if unverified:
        incomplete += [f"{c}(미검증 기준)" for c in unverified]
    if incomplete:
        verdict = "inconclusive"
        why = f"한쪽 결과가 없는 사례: {incomplete}"
    elif regressed:
        verdict = "baseline_win" if not improved else "tie"
        why = f"회귀 사례 {regressed}" + (f" (개선 {improved} 와 혼재 → 무승부)" if improved else "")
    elif improved and reps >= 2:
        verdict = "candidate_win"
        why = f"개선 사례 {improved}, 회귀 없음, 반복 {reps}회 모두에서 유지"
    elif improved:
        verdict = "tie"
        why = f"개선 사례 {improved} 가 있으나 반복 실행 {reps}회 — 재현 확인 전까지 무승부"
    else:
        verdict = "tie"
        why = "결정적 차이 없음"
    lines += ["", f"## 제안 판정: `{verdict}`", f"- 근거: {why}",
              "- ※ 기본값은 tie. promote 전에 두 variant 의 transcript·브리핑을 직접 읽고 확정하라."]
    text = "\n".join(lines)
    out = _suite_root(home, suite, args.b).parent / f"compare_{args.a}_vs_{args.b}.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n(저장: {out})")
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", help="meta-harness 홈(기본: metaharness.py 와 동일).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    judge_default = os.getenv("EVAL_JUDGE_MODEL") or DEFAULT_JUDGE_MODEL

    sp = sub.add_parser("run", help="세트의 사례들을 variant 로 실행하고 채점.")
    sp.add_argument("--variant", default="baseline")
    sp.add_argument("--suite")
    sp.add_argument("--cases", help="쉼표로 구분한 case id(기본: 전체).")
    sp.add_argument("--repeat", type=int, default=1, help="사례별 반복 횟수(접전이면 2~3).")
    sp.add_argument("--jobs", type=int, default=1, help="동시 실행 수.")
    sp.add_argument("--timeout", type=int, default=600)
    sp.add_argument("--recursion-limit", type=int, default=80)
    sp.add_argument("--judge-model", default=judge_default)
    sp.add_argument("--no-grade", action="store_true")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("grade", help="저장된 실행을 다시 채점.")
    sp.add_argument("--variant", default="baseline")
    sp.add_argument("--suite")
    sp.add_argument("--cases")
    sp.add_argument("--judge-model", default=judge_default)
    sp.set_defaults(fn=cmd_grade)

    sp = sub.add_parser("report", help="variant 의 세트 리포트 출력.")
    sp.add_argument("--variant", default="baseline")
    sp.add_argument("--suite")
    sp.set_defaults(fn=cmd_report)

    sp = sub.add_parser("compare", help="두 variant 의 세트 결과 비교 + 판정 제안.")
    sp.add_argument("--a", required=True)
    sp.add_argument("--b", required=True)
    sp.add_argument("--suite")
    sp.set_defaults(fn=cmd_compare)

    args = ap.parse_args()
    # OPENAI_API_KEY 가 셸에 없으면 레포 .env 에서 읽는다(judge 호출용).
    if not os.getenv("OPENAI_API_KEY"):
        try:
            import dotenv
            dotenv.load_dotenv(mh.find_repo_root() / ".env")
        except ImportError:
            pass
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
