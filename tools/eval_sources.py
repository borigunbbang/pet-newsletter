"""소스 후보 실측: 업데이트 빈도·본문 접근성·주제 적합도 → store/source_eval.json"""
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import feedparser
import requests
import trafilatura
import yaml
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

UA = {"User-Agent": "pet-newsletter-source-eval/0.1"}
# 주제는 audience.yaml(topic_fit), 후보 목록은 source_candidates.yaml에서 읽는다
CONFIG = yaml.safe_load((ROOT / "audience.yaml").read_text(encoding="utf-8"))
SOURCES = [(c["name"], c["lang"], c["url"], c["role"])
           for c in yaml.safe_load((ROOT / "source_candidates.yaml").read_text(encoding="utf-8"))["candidates"]]

MIN_FREQ, MIN_ACCESS = 7, 0.8
# 처음엔 적합도 비율 ≥ 30%였으나, 일반 의료 매체는 비율이 낮아도 AI 기사 절대량이 많고
# 비율이 낮은 기사는 예선에서 걸러지므로 "주간 적합 기사 수(빈도 × 적합도) ≥ 7"로 변경
MIN_FIT_WEEKLY = 7
N_EXTRACT, N_FIT, MIN_CHARS = 10, 20, 500


def judge(row: dict) -> list[str]:
    """채택 기준 3개 중 통과하지 못한 항목 (빈 리스트면 채택)"""
    fails = []
    row["fit_weekly"] = round(row["freq_7d"] * row["topic_fit"], 1)
    if row["freq_7d"] < MIN_FREQ:
        fails.append(f"빈도 {row['freq_7d']}건/주 < {MIN_FREQ}")
    if row["access_rate"] < MIN_ACCESS:
        fails.append(f"접근성 {row['access_rate']:.0%} < {MIN_ACCESS:.0%}")
    if row["fit_weekly"] < MIN_FIT_WEEKLY:
        fails.append(f"주간 적합 기사 {row['fit_weekly']}건 < {MIN_FIT_WEEKLY}")
    return fails


class Label(BaseModel):
    i: int
    fit: bool
    reason: str


class Labels(BaseModel):
    labels: list[Label]


def entry_time(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return dt.datetime(*t[:6], tzinfo=dt.timezone.utc) if t else None


def clean(text, n=300):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()[:n]


def fetch_feed(url):
    r = requests.get(url, headers=UA, timeout=20)
    return r.status_code, feedparser.parse(r.content).entries


def extract_rate(entries):
    ok = 0
    for e in entries[:N_EXTRACT]:
        try:
            r = requests.get(e.link, headers=UA, timeout=20)
            text = trafilatura.extract(r.text) if r.status_code == 200 else None
            ok += bool(text and len(text) >= MIN_CHARS)
        except requests.RequestException:
            pass
        time.sleep(1)
    n = min(len(entries), N_EXTRACT)
    return ok / n if n else 0.0


def topic_fit(client, entries):
    items = [f"[{i}] {clean(e.get('title'), 150)} — {clean(e.get('summary'))}"
             for i, e in enumerate(entries[:N_FIT])]
    res = client.chat.completions.parse(
        model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        messages=[
            {"role": "system", "content": (
                f"각 기사가 다음 주제에 해당하는지 판정한다(해당하면 fit=true): {CONFIG['topic_fit'].strip()} "
                "reason은 한국어 한 줄.")},
            {"role": "user", "content": "\n".join(items)},
        ],
        response_format=Labels,
    )
    labels = res.choices[0].message.parsed.labels
    return labels, (sum(l.fit for l in labels) / len(labels) if labels else 0.0)


def main():
    client = OpenAI()
    now = dt.datetime.now(dt.timezone.utc)
    results = []
    for name, lang, url, role in SOURCES:
        row = {"name": name, "lang": lang, "url": url, "role": role}
        try:
            status, entries = fetch_feed(url)
        except requests.RequestException as ex:
            status, entries = f"error: {type(ex).__name__}", []
        row["http_status"] = status
        row["entries"] = len(entries)
        row["freq_7d"] = sum(1 for e in entries if (t := entry_time(e)) and (now - t).days < 7)

        if role == "measure" and entries:
            print(f"측정 중: {name}", file=sys.stderr)
            row["access_rate"] = extract_rate(entries)
            labels, row["topic_fit"] = topic_fit(client, entries)
            row["fit_samples"] = [
                {"title": clean(entries[l.i].get("title"), 150), "fit": l.fit, "reason": l.reason}
                for l in labels if l.i < len(entries)
            ]
            fails = judge(row)
            row["adopted"], row["reject_reasons"] = not fails, fails
        elif role == "measure":
            row["adopted"], row["reject_reasons"] = False, [f"RSS 수집 실패 (HTTP {status}, 항목 0건)"]
        elif role == "rss_fail":
            row["adopted"] = False
            if not entries:
                row["reject_reasons"] = [f"RSS 수집 불가 (HTTP {status}, 항목 0건)"]
            else:
                row["reject_reasons"] = [f"빈도 {row['freq_7d']}건/주 < {MIN_FREQ}"]
        else:
            row["adopted"] = None  # 예비 후보: 측정하지 않음
        results.append(row)

    save({"measured_at": now.isoformat(), "sources": results})


CRITERIA = {"freq_7d_min": MIN_FREQ, "access_rate_min": MIN_ACCESS, "fit_weekly_min": MIN_FIT_WEEKLY,
            "extract_sample": N_EXTRACT, "extract_min_chars": MIN_CHARS, "fit_sample": N_FIT}
PATH = ROOT / "store" / "source_eval.json"


def save(out: dict):
    out["criteria"] = CRITERIA
    PATH.parent.mkdir(exist_ok=True)
    PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    report(out["sources"])


def rejudge():
    """재측정 없이 저장된 실측값에 현재 기준을 다시 적용"""
    out = json.loads(PATH.read_text())
    for r in out["sources"]:
        if r["role"] == "measure" and r["entries"]:
            fails = judge(r)
            r["adopted"], r["reject_reasons"] = not fails, fails
    out["rejudged_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    save(out)


def report(results: list[dict]):
    for r in results:
        if r["role"] != "measure":
            continue
        mark = "채택" if r.get("adopted") else "탈락"
        print(f"{r['name']:22} 빈도 {r['freq_7d']:3}  접근성 {r.get('access_rate', 0):4.0%}  "
              f"적합도 {r.get('topic_fit', 0):4.0%}  주간 적합 {r.get('fit_weekly', 0):5.1f}"
              f"  → {mark} {', '.join(r.get('reject_reasons', []))}")
    print(f"저장: {PATH}")


if __name__ == "__main__":
    rejudge() if "--rejudge" in sys.argv else main()
