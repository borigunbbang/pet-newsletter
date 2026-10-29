"""prelim_screen: 예선(batch PASS/DROP) / extract_body: 본문 추출 / final_rank: 본선(4축 상대 비교)"""
import os
import time
from typing import Literal

import requests
import trafilatura
from openai import OpenAI
from pydantic import BaseModel, create_model

import metrics
from state import NewsState

UA = {"User-Agent": "pet-newsletter/0.1"}
MIN_BODY = 500
BODY_FOR_RANK = 2000  # 본선 프롬프트에 넣는 본문 앞부분 길이


def persona_prompt(cfg: dict) -> str:
    p = cfg["persona"]
    return (f"독자: {p['who']}\n독자가 원하는 것: {' / '.join(p['needs'])}\n"
            f"중요도 판단 문장: {cfg['importance_question'].strip()}\n"
            f"제외 조건: {' / '.join(cfg['exclude']['conditions'])}")


class PrelimLabel(BaseModel):
    id: str
    label: Literal["PASS", "DROP"]
    reason_type: Literal["①", "②", "none"]
    reason: str


class PrelimOut(BaseModel):
    labels: list[PrelimLabel]


def label_batch(client, cfg: dict, batch: list[dict]) -> dict[str, PrelimLabel]:
    keys = {f"A{n}": it["id"] for n, it in enumerate(batch, 1)}  # 짧은 키로 묻고 되돌림 (score_items 참고)
    listing = "\n".join(f"[{k}] ({it['source']}) {it['title']} — {it['summary'][:400]}" for k, it in zip(keys, batch))
    res = client.chat.completions.parse(
        model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        messages=[
            {"role": "system", "content": (
                f"'{cfg['newsletter_name']}' 뉴스레터의 예선 심사관이다. 제목과 요약만 보고 중요도 판단 문장을 적용한다.\n"
                + persona_prompt(cfg) +
                "\nPASS 조건 (하나라도 해당):\n" + "\n".join(f" {p}" for p in cfg["prelim"]["pass"]) +
                f"\nDROP 조건: {cfg['prelim']['drop']} (reason_type=none)"
                "\n모든 기사에 대해 대괄호 안 id를 그대로 적고, reason은 한국어 한 줄.")},
            {"role": "user", "content": listing},
        ],
        response_format=PrelimOut,
    )
    out = {}
    for l in res.choices[0].message.parsed.labels:
        item_id = keys.get(l.id.strip("[] "))
        if item_id and item_id not in out:
            out[item_id] = l
    return out


def prelim_screen(state: NewsState) -> dict:
    cfg, items = state["config"], state["candidates"]
    size = cfg["selection"]["prelim_batch_size"]
    client, labels = OpenAI(), []
    for b, start in enumerate(range(0, len(items), size), 1):
        batch = items[start:start + size]
        got = label_batch(client, cfg, batch)
        missing = [it for it in batch if it["id"] not in got]
        if missing:  # 라벨이 빠진 기사만 한 번 더
            got |= label_batch(client, cfg, missing)
        for it in batch:
            l = got.get(it["id"])
            row = ({"item_id": it["id"], "label": l.label, "reason_type": l.reason_type, "reason": l.reason, "batch_no": b}
                   if l else {"item_id": it["id"], "label": "DROP", "reason_type": "none", "reason": "라벨 누락", "batch_no": b})
            labels.append(row)
            metrics.log(state["run_id"], "prelim", url=it["url"], source=it["source"], **row)
    return {"prelim_labels": labels}


def extract_body(state: NewsState) -> dict:
    passed = {l["item_id"] for l in state["prelim_labels"] if l["label"] == "PASS"}
    bodies = {}
    for it in state["candidates"]:
        if it["id"] not in passed:
            continue
        text = None
        try:
            r = requests.get(it["url"], headers=UA, timeout=20)
            if r.status_code == 200:
                text = trafilatura.extract(r.text)
        except requests.RequestException:
            pass
        ok = bool(text and len(text) >= MIN_BODY)
        if ok:
            bodies[it["id"]] = text
        metrics.log(state["run_id"], "extract", item_id=it["id"], ok=ok, chars=len(text or ""),
                    **({} if ok else {"drop_reason": "extract_fail"}))
        time.sleep(1)
    return {"bodies": bodies}


def score_items(client, state: NewsState, items: list[dict]) -> dict:
    axes = state["config"]["selection"]["final_axes"]  # {축 이름: 설명}
    Score = create_model("Score", id=(str, ...), **{k: (int, ...) for k in axes})
    RankOut = create_model("RankOut", scores=(list[Score], ...))
    # 긴 해시 id는 옮겨 적다 틀리는 경우가 있어 A1, A2… 짧은 키로 묻고 되돌린다
    keys = {f"A{n}": it["id"] for n, it in enumerate(items, 1)}
    listing = "\n\n".join(f"[{k}] ({it['source']}) {it['title']}\n{state['bodies'][it['id']][:BODY_FOR_RANK]}"
                          for k, it in zip(keys, items))
    res = client.chat.completions.parse(
        model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        messages=[
            {"role": "system", "content": (
                f"'{state['config']['newsletter_name']}' 뉴스레터의 본선 심사관이다. "
                "오늘 후보들을 서로 비교해 각 축을 1~5점으로 매긴다.\n"
                + persona_prompt(state["config"]) + "".join(f"\n{k}: {v}" for k, v in axes.items()) +
                "\n모든 후보를 한 번씩만 채점하고, 대괄호 안 id를 그대로 적는다.")},
            {"role": "user", "content": listing},
        ],
        response_format=RankOut,
    )
    out = {}
    for s in res.choices[0].message.parsed.scores:
        item_id = keys.get(s.id.strip("[] "))
        if item_id and item_id not in out:  # 없는 키·중복 채점 무시
            out[item_id] = s
    return out


def final_rank(state: NewsState) -> dict:
    cfg = state["config"]["selection"]
    items = [it for it in state["candidates"] if it["id"] in state["bodies"]]
    if not items:
        return {"ranked": [], "selected": [], "reserves": []}
    client = OpenAI()
    scores = score_items(client, state, items)
    missing = [it for it in items if it["id"] not in scores]
    if missing:  # 점수가 빠진 기사만 한 번 더
        scores |= score_items(client, state, missing)
    ranked = []
    for it in items:
        if s := scores.get(it["id"]):
            axes = s.model_dump(exclude={"id"})
            ranked.append({"item_id": it["id"], "source": it["source"], "scores": axes, "total": sum(axes.values())})
    ranked.sort(key=lambda r: -r["total"])

    selected, per_source = [], {}
    for r in ranked:
        if len(selected) < cfg["final_count"]["max"] and per_source.get(r["source"], 0) < cfg["max_per_source"]:
            selected.append(r["item_id"])
            per_source[r["source"]] = per_source.get(r["source"], 0) + 1
    # 예비 후보도 소스당 제한을 지켜야 대체 후 규칙이 깨지지 않으므로 대체 시점에 다시 확인
    reserves = [r["item_id"] for r in ranked if r["item_id"] not in selected]

    for rank, r in enumerate(ranked, 1):
        metrics.log(state["run_id"], "final", item_id=r["item_id"], scores=r["scores"], total=r["total"],
                    rank=rank, selected=r["item_id"] in selected, reserve=r["item_id"] in reserves)
    return {"ranked": ranked, "selected": selected, "reserves": reserves}
