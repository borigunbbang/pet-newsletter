"""파이프라인 실행 (로컬·GitHub Actions 공용): python run.py"""
import datetime as dt
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv

from graph import build_graph

load_dotenv(Path(__file__).resolve().parent / ".env")


def main():
    now = dt.datetime.now(dt.timezone.utc)
    state = {
        "run_id": now.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
        "trigger": os.getenv("GITHUB_EVENT_NAME", "local"),  # schedule / workflow_dispatch / local
        "started_at": now.isoformat(),
    }
    # 검수 루프(기사당 재생성 2회 + 대체 2건)가 돌아도 멈추지 않도록 단계 한도를 넉넉히
    result = build_graph().invoke(state, {"recursion_limit": 100})
    print(f"run_id={result['run_id']} 발행 상태={result.get('publish_result', {}).get('status')}")


if __name__ == "__main__":
    main()
