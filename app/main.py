import os
import re
import zipfile
import hashlib
import sqlite3
import datetime
import io
import asyncio
import logging
import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("webtoon-server")

# 라이브러리 루트: 이 폴더 바로 아래 1depth = 플랫폼(naver/kakao 등),
# 그 아래 1depth = 시리즈(웹툰) 폴더, 그 안의 zip 파일들 = 회차
LIBRARY_ROOT = os.environ.get("LIBRARY_ROOT", "/library")

# 읽음 진행률을 저장하는 SQLite 파일 (컨테이너 재시작에도 남도록 볼륨 마운트 필요)
DB_PATH = os.environ.get("DB_PATH", "/data/progress.db")

# 외부(디스코드 등)에 공개되는 URL을 만들 때 쓰는 기준 주소.
# 예: https://your-domain.example.com
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# 라이브러리 자동 재스캔 주기(초). 기본 2시간. 0 이하로 설정하면 자동 재스캔을 끈다.
RESCAN_INTERVAL_SECONDS = int(os.environ.get("RESCAN_INTERVAL_SECONDS", "7200"))

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

app = FastAPI(title="webtoon-server")

# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------


def natural_key(s: str):
    """zip 내부 이미지 파일명을 1,2,3...10 순서로 정렬하기 위한 키"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def make_id(*parts: str) -> str:
    return hashlib.sha1("::".join(parts).encode("utf-8")).hexdigest()[:12]


def parse_chapter_label(stem: str):
    """
    zip 파일명(확장자 제외)에서 (정렬키, 표시라벨) 추출.

    예)
      "103 마법사랑해 100화 - 아스라이 스..." -> (103, "100화")
      "0004_1화#64"                          -> (4,   "1화")
      "0003_프롤로그#48"                      -> (3,   "프롤로그")
      "104 마법사랑해 번외편 - 르네의 일기.." -> (104, "마법사랑해 번외편 - 르네의 일기..")
    """
    m = re.match(r"^(\d+)", stem)
    sort_key = int(m.group(1)) if m else 0

    m2 = re.search(r"(\d+\s*화)", stem)
    if m2:
        label = m2.group(1).replace(" ", "")
    else:
        label = re.sub(r"^\d+[_\s]*", "", stem)
        label = re.sub(r"#\d+$", "", label).strip(" -_")
        if not label:
            label = stem
    return sort_key, label


# ---------------------------------------------------------------------------
# 읽음 진행률 저장소 (SQLite)
# ---------------------------------------------------------------------------


def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS progress (
            series_id TEXT PRIMARY KEY,
            chapter_id TEXT NOT NULL,
            chapter_index INTEGER NOT NULL,
            page_index INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    return conn


def get_progress(series_id: str):
    conn = _db()
    try:
        row = conn.execute(
            "SELECT chapter_id, chapter_index, page_index FROM progress WHERE series_id = ?",
            (series_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"chapter_id": row[0], "chapter_index": row[1], "page_index": row[2]}


def get_all_progress():
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT series_id, chapter_id, chapter_index, page_index FROM progress"
        ).fetchall()
    finally:
        conn.close()
    return {
        r[0]: {"chapter_id": r[1], "chapter_index": r[2], "page_index": r[3]}
        for r in rows
    }


def set_progress(series_id: str, chapter_id: str, chapter_index: int, page_index: int):
    conn = _db()
    try:
        conn.execute(
            """
            INSERT INTO progress (series_id, chapter_id, chapter_index, page_index, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(series_id) DO UPDATE SET
                chapter_id = excluded.chapter_id,
                chapter_index = excluded.chapter_index,
                page_index = excluded.page_index,
                updated_at = excluded.updated_at
            """,
            (series_id, chapter_id, chapter_index, page_index, datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def delete_progress(series_id: str):
    conn = _db()
    try:
        conn.execute("DELETE FROM progress WHERE series_id = ?", (series_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 기기 상관없이 동일하게 유지되어야 하는 앱 설정 (서버에 저장)
# ---------------------------------------------------------------------------


def get_setting(key: str, default=None):
    conn = _db()
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else default


def set_setting(key: str, value: str):
    conn = _db()
    try:
        conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 시리즈 폴더 스캔 제외 관리 (플랫폼 폴더 안에 섞여 있는 웹툰 아닌 폴더 등을
# 스캔에서 빼거나 다시 넣을 때 씀). 제외해도 실제 폴더/zip 파일은 절대 건드리지 않는다.
# ---------------------------------------------------------------------------

EXCLUDED_SERIES_KEY = "excluded_series"


def get_excluded_series() -> set:
    """제외된 (platform, series_name) 튜플 집합."""
    raw = get_setting(EXCLUDED_SERIES_KEY)
    if not raw:
        return set()
    try:
        data = json.loads(raw)
        return {(item["platform"], item["series"]) for item in data if "platform" in item and "series" in item}
    except Exception:
        return set()


def set_excluded_series(pairs) -> None:
    data = [{"platform": p, "series": s} for p, s in sorted(pairs)]
    set_setting(EXCLUDED_SERIES_KEY, json.dumps(data, ensure_ascii=False))


def list_all_series_folders():
    """디스크상에 있는 (zip이 하나라도 있는) 모든 (platform, series) 폴더 목록.
    제외 여부와 무관하게 전부 보여준다 - 제외됐던 걸 다시 추가할 때 필요."""
    result = []
    if not os.path.isdir(LIBRARY_ROOT):
        return result
    for platform in sorted(os.listdir(LIBRARY_ROOT)):
        platform_path = os.path.join(LIBRARY_ROOT, platform)
        if not os.path.isdir(platform_path):
            continue
        for series_name in sorted(os.listdir(platform_path)):
            series_path = os.path.join(platform_path, series_name)
            if not os.path.isdir(series_path):
                continue
            has_zip = any(f.lower().endswith(".zip") for f in os.listdir(series_path))
            if has_zip:
                result.append({"platform": platform, "series": series_name})
    return result


# 회차의 page_index로 이 값이 저장되어 있으면 "그 회차까지 다 읽었다"는 뜻.
# /continue 조회 시 이 값을 만나면 실제 페이지 수와 비교해 다음 화로 자동 이동시킨다.
PAGE_FINISHED_SENTINEL = 1_000_000


def apply_read_boundary(series_id: str, chapters: list, index: int):
    """index번째 회차까지(포함) 읽음으로 표시. index가 음수면 전부 안읽음(진행률 삭제)."""
    if index < 0:
        delete_progress(series_id)
        return
    index = min(index, len(chapters) - 1)
    chapter = chapters[index]
    set_progress(series_id, chapter["id"], index, PAGE_FINISHED_SENTINEL)


# ---------------------------------------------------------------------------
# 라이브러리 스캔 (인메모리 카탈로그, 재시작/재스캔 시 갱신)
# ---------------------------------------------------------------------------

_catalog = {"series": {}, "chapters": {}}


def scan_library():
    series_map = {}
    chapters_map = {}

    if not os.path.isdir(LIBRARY_ROOT):
        return series_map, chapters_map

    excluded = get_excluded_series()

    for platform in sorted(os.listdir(LIBRARY_ROOT)):
        platform_path = os.path.join(LIBRARY_ROOT, platform)
        if not os.path.isdir(platform_path):
            continue

        for series_name in sorted(os.listdir(platform_path)):
            if (platform, series_name) in excluded:
                continue

            series_path = os.path.join(platform_path, series_name)
            if not os.path.isdir(series_path):
                continue

            zip_files = [f for f in os.listdir(series_path) if f.lower().endswith(".zip")]
            if not zip_files:
                continue

            series_id = make_id(platform, series_name)
            chapters = []
            for fn in zip_files:
                stem = fn[:-4]
                sort_key, label = parse_chapter_label(stem)
                chapter_id = make_id(platform, series_name, fn)
                full_path = os.path.join(series_path, fn)
                chapters.append(
                    {
                        "id": chapter_id,
                        "label": label,
                        "sort_key": sort_key,
                        "filename": fn,
                        "path": full_path,
                    }
                )
                chapters_map[chapter_id] = full_path

            chapters.sort(key=lambda c: (c["sort_key"], c["filename"]))
            latest_mtime = max((os.path.getmtime(c["path"]) for c in chapters), default=0)

            series_map[series_id] = {
                "id": series_id,
                "platform": platform,
                "title": series_name,
                "path": series_path,
                "chapters": chapters,
                "latest_mtime": latest_mtime,
            }

    return series_map, chapters_map


@app.on_event("startup")
async def startup_scan():
    s, c = scan_library()
    _catalog["series"] = s
    _catalog["chapters"] = c
    log.info(f"라이브러리 스캔 완료 - 시리즈 {len(s)}개, 회차 {len(c)}개 (경로: {LIBRARY_ROOT})")

    if RESCAN_INTERVAL_SECONDS > 0:
        minutes = RESCAN_INTERVAL_SECONDS / 60
        log.info(f"자동 재스캔 활성화 - {minutes:.0f}분마다 실행")
        asyncio.create_task(_auto_rescan_loop())
    else:
        log.info("자동 재스캔 비활성화됨 (RESCAN_INTERVAL_SECONDS <= 0)")


def _diff_and_apply_scan(s: dict, c: dict) -> tuple[int, int]:
    """새 스캔 결과를 카탈로그에 반영하고, (신규 시리즈 수, 제거된 시리즈 수)를 반환."""
    prev_ids = set(_catalog["series"].keys())
    new_ids = set(s.keys())
    added = len(new_ids - prev_ids)
    removed = len(prev_ids - new_ids)
    _catalog["series"] = s
    _catalog["chapters"] = c
    return added, removed


async def _auto_rescan_loop():
    while True:
        await asyncio.sleep(RESCAN_INTERVAL_SECONDS)
        try:
            s, c = scan_library()
            added, removed = _diff_and_apply_scan(s, c)
            if added or removed:
                log.info(f"자동 재스캔 완료 - 시리즈 {len(s)}개 (신규 {added}, 제거 {removed}), 회차 {len(c)}개")
            else:
                log.info(f"자동 재스캔 완료 - 변경 없음 (시리즈 {len(s)}개, 회차 {len(c)}개)")
        except Exception:
            # 한 번 실패해도 다음 주기에 다시 시도 - 서버가 죽으면 안 됨
            log.exception("자동 재스캔 중 오류 발생 - 다음 주기에 재시도")


@app.post("/api/rescan")
def rescan():
    s, c = scan_library()
    added, removed = _diff_and_apply_scan(s, c)
    log.info(f"수동 재스캔 완료 - 시리즈 {len(s)}개 (신규 {added}, 제거 {removed}), 회차 {len(c)}개")
    return {"series_count": len(s)}


# ---------------------------------------------------------------------------
# 시리즈 폴더 스캔 제외/포함 (플랫폼 폴더 안에 웹툰 아닌 폴더가 섞여 있을 때
# 특정 폴더만 스캔 대상에서 뺐다가 나중에 다시 넣을 수 있게 함)
# ---------------------------------------------------------------------------


@app.get("/api/series-folders")
def list_series_folders():
    """디스크상의 모든 시리즈 폴더를 스캔 중/제외됨으로 나눠서 보여준다."""
    excluded = get_excluded_series()
    all_folders = list_all_series_folders()
    return {
        "included": [f for f in all_folders if (f["platform"], f["series"]) not in excluded],
        "excluded": [f for f in all_folders if (f["platform"], f["series"]) in excluded],
    }


class SeriesFolderRef(BaseModel):
    platform: str
    series: str


@app.post("/api/series-folders/exclude")
def exclude_series_folder(body: SeriesFolderRef):
    excluded = get_excluded_series()
    excluded.add((body.platform, body.series))
    set_excluded_series(excluded)

    s, c = scan_library()
    _catalog["series"] = s
    _catalog["chapters"] = c
    log.info(f"시리즈 폴더 스캔 제외: {body.platform}/{body.series} (파일은 삭제하지 않음)")
    return {"ok": True}


@app.post("/api/series-folders/include")
def include_series_folder(body: SeriesFolderRef):
    excluded = get_excluded_series()
    excluded.discard((body.platform, body.series))
    set_excluded_series(excluded)

    s, c = scan_library()
    _catalog["series"] = s
    _catalog["chapters"] = c
    log.info(f"시리즈 폴더 다시 포함: {body.platform}/{body.series}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# zip 내부 이미지 처리
# ---------------------------------------------------------------------------


def _list_images(zip_path: str):
    with zipfile.ZipFile(zip_path) as zf:
        names = [
            n
            for n in zf.namelist()
            if not n.endswith("/") and os.path.splitext(n)[1].lower() in IMAGE_EXTS
        ]
    names.sort(key=natural_key)
    return names


# 썸네일(시리즈 커버)은 원본을 그대로 주지 않고 리사이즈+압축해서 캐싱한다.
# 웹툰 첫 페이지는 세로로 아주 긴 원본 이미지(수 MB)인 경우가 흔해서,
# 그대로 내려주면 모바일에서 목록 화면이 매우 느려지고 무거워진다.
COVER_MAX_WIDTH = 320
_cover_cache = {}  # series_id -> (source_mtime, jpeg_bytes)


def _generate_cover_bytes(zip_path: str, image_name: str) -> tuple[bytes, str]:
    with zipfile.ZipFile(zip_path) as zf:
        raw = zf.read(image_name)
    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        if img.width > COVER_MAX_WIDTH:
            ratio = COVER_MAX_WIDTH / img.width
            new_height = max(1, round(img.height * ratio))
            img = img.resize((COVER_MAX_WIDTH, new_height), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=78, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        # 변환에 실패해도(손상/미지원 포맷 등) 최소한 원본이라도 보여준다
        log.warning(f"커버 이미지 변환 실패, 원본으로 대체 - {zip_path}::{image_name} ({e})")
        ext = os.path.splitext(image_name)[1].lower()
        return raw, IMAGE_MEDIA_TYPES.get(ext, "application/octet-stream")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/series")
def list_series():
    all_progress = get_all_progress()
    result = []
    for s in _catalog["series"].values():
        total = len(s["chapters"])
        prog = all_progress.get(s["id"])
        # 진행률 기록이 없으면 전부 안읽음, 있으면 마지막으로 본 회차 이후를 안읽음으로 취급
        unread = total if not prog else max(total - prog["chapter_index"] - 1, 0)
        result.append(
            {
                "id": s["id"],
                "platform": s["platform"],
                "title": s["title"],
                "chapter_count": total,
                "unread_count": unread,
                "latest_update": s["latest_mtime"],
                "cover_url": f"/api/series/{s['id']}/cover",
            }
        )
    result.sort(key=lambda x: (x["platform"], x["title"]))
    return result


@app.get("/api/lookup/latest")
def lookup_latest(platform: str, series: str):
    """
    hermes(webtoon_checker.py 등)가 디스코드 알림에 붙일 바로가기 URL을 구할 때 쓰는 API.
    플랫폼 폴더명(예: naver)과 시리즈 폴더명을 정확히 알고 있을 때, 그 시리즈의
    최신 화로 바로 가는 URL을 돌려준다.
    """
    for s in _catalog["series"].values():
        if s["platform"] == platform and s["title"] == series:
            if not s["chapters"]:
                raise HTTPException(404, "series has no chapters")
            latest = s["chapters"][-1]
            url = None
            if PUBLIC_BASE_URL:
                url = f"{PUBLIC_BASE_URL}/reader.html?series={s['id']}&chapter={latest['id']}&page=0"
            return {
                "series_id": s["id"],
                "chapter_id": latest["id"],
                "chapter_label": latest["label"],
                "url": url,
            }
    raise HTTPException(404, "series not found")


@app.get("/api/series/{series_id}/continue")
def continue_reading(series_id: str):
    """이 시리즈를 열었을 때 바로 이동해야 할 (회차, 페이지) 반환"""
    s = _catalog["series"].get(series_id)
    if not s:
        raise HTTPException(404, "series not found")
    if not s["chapters"]:
        raise HTTPException(404, "no chapters")

    prog = get_progress(series_id)
    if prog:
        idx = next((i for i, c in enumerate(s["chapters"]) if c["id"] == prog["chapter_id"]), None)
        if idx is not None:
            page_count = len(_list_images(s["chapters"][idx]["path"]))
            # 저장된 page_index가 실제 페이지 수 이상이면 "이 회차는 다 읽음" 신호 ->
            # 다음 화가 있으면 그쪽으로, 없으면(마지막 화) 마지막 페이지로 보정
            if prog["page_index"] >= page_count and idx + 1 < len(s["chapters"]):
                nxt = s["chapters"][idx + 1]
                return {"chapter_id": nxt["id"], "page_index": 0}
            clamped_page = min(prog["page_index"], max(page_count - 1, 0))
            return {"chapter_id": prog["chapter_id"], "page_index": clamped_page}

    first = s["chapters"][0]
    return {"chapter_id": first["id"], "page_index": 0}


class ProgressIn(BaseModel):
    chapter_id: str
    page_index: int = 0


@app.put("/api/series/{series_id}/progress")
def save_progress(series_id: str, body: ProgressIn):
    s = _catalog["series"].get(series_id)
    if not s:
        raise HTTPException(404, "series not found")
    idx = next((i for i, c in enumerate(s["chapters"]) if c["id"] == body.chapter_id), None)
    if idx is None:
        raise HTTPException(404, "chapter not found in series")
    set_progress(series_id, body.chapter_id, idx, max(body.page_index, 0))
    return {"ok": True}


class ReadStateIn(BaseModel):
    scope: str  # "all" | "chapter"
    read: bool
    chapter_id: str | None = None


@app.put("/api/series/{series_id}/read-state")
def set_read_state(series_id: str, body: ReadStateIn):
    s = _catalog["series"].get(series_id)
    if not s:
        raise HTTPException(404, "series not found")
    chapters = s["chapters"]
    if not chapters:
        raise HTTPException(404, "no chapters")

    if body.scope == "all":
        target_index = len(chapters) - 1 if body.read else -1
    elif body.scope == "chapter":
        if not body.chapter_id:
            raise HTTPException(400, "chapter_id is required for scope=chapter")
        idx = next((i for i, c in enumerate(chapters) if c["id"] == body.chapter_id), None)
        if idx is None:
            raise HTTPException(404, "chapter not found in series")

        prog = get_progress(series_id)
        current_index = prog["chapter_index"] if prog else -1

        if body.read:
            # 선택한 회차 "이전(및 선택한 회차 자체)"은 모두 읽음 처리.
            # 이미 그보다 더 뒤까지 읽은 상태라면 뒤로 되돌리지 않음.
            target_index = max(current_index, idx)
        else:
            # 선택한 회차 "이후"는 모두 안읽음 처리 (선택한 회차 자체는 유지).
            # 이미 그보다 앞까지만 읽은 상태라면 앞으로 당기지 않음.
            target_index = min(current_index, idx)
    else:
        raise HTTPException(400, "scope must be 'all' or 'chapter'")

    apply_read_boundary(series_id, chapters, target_index)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 앱 설정 (검색/정렬/필터 등 기기 간 동일하게 유지할 값 저장)
# ---------------------------------------------------------------------------


@app.get("/api/settings/{key}")
def read_setting(key: str):
    return {"key": key, "value": get_setting(key)}


class SettingIn(BaseModel):
    value: str


@app.put("/api/settings/{key}")
def write_setting(key: str, body: SettingIn):
    set_setting(key, body.value)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 백업 / 복원 (읽음 진행률 + 검색/정렬/필터 설정 + 라이브러리 등록 상태 전부)
# ---------------------------------------------------------------------------

BACKUP_VERSION = 1


@app.get("/api/backup")
def export_backup():
    conn = _db()
    try:
        progress_rows = conn.execute(
            "SELECT series_id, chapter_id, chapter_index, page_index, updated_at FROM progress"
        ).fetchall()
        settings_rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    finally:
        conn.close()

    payload = {
        "version": BACKUP_VERSION,
        "exported_at": datetime.datetime.utcnow().isoformat(),
        "progress": [
            {
                "series_id": r[0],
                "chapter_id": r[1],
                "chapter_index": r[2],
                "page_index": r[3],
                "updated_at": r[4],
            }
            for r in progress_rows
        ],
        "app_settings": [{"key": r[0], "value": r[1]} for r in settings_rows],
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    filename = f"webtoon-server-backup-{datetime.date.today().isoformat()}.json"
    log.info(f"백업 생성 - progress {len(payload['progress'])}건, settings {len(payload['app_settings'])}건")
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
    conn = _db()
    try:
        conn.execute("DELETE FROM progress")
        conn.execute("DELETE FROM app_settings")

        progress_count = 0
        for p in body.progress:
            series_id = p.get("series_id")
            chapter_id = p.get("chapter_id")
            if not series_id or not chapter_id:
                continue
            conn.execute(
                """
                INSERT INTO progress (series_id, chapter_id, chapter_index, page_index, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    series_id,
                    chapter_id,
                    int(p.get("chapter_index", 0)),
                    int(p.get("page_index", 0)),
                    p.get("updated_at") or datetime.datetime.utcnow().isoformat(),
                ),
            )
            progress_count += 1

        settings_count = 0
        for s in body.app_settings:
            key = s.get("key")
            if not key:
                continue
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?)",
                (key, s.get("value", "")),
            )
            settings_count += 1

        conn.commit()
    finally:
        conn.close()

    # 라이브러리 등록 상태도 복원됐을 수 있으니 다시 스캔해서 반영
    s, c = scan_library()
    _catalog["series"] = s
    _catalog["chapters"] = c

    log.info(f"백업 복원 완료 - progress {progress_count}건, settings {settings_count}건")
    return {"ok": True, "progress_count": progress_count, "settings_count": settings_count}


@app.get("/api/series/{series_id}/chapters")
def list_chapters(series_id: str):
    s = _catalog["series"].get(series_id)
    if not s:
        raise HTTPException(404, "series not found")
    prog = get_progress(series_id)
    read_index = prog["chapter_index"] if prog else -1
    page_index = prog["page_index"] if prog else 0

    chapters_out = []
    for i, c in enumerate(s["chapters"]):
        if i < read_index or (i == read_index and page_index >= PAGE_FINISHED_SENTINEL):
            is_read, is_reading = True, False
        elif i == read_index:
            # 마지막으로 저장된 위치가 이 회차 안이고, 아직 "다 읽음" 신호(SENTINEL)가 아니면 읽는 중
            is_read, is_reading = False, True
        else:
            is_read, is_reading = False, False
        chapters_out.append(
            {
                "id": c["id"],
                "label": c["label"],
                "sort_key": c["sort_key"],
                "read": is_read,
                "reading": is_reading,
            }
        )

    return {
        "id": s["id"],
        "platform": s["platform"],
        "title": s["title"],
        "chapters": chapters_out,
    }


@app.get("/api/series/{series_id}/cover")
def series_cover(series_id: str):
    s = _catalog["series"].get(series_id)
    if not s or not s["chapters"]:
        raise HTTPException(404, "no cover")
    first_chapter = s["chapters"][0]
    names = _list_images(first_chapter["path"])
    if not names:
        raise HTTPException(404, "no cover image")

    try:
        source_mtime = os.path.getmtime(first_chapter["path"])
    except OSError:
        source_mtime = 0

    cached = _cover_cache.get(series_id)
    if cached and cached[0] == source_mtime:
        data, media_type = cached[1], cached[2]
    else:
        data, media_type = _generate_cover_bytes(first_chapter["path"], names[0])
        _cover_cache[series_id] = (source_mtime, data, media_type)

    return Response(content=data, media_type=media_type)


@app.get("/api/chapters/{chapter_id}/pages")
def chapter_pages(chapter_id: str):
    zip_path = _catalog["chapters"].get(chapter_id)
    if not zip_path:
        raise HTTPException(404, "chapter not found")
    names = _list_images(zip_path)
    return {"page_count": len(names)}


@app.get("/api/chapters/{chapter_id}/pages/{page_index}")
def chapter_page(chapter_id: str, page_index: int):
    zip_path = _catalog["chapters"].get(chapter_id)
    if not zip_path:
        raise HTTPException(404, "chapter not found")
    names = _list_images(zip_path)
    if page_index < 0 or page_index >= len(names):
        raise HTTPException(404, "page not found")
    name = names[page_index]
    ext = os.path.splitext(name)[1].lower()
    with zipfile.ZipFile(zip_path) as zf:
        data = zf.read(name)
    return Response(content=data, media_type=IMAGE_MEDIA_TYPES.get(ext, "application/octet-stream"))


# ---------------------------------------------------------------------------
# 정적 프론트엔드 (API 라우트 전부 등록된 다음 마지막에 마운트)
# ---------------------------------------------------------------------------

STATIC_DIR = os.environ.get("STATIC_DIR", "/app/static")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
