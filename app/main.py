"""
webtoon-server FastAPI 앱.

이 파일은 라우트 정의와 앱 생명주기(시작 시 스캔, 자동 재스캔 루프)만 담당한다.
실제 로직은 각자 책임이 분리된 모듈에 있다:
  - db.py       읽음 진행률 / 설정 / 제외목록 / 겹침캐시 / 백업·복원 (SQLite)
  - catalog.py  스캔 결과를 담아두는 메모리 상태
  - scan.py     파일시스템 스캔 + 회차 라벨 파싱
  - overlap.py  화 전환 겹침(리캡) 감지 + 백그라운드 사전계산
  - covers.py   시리즈 커버 썸네일 생성/캐싱
"""

import asyncio
import json
import logging
import os
import zipfile
from datetime import date, datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import catalog, covers, db, overlap, scan

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("webtoon-server")

# 외부(디스코드 등)에 공개되는 URL을 만들 때 쓰는 기준 주소. 예: https://your-domain.example.com
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# 라이브러리 자동 재스캔 주기(초). 기본 2시간. 0 이하로 설정하면 자동 재스캔을 끈다.
RESCAN_INTERVAL_SECONDS = int(os.environ.get("RESCAN_INTERVAL_SECONDS", "7200"))

BACKUP_VERSION = 1

app = FastAPI(title="webtoon-server")


def _chapter_number_part(label: str) -> str:
    """라벨에서 제목 부분(' · ' 뒤)을 떼고 회차 번호 부분만 반환."""
    return label.split(" · ", 1)[0]


def _log_scan_result(prefix: str, series_map: dict, chapters_map: dict, added: int | None = None, removed: int | None = None) -> None:
    if added is None:
        log.info(f"{prefix} - 시리즈 {len(series_map)}개, 회차 {len(chapters_map)}개")
    elif added or removed:
        log.info(f"{prefix} - 시리즈 {len(series_map)}개 (신규 {added}, 제거 {removed}), 회차 {len(chapters_map)}개")
    else:
        log.info(f"{prefix} - 변경 없음 (시리즈 {len(series_map)}개, 회차 {len(chapters_map)}개)")


def _rescan_and_replace_catalog() -> tuple[dict, dict]:
    series_map, chapters_map = scan.scan_library()
    catalog.replace(series_map, chapters_map)
    return series_map, chapters_map


# ---------------------------------------------------------------------------
# 앱 생명주기: 시작 시 스캔, 자동/수동 재스캔
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def startup_scan():
    db.init_schema()
    series_map, chapters_map = _rescan_and_replace_catalog()
    _log_scan_result(f"라이브러리 스캔 완료 (경로: {scan.LIBRARY_ROOT})", series_map, chapters_map)
    asyncio.create_task(overlap.precompute_overlaps())

    if RESCAN_INTERVAL_SECONDS > 0:
        log.info(f"자동 재스캔 활성화 - {RESCAN_INTERVAL_SECONDS / 60:.0f}분마다 실행")
        asyncio.create_task(_auto_rescan_loop())
    else:
        log.info("자동 재스캔 비활성화됨 (RESCAN_INTERVAL_SECONDS <= 0)")


async def _auto_rescan_loop():
    while True:
        await asyncio.sleep(RESCAN_INTERVAL_SECONDS)
        try:
            series_map, chapters_map = scan.scan_library()
            added, removed = catalog.diff_and_replace(series_map, chapters_map)
            _log_scan_result("자동 재스캔 완료", series_map, chapters_map, added, removed)
            asyncio.create_task(overlap.precompute_overlaps())
        except Exception:
            # 한 번 실패해도 다음 주기에 다시 시도 - 서버가 죽으면 안 됨
            log.exception("자동 재스캔 중 오류 발생 - 다음 주기에 재시도")


@app.post("/api/rescan")
async def rescan():
    series_map, chapters_map = scan.scan_library()
    added, removed = catalog.diff_and_replace(series_map, chapters_map)
    _log_scan_result("수동 재스캔 완료", series_map, chapters_map, added, removed)
    asyncio.create_task(overlap.precompute_overlaps())
    return {"series_count": len(series_map)}


@app.get("/api/scan-status")
def scan_status():
    """설정 패널 등에 표시할 마지막 스캔 시각."""
    return {"last_scan_at": catalog.get_last_scan_display()}


# ---------------------------------------------------------------------------
# 시리즈 폴더 스캔 제외/포함 (플랫폼 폴더 안에 웹툰 아닌 폴더가 섞여 있을 때
# 특정 폴더만 스캔 대상에서 뺐다가 나중에 다시 넣을 수 있게 함)
# ---------------------------------------------------------------------------


@app.get("/api/series-folders")
def list_series_folders():
    """디스크상의 모든 시리즈 폴더를 스캔 중/제외됨으로 나눠서 보여준다."""
    excluded = db.get_excluded_series()
    all_folders = scan.list_all_series_folders()
    return {
        "included": [f for f in all_folders if (f["platform"], f["series"]) not in excluded],
        "excluded": [f for f in all_folders if (f["platform"], f["series"]) in excluded],
    }


class SeriesFolderRef(BaseModel):
    platform: str
    series: str


@app.post("/api/series-folders/exclude")
def exclude_series_folder(body: SeriesFolderRef):
    excluded = db.get_excluded_series()
    excluded.add((body.platform, body.series))
    db.set_excluded_series(excluded)
    _rescan_and_replace_catalog()
    log.info(f"시리즈 폴더 스캔 제외: {body.platform}/{body.series} (파일은 삭제하지 않음)")
    return {"ok": True}


@app.post("/api/series-folders/include")
def include_series_folder(body: SeriesFolderRef):
    excluded = db.get_excluded_series()
    excluded.discard((body.platform, body.series))
    db.set_excluded_series(excluded)
    _rescan_and_replace_catalog()
    log.info(f"시리즈 폴더 다시 포함: {body.platform}/{body.series}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# 시리즈 목록 / 조회
# ---------------------------------------------------------------------------


@app.get("/api/series")
def list_series():
    all_progress = db.get_all_progress()
    result = []
    for series in catalog.get_series_map().values():
        total = len(series["chapters"])
        prog = all_progress.get(series["id"])
        # 진행률 기록이 없으면 전부 안읽음, 있으면 마지막으로 본 회차 이후를 안읽음으로 취급
        unread = total if not prog else max(total - prog["chapter_index"] - 1, 0)

        if total == 0:
            progress_display = ""
        elif unread == 0:
            progress_display = "완독"
        else:
            # 다음에 읽어야 할(또는 읽는 중인) 회차 번호 / 마지막 회차 번호
            next_idx = max(0, min(total - unread, total - 1))
            current_label = _chapter_number_part(series["chapters"][next_idx]["label"])
            last_label = _chapter_number_part(series["chapters"][-1]["label"])
            progress_display = f"{current_label}/{last_label}"

        result.append(
            {
                "id": series["id"],
                "platform": series["platform"],
                "title": series["title"],
                "chapter_count": total,
                "unread_count": unread,
                "progress_display": progress_display,
                "latest_update": series["latest_mtime"],
                "cover_url": f"/api/series/{series['id']}/cover",
            }
        )
    result.sort(key=lambda item: (item["platform"], item["title"]))
    return result


@app.get("/api/lookup/latest")
def lookup_latest(series: str, platform: str | None = None):
    """
    hermes(webtoon_checker.py 등)가 디스코드 알림에 붙일 바로가기 URL을 구할 때 쓰는 API.
    시리즈 폴더명만으로 찾을 수 있다 - platform은 선택사항이며, 여러 플랫폼에 같은 이름의
    시리즈가 있어 구분이 필요할 때만 넘기면 된다. (platform을 필수로 요구하면, 서버 쪽
    /library 폴더명을 나중에 바꿀 때마다 호출하는 쪽 코드도 같이 고쳐야 하는 문제가 있었음)
    """
    for candidate in catalog.get_series_map().values():
        if candidate["title"] != series:
            continue
        if platform is not None and candidate["platform"] != platform:
            continue
        if not candidate["chapters"]:
            raise HTTPException(404, "series has no chapters")
        latest = candidate["chapters"][-1]
        url = None
        if PUBLIC_BASE_URL:
            url = f"{PUBLIC_BASE_URL}/reader.html?series={candidate['id']}&chapter={latest['id']}&page=0"
        return {
            "series_id": candidate["id"],
            "chapter_id": latest["id"],
            "chapter_label": latest["label"],
            "url": url,
        }
    raise HTTPException(404, "series not found")


@app.get("/api/series/{series_id}/continue")
def continue_reading(series_id: str):
    """이 시리즈를 열었을 때 바로 이동해야 할 (회차, 페이지) 반환."""
    series = catalog.get_series(series_id)
    if not series:
        raise HTTPException(404, "series not found")
    if not series["chapters"]:
        raise HTTPException(404, "no chapters")

    prog = db.get_progress(series_id)
    if prog:
        idx = next((i for i, ch in enumerate(series["chapters"]) if ch["id"] == prog["chapter_id"]), None)
        if idx is not None:
            page_count = len(scan.list_zip_image_names(series["chapters"][idx]["path"]))
            # 저장된 page_index가 실제 페이지 수 이상이면 "이 회차는 다 읽음" 신호 ->
            # 다음 화가 있으면 그쪽으로, 없으면(마지막 화) 마지막 페이지로 보정
            if prog["page_index"] >= page_count and idx + 1 < len(series["chapters"]):
                next_chapter = series["chapters"][idx + 1]
                return {"chapter_id": next_chapter["id"], "page_index": 0}
            clamped_page = min(prog["page_index"], max(page_count - 1, 0))
            return {"chapter_id": prog["chapter_id"], "page_index": clamped_page}

    first_chapter = series["chapters"][0]
    return {"chapter_id": first_chapter["id"], "page_index": 0}


class ProgressIn(BaseModel):
    chapter_id: str
    page_index: int = 0


@app.put("/api/series/{series_id}/progress")
def save_progress(series_id: str, body: ProgressIn):
    series = catalog.get_series(series_id)
    if not series:
        raise HTTPException(404, "series not found")
    idx = next((i for i, ch in enumerate(series["chapters"]) if ch["id"] == body.chapter_id), None)
    if idx is None:
        raise HTTPException(404, "chapter not found in series")
    db.set_progress(series_id, body.chapter_id, idx, max(body.page_index, 0))
    return {"ok": True}


class ReadStateIn(BaseModel):
    scope: str  # "all" | "chapter"
    read: bool
    chapter_id: str | None = None


@app.put("/api/series/{series_id}/read-state")
def set_read_state(series_id: str, body: ReadStateIn):
    series = catalog.get_series(series_id)
    if not series:
        raise HTTPException(404, "series not found")
    chapters = series["chapters"]
    if not chapters:
        raise HTTPException(404, "no chapters")

    if body.scope == "all":
        target_index = len(chapters) - 1 if body.read else -1
    elif body.scope == "chapter":
        if not body.chapter_id:
            raise HTTPException(400, "chapter_id is required for scope=chapter")
        idx = next((i for i, ch in enumerate(chapters) if ch["id"] == body.chapter_id), None)
        if idx is None:
            raise HTTPException(404, "chapter not found in series")

        prog = db.get_progress(series_id)
        current_index = prog["chapter_index"] if prog else -1

        if body.read:
            # 선택한 회차 "이전(및 선택한 회차 자체)"은 모두 읽음 처리.
            # 이미 그보다 더 뒤까지 읽은 상태라면 뒤로 되돌리지 않음.
            target_index = max(current_index, idx)
        else:
            # 선택한 회차 "부터(포함)" 안읽음 처리 (선택한 회차 자체도 안읽음이 됨).
            # 이미 그보다 앞까지만 읽은 상태라면 앞으로 당기지 않음.
            target_index = min(current_index, idx - 1)
    else:
        raise HTTPException(400, "scope must be 'all' or 'chapter'")

    db.apply_read_boundary(series_id, chapters, target_index)
    return {"ok": True}


@app.get("/api/series/{series_id}/chapters")
def list_chapters(series_id: str):
    series = catalog.get_series(series_id)
    if not series:
        raise HTTPException(404, "series not found")
    prog = db.get_progress(series_id)
    read_index = prog["chapter_index"] if prog else -1
    page_index = prog["page_index"] if prog else 0

    chapters_out = []
    for i, chapter in enumerate(series["chapters"]):
        if i < read_index or (i == read_index and page_index >= db.PAGE_FINISHED_SENTINEL):
            is_read, is_reading = True, False
        elif i == read_index:
            # 마지막으로 저장된 위치가 이 회차 안이고, 아직 "다 읽음" 신호(SENTINEL)가 아니면 읽는 중
            is_read, is_reading = False, True
        else:
            is_read, is_reading = False, False
        chapters_out.append(
            {
                "id": chapter["id"],
                "label": chapter["label"],
                "sort_key": chapter["sort_key"],
                "read": is_read,
                "reading": is_reading,
            }
        )

    return {
        "id": series["id"],
        "platform": series["platform"],
        "title": series["title"],
        "chapters": chapters_out,
    }


@app.get("/api/series/{series_id}/cover")
def series_cover(series_id: str):
    series = catalog.get_series(series_id)
    if not series or not series["chapters"]:
        raise HTTPException(404, "no cover")
    first_chapter = series["chapters"][0]
    names = scan.list_zip_image_names(first_chapter["path"])
    if not names:
        raise HTTPException(404, "no cover image")

    try:
        source_mtime = os.path.getmtime(first_chapter["path"])
    except OSError:
        source_mtime = 0

    cached = covers.get_cached_cover(series_id, source_mtime)
    if cached:
        data, media_type = cached
    else:
        data, media_type = covers.generate_and_cache_cover(
            series_id, source_mtime, first_chapter["path"], names[0]
        )

    return Response(content=data, media_type=media_type)


# ---------------------------------------------------------------------------
# 앱 설정 (검색/정렬/필터 등 기기 간 동일하게 유지할 값 저장)
# ---------------------------------------------------------------------------


@app.get("/api/settings/{key}")
def read_setting(key: str):
    return {"key": key, "value": db.get_setting(key)}


class SettingIn(BaseModel):
    value: str


@app.put("/api/settings/{key}")
def write_setting(key: str, body: SettingIn):
    db.set_setting(key, body.value)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 백업 / 복원 (읽음 진행률 + 검색/정렬/필터 설정 + 라이브러리 등록 상태 전부)
# ---------------------------------------------------------------------------


@app.get("/api/backup")
def export_backup():
    data = db.export_backup_data()
    payload = {
        "version": BACKUP_VERSION,
        "exported_at": datetime.utcnow().isoformat(),
        "progress": data["progress"],
        "app_settings": data["app_settings"],
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    filename = f"webtoon-server-backup-{date.today().isoformat()}.json"
    log.info(f"백업 생성 - progress {len(data['progress'])}건, settings {len(data['app_settings'])}건")
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class RestorePayload(BaseModel):
    version: int | None = None
    progress: list = []
    app_settings: list = []


@app.post("/api/restore")
def import_backup(body: RestorePayload):
    progress_count, settings_count = db.import_backup_data(body.progress, body.app_settings)

    # 라이브러리 등록(제외 목록) 상태도 복원됐을 수 있으니 다시 스캔해서 반영
    _rescan_and_replace_catalog()

    log.info(f"백업 복원 완료 - progress {progress_count}건, settings {settings_count}건")
    return {"ok": True, "progress_count": progress_count, "settings_count": settings_count}


# ---------------------------------------------------------------------------
# zip 내부 이미지 / 화 전환 겹침
# ---------------------------------------------------------------------------


@app.get("/api/chapters/{chapter_id}/pages")
def chapter_pages(chapter_id: str):
    zip_path = catalog.get_chapter_zip_path(chapter_id)
    if not zip_path:
        raise HTTPException(404, "chapter not found")
    names = scan.list_zip_image_names(zip_path)
    return {"page_count": len(names)}


@app.get("/api/chapters/{chapter_id}/overlap")
def chapter_overlap(chapter_id: str):
    """
    이 회차 맨 앞부분이 바로 이전 회차(같은 시리즈, 정렬상 직전) 끝부분과 겹치는
    페이지 수를 반환. 결과는 DB에 캐싱되어 다음부터는 즉시 응답한다.
    """
    zip_path = catalog.get_chapter_zip_path(chapter_id)
    if not zip_path:
        raise HTTPException(404, "chapter not found")

    series, index = catalog.find_chapter_position(chapter_id)
    prev_chapter = None
    if series is not None and index is not None and index > 0:
        prev_chapter = series["chapters"][index - 1]

    if not prev_chapter:
        return {"skip_pages": 0}

    cached = db.get_cached_overlap(chapter_id)
    if cached is not None:
        return {"skip_pages": cached}

    skip_pages = overlap.compute_overlap_pages(prev_chapter["path"], zip_path)
    db.set_cached_overlap(chapter_id, prev_chapter["id"], skip_pages)
    if skip_pages > 0:
        log.info(f"화 전환 겹침 감지: {chapter_id} 앞부분 {skip_pages}페이지가 이전 화와 중복 (자동 건너뜀)")
    return {"skip_pages": skip_pages}


@app.get("/api/chapters/{chapter_id}/pages/{page_index}")
def chapter_page(chapter_id: str, page_index: int):
    zip_path = catalog.get_chapter_zip_path(chapter_id)
    if not zip_path:
        raise HTTPException(404, "chapter not found")
    names = scan.list_zip_image_names(zip_path)
    if page_index < 0 or page_index >= len(names):
        raise HTTPException(404, "page not found")
    name = names[page_index]
    ext = os.path.splitext(name)[1].lower()
    with zipfile.ZipFile(zip_path) as zf:
        data = zf.read(name)
    return Response(content=data, media_type=covers.IMAGE_MEDIA_TYPES.get(ext, "application/octet-stream"))


# ---------------------------------------------------------------------------
# 정적 프론트엔드 (API 라우트 전부 등록된 다음 마지막에 마운트)
# ---------------------------------------------------------------------------

STATIC_DIR = os.environ.get("STATIC_DIR", "/app/static")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
