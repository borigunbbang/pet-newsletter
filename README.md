# 멍냥 데일리 — 강아지·고양이 힐링 뉴스레터 에이전트

출근길 직장인을 위해 매일 07:00 KST에 강아지·고양이 소식을 수집·선별·요약·검수해 디스코드로 발행하는 LangGraph 파이프라인입니다.
[헬스케어 AI 데일리](https://github.com/borigunbbang/my-newsletter)와 같은 코드에 설정(`audience.yaml`, `source_candidates.yaml`)만 바꿔 만들었습니다.

## 구조

```
graph.py             LangGraph 노드·엣지 (검수 재생성·대체 루프 포함)
run.py               실행 진입점 (로컬·GitHub Actions 공용)
state.py             State 정의
metrics.py           store/metrics.jsonl 기록
audience.yaml        주제·독자·중요도 판단 문장·예선 조건·채택 소스·선별/검수 설정
source_candidates.yaml  소스 후보 목록 (실측 대상)
nodes/               collect · select(예선·본문·본선) · summarize · review · publish
tools/eval_sources.py  소스 후보 실측 → store/source_eval.json
tests/               오류 주입 테스트 (요약 숫자 변조 → 검수 실패 → 재생성)
store/               metrics.jsonl · seen.json · source_eval.json
```

## 로컬 실행

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # OPENAI_API_KEY, DISCORD_WEBHOOK_URL 입력
.venv/bin/python run.py
```

- 소스 재실측: `.venv/bin/python tools/eval_sources.py` (저장값으로 재판정만: `--rejudge`)
- 오류 주입 테스트: `.venv/bin/python tests/test_review_injection.py`

## 새 주제로 만들기

코드는 그대로 두고 설정 파일 두 개만 바꿉니다.

1. 폴더를 복사한 뒤 `.git`을 지우고 `git init` → `gh repo create`로 새 저장소를 만듭니다.
2. `audience.yaml`을 고칩니다: `newsletter_name`, `topic_fit`, `persona`, `importance_question`, `prelim.pass`·`prelim.drop`, `exclude`.
3. `source_candidates.yaml`에 새 주제의 소스 후보를 넣고 `python tools/eval_sources.py`로 실측합니다.
4. 결과(`store/source_eval.json`)에서 채택된 소스를 `audience.yaml`의 `sources`에 넣습니다.
5. `store/metrics.jsonl`·`store/seen.json`을 비우고, Secrets 3개를 등록한 뒤 Actions에서 **Run workflow**로 확인합니다.

텔레그램은 같은 봇을 써도 되고, 주제별로 봇·채널을 따로 만들어 `TELEGRAM_*` Secrets만 바꿔도 됩니다.

## 자동 실행 (GitHub Actions)

- `.github/workflows/newsletter.yml`: 매일 07:00 KST(`cron: 0 22 * * *` UTC) + 수동 실행(`workflow_dispatch`)
- 실행 후 `store/`의 변경을 `github-actions[bot]`이 커밋합니다 (`[skip ci]`).
- 필요한 Secrets: `OPENAI_API_KEY`, `DISCORD_WEBHOOK_URL` (텔레그램으로 바꾸려면 `publish.channel: telegram` + `TELEGRAM_*`)
- GitHub의 예약 실행은 부하에 따라 몇 분~몇십 분 늦게 시작될 수 있습니다.

### 워크플로 끄기

- 웹: 저장소 → **Actions** 탭 → 왼쪽에서 `newsletter` 선택 → 오른쪽 위 **`···`** → **Disable workflow** (다시 켤 때는 같은 위치의 **Enable workflow**)
- CLI: `gh workflow disable newsletter` / `gh workflow enable newsletter`
- 영구 제거: `.github/workflows/newsletter.yml` 삭제 후 커밋
