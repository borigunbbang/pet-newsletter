"""store/metrics.jsonl 기록: 한 줄 = 이벤트 1개, run_id로 묶음"""
import datetime as dt
import json
from pathlib import Path

PATH = Path(__file__).resolve().parent / "store" / "metrics.jsonl"


def log(run_id: str, event: str, **fields):
    PATH.parent.mkdir(exist_ok=True)
    row = {"run_id": run_id, "ts": dt.datetime.now(dt.timezone.utc).isoformat(), "event": event, **fields}
    with PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
