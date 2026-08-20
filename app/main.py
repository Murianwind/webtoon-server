import os
import re
import zipfile
import hashlib

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

# 라이브러리 루트: 이 폴더 바로 아래 1depth = 플랫폼(naver/kakao 등),
# 그 아래 1depth = 시리즈(웹툰) 폴더, 그 안의 zip 파일들 = 회차
LIBRARY_ROOT = os.environ.get("LIBRARY_ROOT", "/library")

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

            series_map[series_id] = {
                "id": series_id,
                "platform": platform,
                "title": series_name,
                "path": series_path,
                "chapters": chapters,
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
    result = []
    for s in _catalog["series"].values():
        result.append(
            {
                "id": s["id"],
                "platform": s["platform"],
                "title": s["title"],
                "chapter_count": len(s["chapters"]),
                "cover_url": f"/api/series/{s['id']}/cover",
            }
        )
    result.sort(key=lambda x: (x["platform"], x["title"]))
    return result


@app.get("/api/series/{series_id}/chapters")
def list_chapters(series_id: str):
    s = _catalog["series"].get(series_id)
    if not s:
        raise HTTPException(404, "series not found")
    return {
        "id": s["id"],
        "platform": s["platform"],
        "title": s["title"],
        "chapters": [
            {"id": c["id"], "label": c["label"], "sort_key": c["sort_key"]}
            for c in s["chapters"]
        ],
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
