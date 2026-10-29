from typing import TypedDict


class Article(TypedDict, total=False):
    id: str
    source: str
    url: str
    title: str
    summary: str
    published_at: str


class NewsState(TypedDict, total=False):
    # 실행 메타데이터
    run_id: str
    trigger: str  # schedule / workflow_dispatch / local
    started_at: str
    config: dict  # audience.yaml

    # 수집·선별
    raw_items: list[Article]
    candidates: list[Article]  # 사전 필터 이후
    prelim_labels: list[dict]  # 예선 PASS/DROP
    bodies: dict[str, str]  # item_id -> 본문
    ranked: list[dict]  # 본선 점수 순
    selected: list[str]  # 선발 item_id (대체 기사 포함)
    original_selected: list[str]  # 본선에서 처음 선발된 item_id
    reserves: list[str]  # 예비 후보 item_id

    # 요약·검수
    drafts: dict[str, dict]  # item_id -> 요약·인사이트
    review_results: dict[str, list[dict]]  # item_id -> 시도별 결과
    attempts: dict[str, int]
    replacements_used: int
    approved: list[str]
    skipped: list[str]  # 재생성 한도 초과로 제외

    # 발행
    newsletter: dict  # header, messages
    publish_result: dict  # status, message_ids
    errors: list[str]
