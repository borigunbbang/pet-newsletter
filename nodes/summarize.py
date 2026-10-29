"""summarize: 본문 기반 한국어 제목·요약 3줄·카드 항목 (검수 실패 기사는 실패 사유를 넣어 재생성)

카드 항목과 근거 수준 태그는 audience.yaml의 card 설정을 따른다.
"""
import os
from typing import Literal

from openai import OpenAI
from pydantic import create_model

from state import NewsState

BODY_FOR_SUMMARY = 8000


def draft_model(card: dict):
    fields = {"title": (str, ...), "summary": (list[str], ...)}
    fields |= {f["key"]: (str, ...) for f in card["fields"]}
    if card.get("evidence"):
        fields["evidence"] = (Literal[tuple(card["evidence"])], ...)
    return create_model("Draft", **fields)


def field_rules(card: dict) -> str:
    lines = [f"- {f['key']}: {f['instruction']}" + (". 없으면 빈 문자열" if f.get("optional") else "")
             for f in card["fields"]]
    if card.get("evidence"):
        lines.append(f"- evidence: 근거 수준 ({card.get('evidence_hint', '')})")
    return "\n".join(lines)


def pending(state: NewsState) -> list[str]:
    """아직 검수를 통과하지도, 스킵되지도 않은 선발 기사"""
    done = set(state.get("approved", [])) | set(state.get("skipped", []))
    return [i for i in state["selected"] if i not in done]


def summarize(state: NewsState) -> dict:
    cfg = state["config"]
    items = {it["id"]: it for it in state["candidates"]}
    drafts = dict(state.get("drafts", {}))
    client, Draft = OpenAI(), draft_model(cfg["card"])
    for item_id in pending(state):
        it = items[item_id]
        history = state.get("review_results", {}).get(item_id, [])
        feedback = (f"\n\n[이전 초안의 검수 실패 사유 — 반드시 고칠 것]\n{history[-1]['feedback']}" if history else "")
        res = client.chat.completions.parse(
            model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
            messages=[
                {"role": "system", "content": (
                    f"'{cfg['newsletter_name']}' 뉴스레터 편집자다. "
                    f"독자: {cfg['persona']['who']}. 아래 본문에 있는 사실만 쓴다. 모든 항목은 한국어로 쓴다.\n"
                    "- title: 한국어 제목 (외국어 기사도 번역, 40자 이내)\n"
                    "- summary: 핵심 요약 정확히 3문장. 수치·날짜와 이름(기관·제품·모델·사람·동물 이름)만 원문 표기 그대로, "
                    "일반 용어·설명은 한국어로 번역\n"
                    + field_rules(cfg["card"]))},
                {"role": "user", "content": (
                    f"출처: {it['source']}\n원문 제목: {it['title']}\n\n본문:\n"
                    f"{state['bodies'][item_id][:BODY_FOR_SUMMARY]}{feedback}")},
            ],
            response_format=Draft,
        )
        d = res.choices[0].message.parsed
        drafts[item_id] = {**d.model_dump(), "title_original": it["title"], "source": it["source"],
                           "url": it["url"], "published_at": it["published_at"][:10]}
    return {"drafts": drafts}
