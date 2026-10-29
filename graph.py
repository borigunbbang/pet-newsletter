"""LangGraph 워크플로: 수집 → 선별 → 요약 → 검수(재생성·대체 루프) → 발행 → 기록"""
import datetime as dt
from pathlib import Path

import yaml
from langgraph.graph import END, START, StateGraph

import metrics
from nodes.collect import collect, prefilter
from nodes.publish import compose, publish
from nodes.review import review
from nodes.select import extract_body, final_rank, prelim_screen
from nodes.summarize import pending, summarize
from state import NewsState

ROOT = Path(__file__).resolve().parent


def load_config(state: NewsState) -> dict:
    config = yaml.safe_load((ROOT / "audience.yaml").read_text(encoding="utf-8"))
    return {"config": config, "errors": []}


# --- 검수 루프·발행 게이트·기록 ---
def after_review(state: NewsState) -> str:
    """재생성·대체할 기사가 남았으면 다시 요약, 아니면 통과 기사가 최소 발행 수 이상일 때만 발행"""
    if pending(state):
        return "summarize"
    min_publish = state["config"]["review"]["min_publish"]
    return "compose" if len(state.get("approved", [])) >= min_publish else "hold"


def hold(state: NewsState) -> dict:
    metrics.log(state["run_id"], "publish", status="held", message_ids=[], char_counts=[], item_ids=[])
    return {"publish_result": {"status": "held", "message_ids": []}}


def record(state: NewsState) -> dict:
    ended = dt.datetime.now(dt.timezone.utc)
    started = dt.datetime.fromisoformat(state["started_at"])
    metrics.log(
        state["run_id"], "run_summary",
        started_at=state["started_at"],
        ended_at=ended.isoformat(),
        duration_s=round((ended - started).total_seconds(), 1),
        trigger=state["trigger"],
        funnel={
            "collected": len(state.get("raw_items", [])),
            "prefiltered": len(state.get("candidates", [])),
            "prelim_pass": sum(1 for l in state.get("prelim_labels", []) if l.get("label") == "PASS"),
            "final_selected": len(state.get("selected", [])),
            "published": len(state.get("approved", [])) if state.get("publish_result", {}).get("status") == "published" else 0,
        },
        publish_status=state.get("publish_result", {}).get("status"),
        errors=state.get("errors", []),
    )
    return {}


def build_graph():
    g = StateGraph(NewsState)
    steps = [load_config, collect, prefilter, prelim_screen, extract_body, final_rank, summarize, review]
    for fn in steps + [compose, publish, hold, record]:
        g.add_node(fn.__name__, fn)

    g.add_edge(START, steps[0].__name__)
    for a, b in zip(steps, steps[1:]):
        g.add_edge(a.__name__, b.__name__)
    g.add_conditional_edges("review", after_review, {"summarize": "summarize", "compose": "compose", "hold": "hold"})
    g.add_edge("compose", "publish")
    g.add_edge("publish", "record")
    g.add_edge("hold", "record")
    g.add_edge("record", END)
    return g.compile()
