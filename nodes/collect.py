"""collect: 소스별 RSS/arXiv 수집 / prefilter: 수집 기간·발행 이력·제외 목록·중복 제거"""
import datetime as dt
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests

import metrics
from state import NewsState

SEEN = Path(__file__).resolve().parent.parent / "store" / "seen.json"
UA = {"User-Agent": "pet-newsletter/0.1"}


def clean(text: str, n: int = 1000) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()[:n]


def normalize_url(url: str) -> str:
    """utm 등 추적 파라미터·#·끝 슬래시 제거"""
    p = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.startswith("utm_")])
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), query, ""))


def published(e) -> dt.datetime | None:
    t = e.get("published_parsed") or e.get("updated_parsed")
    return dt.datetime(*t[:6], tzinfo=dt.timezone.utc) if t else None


def collect(state: NewsState) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    default_window = state["config"]["collect"]["window_hours"]
    items, errors = [], list(state.get("errors", []))
    for src in state["config"]["sources"]:
        start = time.monotonic()
        window = dt.timedelta(hours=src.get("window_hours", default_window))
        try:
            r = requests.get(src["url"], headers=UA, timeout=20)
            r.raise_for_status()
            entries = feedparser.parse(r.content).entries
        except requests.RequestException as ex:
            entries = []
            errors.append(f"collect {src['name']}: {type(ex).__name__}")
        in_window = 0
        for e in entries:
            t = published(e)
            if not (t and e.get("link")) or now - t > window:
                continue
            in_window += 1
            url = normalize_url(e.link)
            items.append({
                "id": hashlib.sha1(url.encode()).hexdigest()[:12],
                "source": src["name"],
                "url": url,
                "title": clean(e.get("title"), 300),
                "summary": clean(e.get("summary")),
                "published_at": t.isoformat(),
            })
        metrics.log(state["run_id"], "collect", source=src["name"], fetched=len(entries),
                    in_window=in_window, duration_ms=int((time.monotonic() - start) * 1000))
    return {"raw_items": items, "errors": errors}


def norm_title(title: str) -> str:
    return re.sub(r"[\W_]+", "", title.lower())


def prefilter(state: NewsState) -> dict:
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    excluded = {urlsplit(u).netloc.lower() for u in state["config"]["exclude"]["sources"]}  # 제외 소스 도메인
    urls, titles, out = set(), set(), []
    for it in state["raw_items"]:
        t = norm_title(it["title"])
        if it["url"] in seen or it["url"] in urls or t in titles:
            continue
        if urlsplit(it["url"]).netloc in excluded:
            continue
        urls.add(it["url"])
        titles.add(t)
        out.append(it)
    return {"candidates": out}
