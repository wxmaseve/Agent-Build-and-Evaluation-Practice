---
name: ai-agent-daily-briefing
description: GeekNews·AWS 기술 블로그(AI/ML)에서 AI Agent 관련 최신 글을 수집·정제해 한국어 일일 브리핑 초안(제목+요약+링크+주목 포인트)을 만들고 briefings/YYYY-MM-DD.md 로 아카이빙한다. "AI Agent 브리핑", "오늘의 에이전트 소식", "데일리 브리핑", "GeekNews/AWS 블로그 에이전트 글 정리" 같은 요청에 사용. 투자 매수/매도 권유는 하지 않는다.
license: MIT
---

# AI Agent 일일 브리핑 스킬

매일 AI Agent 기술 트렌드를 추적하기 위해, 고정 소스 2곳에서 최신 글을 모아
**원문 근거 기반** 한국어 브리핑 초안을 만들고 날짜별로 보관한다.

- 소스: GeekNews(`https://news.hada.io/rss/news`), AWS ML Blog(`https://aws.amazon.com/blogs/machine-learning/feed/`)
- 산출물: `/briefings/YYYY-MM-DD.md` (workspace 루트 기준)
- 범위(1단계): 수집 → 정제 → 브리핑 초안 → 아카이빙. 발송은 채널이 설정된 경우에만.

## 절차

1. **수집** — 피드 스크립트를 실행해 후보를 파일로 받는다(소스당 1회 요청).
   결과 파일은 스크래핑 로그로 함께 보관된다.
   ```
   python skills/ai-agent-daily-briefing/fetch_feeds.py --out briefings/raw/YYYY-MM-DD.json
   ```
   그다음 `read_file /briefings/raw/YYYY-MM-DD.json` 으로 후보를 읽는다.
   - 사용자가 소스를 한정하면 `--source geeknews` 또는 `--source aws-ml`.
   - 기간 기본값은 48시간(`--since-hours`). 후보가 3건 미만이면 72~168시간으로 넓힌다.
   - 한 소스가 실패해도(`sources.<이름>.ok=false`) 나머지로 진행하고 브리핑에 실패 사실을 적는다.

2. **정제** — 후보 JSON 을 읽고 선별한다.
   - 제목·요약을 읽고 **AI Agent 와의 관련성을 직접 판단**한다. `keyword_hits` 는 힌트일 뿐이다.
   - 같은 사건·발표를 다룬 글은 하나로 묶는다.
   - 중요도 순 정렬: 에이전트 관련성 > 지속적 영향(신규 제품·프레임워크·표준) > 최신성.
   - 기본 5~10건.

3. **초안 작성** — `templates/briefing.md` 형식을 따른다.
   - 상단에 **핵심 요약 3줄**.
   - 항목마다 **제목 / 2~3줄 요약 / 원문 링크 / 주목 포인트**.

4. **변환·발송(선택)** — 사용자가 발송을 요청하고 해당 커넥터 도구(Slack/Email 등)가
   실제로 있을 때만 보낸다. 발송 전에 반드시 5단계 아카이빙을 먼저 한다.

5. **아카이빙** — `write_file` 로 `/briefings/YYYY-MM-DD.md` 에 저장한다(오늘 날짜, KST).

6. **자기 점검** — `references/rules.md` 체크리스트로 확인한 뒤, 최종 답변에 저장 경로와
   핵심 요약을 짧게 알린다.

## 반드시 지키는 규칙 (요약)

- **허위 요약 금지** — 수집된 제목·요약에 근거한 내용만 쓴다.
- **AI Agent 와 무관한 글은 제외**, 애매하면 판단 근거를 한 줄로 명시.
- **투자 매수/매도 권유 금지** — 기술 트렌드 정보만 제공.
- **자격증명(API 키·웹훅 URL)을 파일에 저장하지 않는다.**
- **발송 전 아카이빙** (`/briefings/`).
- **한국어**, 기술 용어는 원어 병기, 간결·사실 중심.

자세한 규칙은 `references/rules.md`, 형식은 `templates/briefing.md` 참조.
