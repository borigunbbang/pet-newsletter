"""compose: 검수 통과 기사 → 채널별 메시지 / publish: 전송·확인·기록

채널은 audience.yaml의 publish.channel (telegram | discord).
drafts[item_id] 형식 (summarize 노드가 채움):
  title(한국어 제목 — 외국어 기사도 번역), title_original, source, url, published_at,
  summary(3줄 리스트), card.fields의 각 key, evidence(card.evidence가 있을 때)
"""
import datetime as dt
import html
import json
import os
import re
from pathlib import Path

import requests
from openai import OpenAI

import metrics
from state import NewsState

SEEN = Path(__file__).resolve().parent.parent / "store" / "seen.json"
LIMIT = {"telegram": 4096, "discord": 2000}
KST = dt.timezone(dt.timedelta(hours=9))


def headline(name: str, drafts: list[dict]) -> str:
    """오늘 기사 전체를 관통하는 흐름 1문장"""
    titles = "\n".join(f"- {d['title']}: {d['summary'][0]}" for d in drafts)
    res = OpenAI().chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        messages=[
            {"role": "system", "content": (
                f"'{name}' 뉴스레터의 '오늘의 한 줄'을 쓴다. 아래 기사들만 근거로, "
                "공통 흐름을 한국어 한 문장(60자 이내)으로. 기사에 없는 사실은 쓰지 않는다.")},
            {"role": "user", "content": titles},
        ],
    )
    return res.choices[0].message.content.strip()


# --- 채널별 서식: 텔레그램은 HTML, 디스코드는 마크다운 ---
def esc(channel: str, s: str) -> str:
    return html.escape(s) if channel == "telegram" else re.sub(r"([\\*_~|`>])", r"\\\1", s)


def bold(channel: str, s: str) -> str:
    return f"<b>{esc(channel, s)}</b>" if channel == "telegram" else f"**{esc(channel, s)}**"


def link(channel: str, url: str) -> str:
    # 텔레그램은 link_preview_options로, 디스코드는 맨 아래 URL 자체로 미리보기 카드(대표 사진)가 붙는다
    return f"🔗 <a href=\"{html.escape(url)}\">원문 보기</a>" if channel == "telegram" else f"🔗 원문 보기\n{url}"


def item_message(d: dict, card: dict, channel: str) -> str:
    e = lambda s: esc(channel, s)
    meta = [d["source"], d["published_at"]] + ([f"[{d['evidence']}]"] if d.get("evidence") else [])
    lines = [f"■ {bold(channel, d['title'])}", e(" · ".join(meta)), "", *[f"• {e(s)}" for s in d["summary"]]]
    lines += [f"{f['label']}: {e(d[f['key']])}" for f in card["fields"] if d.get(f["key"], "").strip()]
    # 본문 아래 빈 줄 3개 후 원문 링크 → 그 아래에 미리보기 카드
    tail = "\n\n\n\n" + link(channel, d["url"])
    return "\n".join(lines)[:LIMIT[channel] - len(tail)] + tail


def compose(state: NewsState) -> dict:
    cfg = state["config"]
    channel = cfg["publish"]["channel"]
    drafts = [state["drafts"][i] for i in state["approved"]]
    today = dt.datetime.now(KST).strftime("%Y-%m-%d")
    name = cfg["newsletter_name"]
    header = f"📰 [{today}] {esc(channel, name)}\n💡 오늘의 한 줄: {esc(channel, headline(name, drafts))}"
    return {"newsletter": {"header": header, "messages": [item_message(d, cfg["card"], channel) for d in drafts]}}


def send_telegram(text: str, url: str | None) -> int | None:
    # 기사 메시지: 원문 대표 사진을 크게, 본문 아래에 미리보기 / 헤더: 미리보기 없음
    preview = ({"url": url, "prefer_large_media": True, "show_above_text": False}
               if url else {"is_disabled": True})
    res = requests.post(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
        json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML",
              "link_preview_options": preview},
        timeout=20,
    ).json()
    return res["result"]["message_id"] if res.get("ok") and res.get("result", {}).get("message_id") else None


def send_discord(text: str, url: str | None) -> str | None:
    # wait=true여야 생성된 메시지(id)를 돌려받아 발행 성공을 확인할 수 있다
    r = requests.post(os.environ["DISCORD_WEBHOOK_URL"], params={"wait": "true"},
                      json={"content": text, "allowed_mentions": {"parse": []}}, timeout=20)
    return r.json().get("id") if r.status_code == 200 else None


def send_with_retry(channel: str, text: str, url: str | None = None):
    """성공하면 message_id, 1회 재시도 후에도 실패하면 None"""
    send = send_telegram if channel == "telegram" else send_discord
    for _ in range(2):
        try:
            if mid := send(text, url):
                return mid
        except (requests.RequestException, ValueError):
            pass
    return None


def publish(state: NewsState) -> dict:
    channel = state["config"]["publish"]["channel"]
    nl = state["newsletter"]
    texts = [nl["header"], *nl["messages"]]
    urls = [None, *[state["drafts"][i]["url"] for i in state["approved"]]]
    message_ids = [send_with_retry(channel, t, u) for t, u in zip(texts, urls)]
    ok = all(message_ids)
    status = "published" if ok else "partial"

    # 발행에 성공한 기사 URL만 seen.json에 추가 (message_ids[0]은 헤더)
    sent_items = [i for i, mid in zip(state["approved"], message_ids[1:]) if mid]
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    seen |= {state["drafts"][i]["url"] for i in sent_items}
    SEEN.parent.mkdir(exist_ok=True)
    SEEN.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2))

    metrics.log(state["run_id"], "publish", channel=channel, status=status, message_ids=message_ids,
                char_counts=[len(t) for t in texts], item_ids=sent_items)
    errors = state.get("errors", []) + ([] if ok else [f"{channel}: 일부 메시지 전송 실패"])
    return {"publish_result": {"status": status, "message_ids": message_ids}, "errors": errors}
