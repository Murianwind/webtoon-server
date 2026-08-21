"""
스캔 결과를 메모리에 담아두는 카탈로그. scan.scan_library()가 만든 (series, chapters)
결과를 이 모듈이 보관하고, API 라우트들은 여기서 읽는다.

실제 파일시스템 스캔 로직은 scan.py에 있다 - 이 모듈은 "지금 알고 있는 상태"만 담당한다
(단일 책임: 상태 저장/조회, 스캔 방법은 모름).
"""

_state = {"series": {}, "chapters": {}}


def get_series_map() -> dict:
    return _state["series"]


def get_chapters_map() -> dict:
    return _state["chapters"]


def get_series(series_id: str) -> dict | None:
    return _state["series"].get(series_id)


def get_chapter_zip_path(chapter_id: str) -> str | None:
    return _state["chapters"].get(chapter_id)


def replace(series_map: dict, chapters_map: dict) -> None:
    _state["series"] = series_map
    _state["chapters"] = chapters_map


def diff_and_replace(series_map: dict, chapters_map: dict) -> tuple[int, int]:
    """새 스캔 결과로 교체하면서 (추가된 시리즈 수, 제거된 시리즈 수)를 반환."""
    prev_ids = set(_state["series"].keys())
    new_ids = set(series_map.keys())
    added = len(new_ids - prev_ids)
    removed = len(prev_ids - new_ids)
    replace(series_map, chapters_map)
    return added, removed


def find_chapter_position(chapter_id: str) -> tuple[dict | None, int | None]:
    """chapter_id가 속한 시리즈와 그 안에서의 인덱스를 찾는다. 못 찾으면 (None, None)."""
    for series in _state["series"].values():
        for index, chapter in enumerate(series["chapters"]):
            if chapter["id"] == chapter_id:
                return series, index
    return None, None
