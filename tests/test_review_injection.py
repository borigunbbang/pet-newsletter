"""오류 주입 테스트: 요약에 본문에 없는 숫자를 넣으면 검수가 실패시키고, 재생성 후 통과해야 한다.

실행: python tests/test_review_injection.py  (OpenAI API 호출, 텔레그램 발행 없음, 기록 안 남김)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
import metrics

metrics.log = lambda *a, **k: None  # 테스트 실행은 metrics.jsonl에 남기지 않음

from nodes.collect import collect, prefilter
from nodes.review import review
from nodes.select import extract_body, final_rank, prelim_screen
from nodes.summarize import pending, summarize


def main():
    s = {"run_id": "test", "config": yaml.safe_load((ROOT / "audience.yaml").read_text(encoding="utf-8"))}
    s.update(collect(s))
    s.update(prefilter(s))
    s.update(prelim_screen(s))
    s.update(extract_body(s))
    s.update(final_rank(s))
    s["selected"] = s["selected"][:1]  # 기사 1건으로만 시험
    target = s["selected"][0]
    s.update(summarize(s))

    # 주입: 첫 요약 문장에 본문에 없는 수치를 덧붙임
    draft = s["drafts"][target]
    draft["summary"][0] += " 정확도는 99.73%로 보고됐다."
    print("주입한 요약:", draft["summary"][0])

    s.update(review(s))
    first = s["review_results"][target][0]
    assert not first["ok"], "주입한 오류를 검수가 통과시킴"
    assert "99.73" in first["feedback"], "숫자 규칙 검사가 99.73을 잡지 못함"
    print("1차 검수: 실패 ✅\n" + first["feedback"])

    while pending(s):  # 실패 사유를 넣어 재생성 → 재검수
        s.update(summarize(s))
        s.update(review(s))
    last = s["review_results"][target][-1]
    print(f"최종: {'통과' if target in s['approved'] else '스킵'} (시도 {last['attempt']}회)")
    assert "99.73" not in " ".join(s["drafts"][target]["summary"]), "재생성 후에도 주입한 숫자가 남음"
    print("재생성 요약:", s["drafts"][target]["summary"][0])


if __name__ == "__main__":
    main()
