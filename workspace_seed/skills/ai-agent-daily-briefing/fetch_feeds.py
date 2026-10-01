#!/usr/bin/env python
"""GeekNews·AWS ML Blog RSS 를 수집해 브리핑 후보 목록(JSON)을 만든다.

표준 라이브러리만 쓴다(에이전트 `execute` 셸에서 바로 실행 가능).

    python skills/ai-agent-daily-briefing/fetch_feeds.py                 # 두 소스, 최근 48시간
    python skills/ai-agent-daily-briefing/fetch_feeds.py --source geeknews --since-hours 24
    python skills/ai-agent-daily-briefing/fetch_feeds.py --out briefings/raw/2026-10-01.json

출력: {"fetched_at", "sources": {이름: {"ok", "count", "error"}}, "items": [...]}
  item = {source, title, url, published, summary, keyword_hits}

- keyword_hits 는 '힌트'일 뿐이다. 최종 관련성 판단은 에이전트가 제목·요약을 읽고 한다
  (하드코딩 키워드로만 거르면 새 용어로 나온 발표를 놓친다).
- 수집은 소스당 1회 요청(저빈도). 상세 페이지 대량 크롤링은 하지 않는다.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

SOURCES = {
    "geeknews": "https://news.hada.io/rss/news",
    "aws-ml": "https://aws.amazon.com/blogs/machine-learning/feed/",
}

# 관련성 '힌트' 키워드(소문자 비교). 판단의 근거가 아니라 정렬·검토 보조용이다.
HINT_KEYWORDS = [
    "agent", "agentic", "에이전트", "mcp", "a2a", "tool use", "tool calling", "function calling",
    "multi-agent", "멀티 에이전트", "agentcore", "bedrock", "llm", "rag", "harness", "하네스",
    "copilot", "claude", "gpt", "gemini", "openai", "anthropic", "langgraph", "langchain",
    "deepagents", "autonomous", "workflow", "computer use", "coding agent", "코딩 에이전트",
]

USER_AGENT = "ai-agent-daily-briefing/1.0 (+personal daily digest; 1 request per source per day)"
ATOM = "{http://www.w3.org/2005/Atom}"


def _fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.URLError as e:
        # 사내 TLS 검사 프록시 환경 대비(하네스 본체도 같은 이유로 verify=False 를 쓴다).
        if isinstance(e.reason, ssl.SSLError):
            print(f"[fetch_feeds] SSL 검증 실패 → 검증 없이 재시도: {url}", file=sys.stderr)
            ctx = ssl._create_unverified_context()  # noqa: S323
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read()
        raise


def _clean(text: str | None, limit: int = 400) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _parse(source: str, raw: bytes) -> list[dict]:
    root = ET.fromstring(raw)
    items: list[dict] = []
    if root.tag == f"{ATOM}feed":  # Atom (GeekNews)
        for e in root.findall(f"{ATOM}entry"):
            # Element 의 truthiness 는 자식 수 기준이라 `or` 로 잇지 않는다.
            link = e.find(f"{ATOM}link[@rel='alternate']")
            if link is None:
                link = e.find(f"{ATOM}link")
            items.append({
                "source": source,
                "title": _clean(e.findtext(f"{ATOM}title"), 200),
                "url": (link.get("href") if link is not None else "") or "",
                "published": e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated"),
                "summary": _clean(e.findtext(f"{ATOM}content") or e.findtext(f"{ATOM}summary")),
            })
    else:  # RSS 2.0 (AWS Blog)
        for e in root.iter("item"):
            items.append({
                "source": source,
                "title": _clean(e.findtext("title"), 200),
                "url": (e.findtext("link") or "").strip(),
                "published": e.findtext("pubDate"),
                "summary": _clean(e.findtext("description")),
            })
    return items


def _norm_url(url: str) -> str:
    url = re.sub(r"[?#].*$", "", url) if "news.hada.io" not in url else url  # GeekNews 는 ?id= 가 본체
    return url.rstrip("/").lower()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    ap.add_argument("--since-hours", type=int, default=48, help="이 시간 안에 게시된 글만(기본 48).")
    ap.add_argument("--out", help="결과 JSON 저장 경로(없으면 stdout).")
    args = ap.parse_args()

    names = list(SOURCES) if args.source == "all" else [args.source]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.since_hours)
    status: dict[str, dict] = {}
    items: list[dict] = []
    seen: set[str] = set()

    for name in names:
        try:
            parsed = _parse(name, _fetch(SOURCES[name]))
        except Exception as e:  # noqa: BLE001 — 한 소스 실패가 전체를 막지 않게
            status[name] = {"ok": False, "count": 0, "error": f"{type(e).__name__}: {e}"}
            continue
        kept = 0
        for it in parsed:
            dt = _parse_date(it["published"])
            if dt is not None and dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt is not None and dt < cutoff:
                continue
            key = _norm_url(it["url"])
            if not it["url"] or key in seen:
                continue
            seen.add(key)
            it["published"] = dt.isoformat() if dt else it["published"]
            hay = f"{it['title']} {it['summary']}".lower()
            it["keyword_hits"] = [k for k in HINT_KEYWORDS if k in hay]
            items.append(it)
            kept += 1
        status[name] = {"ok": True, "count": kept, "fetched": len(parsed), "error": None}

    items.sort(key=lambda x: (len(x["keyword_hits"]), x["published"] or ""), reverse=True)
    result = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since_hours": args.since_hours,
        "sources": status,
        "items": items,
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(json.dumps({"saved": args.out, "sources": status, "items": len(items)}, ensure_ascii=False))
    else:
        print(text)
    return 0 if any(s["ok"] for s in status.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
