import os
import re
import zipfile
import hashlib
import sqlite3
import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 라이브러리 루트: 이 폴더 바로 아래 1depth = 플랫폼(naver/kakao 등),
# 그 아래 1depth = 시리즈(웹툰) 폴더, 그 안의 zip 파일들 = 회차
LIBRARY_ROOT = os.environ.get("LIBRARY_ROOT", "/library")

# 읽음 진행률을 저장하는 SQLite 파일 (컨테이너 재시작에도 남도록 볼륨 마운트 필요)
DB_PATH = os.environ.get("DB_PATH", "/data/progress.db")

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

    for platform in sorted(os.listdir(LIBRARY_ROOT)):
        platform_path = os.path.join(LIBRARY_ROOT, platform)
        if not os.path.isdir(platform_path):
            continue

        for series_name in sorted(os.listdir(platform_path)):
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
def startup_scan():
    s, c = scan_library()
    _catalog["series"] = s
    _catalog["chapters"] = c


@app.post("/api/rescan")
def rescan():
    s, c = scan_library()
    _catalog["series"] = s
    _catalog["chapters"] = c
    return {"series_count": len(s)}


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
    with zipfile.ZipFile(first_chapter["path"]) as zf:
        data = zf.read(names[0])
    ext = os.path.splitext(names[0])[1].lower()
    return Response(content=data, media_type=IMAGE_MEDIA_TYPES.get(ext, "application/octet-stream"))


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
