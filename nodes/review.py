"""review: 규칙 검사(코드) + LLM 검수관(근거 인용) → 통과 / 재생성 / 스킵·대체

본문 추출에 성공한 기사만 여기까지 오므로 원문 링크가 열리는 것은 extract_body 단계에서 확인된다.
"""
import os
import re
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel

import metrics
from nodes.summarize import BODY_FOR_SUMMARY, pending
from state import NewsState


NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
                "nineteen", "twenty"]


def rule_check(draft: dict, body: str, card: dict) -> list[str]:
    fails = []
    text = " ".join([draft["title"], *draft["summary"], *(draft[f["key"]] for f in card["fields"])])
    # 원문이 'ten'처럼 영어 단어로 쓴 수는 숫자로도 인정 (요약은 한국어라 '10개'로 옮기므로)
    words = {str(n) for n, w in enumerate(NUMBER_WORDS) if re.search(rf"\b{w}\b", body, re.I)}
    flat_body = body.replace(",", "") + " " + " ".join(words)
    # 두 자리 이상이거나 소수인 숫자는 본문에 그대로 있어야 함 (한 자리 정수는 번역 표현 차이가 커서 제외)
    for num in set(re.findall(r"\d+(?:[.,]\d+)*", text)):
        n = num.replace(",", "")
        if (len(n) >= 2 or "." in n) and n not in flat_body:
            fails.append(f"본문에 없는 숫자: {num}")
    # 영문 고유명사(대문자·숫자 포함 토큰)는 원문 표기로 본문에 있어야 함
    for word in set(re.findall(r"\b[A-Za-z][A-Za-z0-9\-]*[A-Z0-9][A-Za-z0-9\-]*\b", text)):
        if word.lower() not in body.lower():
            fails.append(f"본문에 없는 고유명사: {word}")
    if len(draft["summary"]) != 3 or not all(s.strip() for s in draft["summary"]):
        fails.append("요약이 3문장이 아님")
    required = ["title"] + [f["key"] for f in card["fields"] if not f.get("optional")]
    for field in required:
        if not draft[field].strip():
            fails.append(f"빈 필드: {field}")
    return fails


class SentenceVerdict(BaseModel):
    idx: int
    verdict: Literal["SUPPORTED", "UNSUPPORTED", "CONTRADICTED"]
    quote: str  # 근거가 되는 본문 문장 (없으면 빈 문자열)


class ReviewOut(BaseModel):
    sentences: list[SentenceVerdict]
    insight_issues: list[str]


def llm_check(client, draft: dict, body: str, card: dict) -> ReviewOut:
    lines = "\n".join(f"[{i}] {s}" for i, s in enumerate(draft["summary"]))
    extras = "\n".join(f"{f['label']}: {draft[f['key']]}" for f in card["fields"])
    res = client.chat.completions.parse(
        model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        messages=[
            {"role": "system", "content": (
                "너는 요약을 쓰지 않은 독립 검수관이다. 본문만 근거로 판정한다.\n"
                "1) 요약 문장마다 근거가 되는 본문 문장을 quote에 원문 그대로 인용하고 판정한다: "
                "SUPPORTED(본문이 뒷받침), UNSUPPORTED(본문에 근거 없음), CONTRADICTED(본문과 모순). 번역 표현 차이는 허용.\n"
                "2) 요약 외 카드 항목은 본문과 모순되거나 과장하는지만 본다. "
                "예: 승인되지 않았는데 '승인', 예비 결과를 '입증', 본문에 없는 효과·사실을 단정. "
                "문제가 있으면 insight_issues에 한국어로 적고, 없으면 빈 리스트.")},
            {"role": "user", "content": (
                f"본문:\n{body[:BODY_FOR_SUMMARY]}\n\n요약 문장:\n{lines}\n\n카드 항목:\n{extras}")},
        ],
        response_format=ReviewOut,
    )
    return res.choices[0].message.parsed


def replacement(state: NewsState, selected: list[str], skipped: list[str]) -> str | None:
    """예비 후보 중 차순위, 소스당 최대 건수를 지키는 기사"""
    cap = state["config"]["selection"]["max_per_source"]
    src = {r["item_id"]: r["source"] for r in state["ranked"]}
    active = [i for i in selected if i not in skipped]
    for cand in state["reserves"]:
        if cand not in selected and sum(src[i] == src[cand] for i in active) < cap:
            return cand
    return None


def review(state: NewsState) -> dict:
    cfg = state["config"]["review"]
    client = OpenAI()
    results = {k: list(v) for k, v in state.get("review_results", {}).items()}
    attempts = dict(state.get("attempts", {}))
    approved, skipped = list(state.get("approved", [])), list(state.get("skipped", []))
    selected, used = list(state["selected"]), state.get("replacements_used", 0)
    original = set(state.get("original_selected") or state["selected"])

    for item_id in pending(state):
        draft, body = state["drafts"][item_id], state["bodies"][item_id]
        rule_fail = rule_check(draft, body, state["config"]["card"])
        judged = llm_check(client, draft, body, state["config"]["card"])
        verdicts = [s.verdict for s in judged.sentences]
        counts = {v: verdicts.count(v) for v in ("SUPPORTED", "UNSUPPORTED", "CONTRADICTED")}
        ok = (not rule_fail and counts["SUPPORTED"] == len(draft["summary"])
              and not counts["UNSUPPORTED"] and not counts["CONTRADICTED"] and not judged.insight_issues)

        bad = [f"요약 [{s.idx}] {s.verdict}" for s in judged.sentences if s.verdict != "SUPPORTED"]
        feedback = "\n".join(rule_fail + bad + judged.insight_issues)
        results.setdefault(item_id, []).append({"attempt": attempts.get(item_id, 0) + 1, "ok": ok, "feedback": feedback})

        if ok:
            approved.append(item_id)
            outcome = "replaced" if item_id not in original else ("regenerated" if attempts.get(item_id) else "pass")
        else:
            attempts[item_id] = attempts.get(item_id, 0) + 1
            outcome = "retry"
            if attempts[item_id] > cfg["max_regenerations"]:
                skipped.append(item_id)
                outcome = "skipped"
                if used < cfg["max_replacements"] and (new := replacement(state, selected, skipped)):
                    selected.append(new)
                    used += 1
        metrics.log(state["run_id"], "review", item_id=item_id, attempt=results[item_id][-1]["attempt"],
                    rule_fail=rule_fail, supported=counts["SUPPORTED"], unsupported=counts["UNSUPPORTED"],
                    contradicted=counts["CONTRADICTED"], insight_issues=judged.insight_issues, outcome=outcome)

    return {"review_results": results, "attempts": attempts, "approved": approved, "skipped": skipped,
            "selected": selected, "replacements_used": used, "original_selected": sorted(original)}
