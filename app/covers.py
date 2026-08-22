"""
시리즈 커버(썸네일) 생성. 웹툰 첫 페이지는 세로로 아주 긴 원본 이미지(수 MB)인 경우가
흔해서, 그대로 내려주면 모바일에서 목록 화면이 매우 느려지고 무거워진다. 그래서
리사이즈+JPEG 압축한 결과를 원본 mtime 기준으로 캐싱해서 재사용한다.
"""

import io
import logging
import os
import zipfile

from PIL import Image

log = logging.getLogger("webtoon-server")

COVER_MAX_WIDTH = 320

IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# series_id -> (source_mtime, jpeg_bytes, media_type)
_cache: dict[str, tuple[float, bytes, str]] = {}


def get_cached_cover(series_id: str, source_mtime: float) -> tuple[bytes, str] | None:
    """원본이 그 사이에 바뀌지 않았으면(mtime 동일) 캐시된 것을 반환, 아니면 None."""
    cached = _cache.get(series_id)
    if cached and cached[0] == source_mtime:
        return cached[1], cached[2]
    return None


def generate_and_cache_cover_from_zip(series_id: str, source_mtime: float, zip_path: str, image_name: str) -> tuple[bytes, str]:
    with zipfile.ZipFile(zip_path) as zf:
        raw = zf.read(image_name)
    ext = os.path.splitext(image_name)[1].lower()
    data, media_type = _resize_and_compress(raw, ext, source_label=f"{zip_path}::{image_name}")
    _cache[series_id] = (source_mtime, data, media_type)
    return data, media_type


def generate_and_cache_cover_from_file(series_id: str, source_mtime: float, file_path: str) -> tuple[bytes, str]:
    """cover.jpg처럼 zip 밖에 별도로 있는 대표 이미지 파일로 커버를 만든다."""
    with open(file_path, "rb") as f:
        raw = f.read()
    ext = os.path.splitext(file_path)[1].lower()
    data, media_type = _resize_and_compress(raw, ext, source_label=file_path)
    _cache[series_id] = (source_mtime, data, media_type)
    return data, media_type


def _resize_and_compress(raw: bytes, ext: str, source_label: str) -> tuple[bytes, str]:
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
        log.warning(f"커버 이미지 변환 실패, 원본으로 대체 - {source_label} ({e})")
        return raw, IMAGE_MEDIA_TYPES.get(ext, "application/octet-stream")
