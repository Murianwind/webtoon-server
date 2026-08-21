from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

BASE_DIR = Path(r"D:\Downloads\Webtoon\Webtoon_Download")
ID_LIST_PATH = Path(r"D:\Downloads\Webtoon\id_list.txt")
NAVER_WEBTOON_URL = "https://comic.naver.com/webtoon"
NAVER_WEEKDAY_API = "https://comic.naver.com/api/webtoon/titlelist/weekday"
# Webtoon check 전용 Discord webhook. RSS/Notification webhook과 혼동하지 말 것.
WEBTOON_WEBHOOK_URL = "https://discord.com/api/webhooks/1518366757918736426/ui06MGRya6liLrrjfHvaxmzobn3T6duI6HW4DtSucBYFUVWhe5Dfs34TIpjCcuNTCdeX"
# webtoon-server는 이 PC(192.168.0.49)에서 25600 포트로 떠 있으므로 로컬 호출로 조회한다.
# 알림에 실제로 붙는 바로가기 URL은 서버 쪽 PUBLIC_BASE_URL 설정을 따라간다.
WEBTOON_SERVER_LOCAL_URL = os.getenv("WEBTOON_SERVER_LOCAL_URL", "http://127.0.0.1:25600")
WEBTOON_SERVER_PLATFORM = "naver"  # 이 스크립트는 Webtoon_Download(네이버) 폴더만 다룸
MAX_DISCORD_CHARS = 1900
WEEK_PARAMS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# ----------------------------------------------------------------------
# 로깅 (webtoon_manager.py와 동일한 방식: 파일 하나만 유지, 3일 지난 줄은 삭제)
# ----------------------------------------------------------------------
LOG_FILE_PATH = r"C:\Users\y2k00\AppData\Local\hermes\scripts\webtoon_checker.log"
LOG_RETENTION_DAYS = 3


def _trim_log_file(path: str, days: int):
    """로그 파일을 하나만 유지하면서, days일보다 오래된 줄은 지운다."""
    if not os.path.exists(path):
        return
    cutoff = datetime.now() - timedelta(days=days)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return
    kept = []
    for line in lines:
        m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),", line)
        if m:
            try:
                if datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") < cutoff:
                    continue
            except ValueError:
                pass
        kept.append(line)
    if len(kept) != len(lines):
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(kept)


_trim_log_file(LOG_FILE_PATH, LOG_RETENTION_DAYS)

log = logging.getLogger("webtoon_checker")
log.setLevel(logging.INFO)
_log_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_log_handler)

WEBTOON_SERVER_RETRY_COUNT = 2
WEBTOON_SERVER_RETRY_DELAY_SEC = 2


@dataclass(frozen=True)
class WatchedWebtoon:
    title: str
    title_id: int | None = None


@dataclass(frozen=True)
class UpdatedWebtoon:
    title: str
    title_id: int
    author: str = ""


@dataclass(frozen=True)
class DownloadedWebtoon:
    webtoon: UpdatedWebtoon
    folder: Path
    total_size: int
    url: str | None = None


@dataclass(frozen=True)
class Report:
    updated_watched: list[UpdatedWebtoon]
    downloaded: list[DownloadedWebtoon]
    missing: list[UpdatedWebtoon]
    zero_kb_files_in_downloaded_folders: list[Path]
    modified_folders: list[Path]
    id_list_count: int
    naver_up_count: int
    week: str


def local_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts).astimezone()


def fmt_dt(ts: float) -> str:
    return local_dt(ts).strftime("%Y-%m-%d %H:%M:%S %Z")


def is_today(ts: float, today: date) -> bool:
    return local_dt(ts).date() == today


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    # Windows 폴더명에서 바뀌거나 제거되기 쉬운 문자/공백을 비교에서 제외한다.
    value = re.sub(r'[\\/:*?"<>|\s]+', "", value)
    return value


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(BASE_DIR))
    except ValueError:
        return str(path)


def read_watched_webtoons(path: Path = ID_LIST_PATH) -> list[WatchedWebtoon]:
    if not path.exists():
        fallback = Path(__file__).with_name("id_list.txt")
        if fallback.exists():
            path = fallback
        else:
            raise FileNotFoundError(f"id_list.txt not found: {path}")

    watched: list[WatchedWebtoon] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^(?P<title>.+?)\s+(?P<title_id>\d+)\s*$", line)
        if match:
            watched.append(WatchedWebtoon(match.group("title").strip(), int(match.group("title_id"))))
        else:
            watched.append(WatchedWebtoon(line, None))
    return watched


def today_week_param() -> str:
    return WEEK_PARAMS[datetime.now().astimezone().weekday()]


def fetch_today_updated_webtoons(week: str | None = None) -> list[UpdatedWebtoon]:
    week = week or today_week_param()
    url = f"{NAVER_WEEKDAY_API}?week={week}&order=user"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Referer": f"{NAVER_WEBTOON_URL}?tab={week}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Hermes Webtoon Checker/2.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    updated: list[UpdatedWebtoon] = []
    for item in payload.get("titleList", []):
        if not item.get("up"):
            continue
        title_id = item.get("titleId")
        title = item.get("titleName")
        if not isinstance(title_id, int) or not title:
            continue
        updated.append(UpdatedWebtoon(title=str(title), title_id=title_id, author=str(item.get("author") or "")))
    return updated


def filter_watched_updates(updated: Iterable[UpdatedWebtoon], watched: Iterable[WatchedWebtoon]) -> list[UpdatedWebtoon]:
    watched_ids = {w.title_id for w in watched if w.title_id is not None}
    watched_titles = {normalize_name(w.title) for w in watched}
    matched = [u for u in updated if u.title_id in watched_ids or normalize_name(u.title) in watched_titles]
    matched.sort(key=lambda u: (u.title not in {w.title for w in watched}, u.title))
    return matched


def folder_total_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        root_path = Path(root)
        for name in files:
            p = root_path / name
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


def collect_modified_folders_and_zero_files(today: date) -> tuple[list[Path], list[Path]]:
    modified_folders: list[Path] = []
    zero_kb_files: list[Path] = []

    if not BASE_DIR.exists():
        raise FileNotFoundError(f"Base directory does not exist: {BASE_DIR}")
    if not BASE_DIR.is_dir():
        raise NotADirectoryError(f"Base path is not a directory: {BASE_DIR}")

    for root, dirs, files in os.walk(BASE_DIR):
        root_path = Path(root)
        for dirname in dirs:
            p = root_path / dirname
            try:
                st = p.stat()
            except OSError:
                continue
            if is_today(st.st_mtime, today):
                modified_folders.append(p)
        for filename in files:
            p = root_path / filename
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size <= 10 * 1024 and is_today(st.st_mtime, today):
                zero_kb_files.append(p)

    modified_folders.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    zero_kb_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return modified_folders, zero_kb_files


def find_matching_folder(title: str, modified_folders: list[Path]) -> Path | None:
    target = normalize_name(title)
    exact_matches: list[Path] = []
    partial_matches: list[Path] = []
    for folder in modified_folders:
        normalized_parts = [normalize_name(part) for part in folder.relative_to(BASE_DIR).parts]
        basename = normalize_name(folder.name)
        if target in normalized_parts or basename == target:
            exact_matches.append(folder)
        elif target and any(target in part or part in target for part in normalized_parts):
            partial_matches.append(folder)

    candidates = exact_matches or partial_matches
    if not candidates:
        return None
    candidates.sort(key=lambda p: (len(p.parts), -p.stat().st_mtime))
    return candidates[0]


def is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def series_folder_name(folder: Path) -> str | None:
    """BASE_DIR 바로 아래의 '시리즈 폴더' 이름을 얻는다 (webtoon-server가 스캔하는 단위와 동일)."""
    try:
        parts = folder.relative_to(BASE_DIR).parts
    except ValueError:
        return None
    return parts[0] if parts else None


def get_webtoon_url(folder: Path) -> str | None:
    """
    webtoon-server의 /api/lookup/latest를 호출해 이 시리즈 폴더의 최신 화 바로가기 URL을 가져온다.
    일시적 오류(타임아웃/연결거부/5xx)에는 짧게 재시도한다.
    """
    series_name = series_folder_name(folder)
    log_prefix = f"[webtoon-server] '{series_name}':"
    if not series_name:
        log.warning(f"[webtoon-server] 시리즈 폴더명을 판별하지 못함: {folder}")
        return None

    params = urllib.parse.urlencode({"platform": WEBTOON_SERVER_PLATFORM, "series": series_name})
    url = f"{WEBTOON_SERVER_LOCAL_URL}/api/lookup/latest?{params}"

    last_exc: Exception | None = None
    for attempt in range(1, WEBTOON_SERVER_RETRY_COUNT + 2):  # 최초 시도 + 재시도
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            webtoon_url = data.get("url")
            if not webtoon_url:
                log.warning(f"{log_prefix} lookup은 성공했지만 url이 비어있음 "
                            f"(PUBLIC_BASE_URL이 서버에 설정되어 있는지 확인 필요)")
            return webtoon_url
        except urllib.error.HTTPError as e:
            if e.code == 404:
                log.warning(f"{log_prefix} webtoon-server에서 해당 시리즈를 찾지 못함 (404)")
                return None
            body = e.read().decode("utf-8", errors="replace")[:300]
            log.error(f"{log_prefix} HTTP 오류 {e.code} (시도 {attempt}/{WEBTOON_SERVER_RETRY_COUNT + 1}): {body}")
            last_exc = e
        except Exception as e:
            log.error(f"{log_prefix} 요청 예외 (시도 {attempt}/{WEBTOON_SERVER_RETRY_COUNT + 1}): {e!r}")
            last_exc = e
        if attempt <= WEBTOON_SERVER_RETRY_COUNT:
            time.sleep(WEBTOON_SERVER_RETRY_DELAY_SEC)

    log.error(f"{log_prefix} 재시도 {WEBTOON_SERVER_RETRY_COUNT}회 모두 실패, 포기함: {last_exc!r}")
    return None


def build_report() -> Report:
    today = date.today()
    week = today_week_param()
    watched = read_watched_webtoons()
    updated = fetch_today_updated_webtoons(week)
    updated_watched = filter_watched_updates(updated, watched)
    modified_folders, zero_kb_files_today = collect_modified_folders_and_zero_files(today)

    downloaded: list[DownloadedWebtoon] = []
    missing: list[UpdatedWebtoon] = []
    url_failures = []
    for webtoon in updated_watched:
        folder = find_matching_folder(webtoon.title, modified_folders)
        if folder is None:
            missing.append(webtoon)
        else:
            url = get_webtoon_url(folder)
            if url is None:
                url_failures.append(webtoon.title)
            downloaded.append(DownloadedWebtoon(webtoon=webtoon, folder=folder, total_size=folder_total_size(folder), url=url))

    if url_failures:
        log.error(f"[webtoon-server] 바로가기 URL을 못 만든 작품 {len(url_failures)}개: {url_failures}")

    downloaded_folders = [item.folder for item in downloaded]
    zero_kb_files_in_downloaded_folders = [
        p for p in zero_kb_files_today if any(is_under(p, folder) for folder in downloaded_folders)
    ]
    return Report(
        updated_watched=updated_watched,
        downloaded=downloaded,
        missing=missing,
        zero_kb_files_in_downloaded_folders=zero_kb_files_in_downloaded_folders,
        modified_folders=modified_folders,
        id_list_count=len(watched),
        naver_up_count=len(updated),
        week=week,
    )


def section(title: str, rows: list[str], empty: str) -> str:
    if not rows:
        return f"**{title}**\n{empty}"
    return f"**{title} ({len(rows)})**\n" + "\n".join(rows)


def sized_folder_row(item: DownloadedWebtoon) -> str:
    size_mb = item.total_size / (1024 * 1024)
    return f"- `{item.webtoon.title}` → `{rel(item.folder)}` ({size_mb:.1f} MB)"


def build_message(report: Report) -> str:
    check_date = date.today().isoformat()
    downloaded_rows = [f"• {item.webtoon.title} [바로가기]({item.url})" if item.url else f"• {item.webtoon.title}" for item in report.downloaded]
    missing_rows = [w.title for w in report.missing]
    zero_file_rows = [rel(p) for p in report.zero_kb_files_in_downloaded_folders]

    parts = [
        f"📅 웹툰 업데이트 확인 ({check_date})",
        "",
        "📁 오늘 다운로드된 폴더:",
        "\n".join(downloaded_rows) if downloaded_rows else "없음",
    ]

    if missing_rows:
        parts.extend([
            "",
            "❌ 다운로드 되지 않은 폴더:",
            "\n".join(missing_rows),
        ])

    if zero_file_rows:
        parts.extend([
            "",
            "⚠️ 다운로드 된 폴더의 오늘 수정된 파일 중 크기가 10KB 이하인 파일 목록:",
            "\n".join(zero_file_rows),
        ])

    message = "\n".join(parts)
    if len(message) <= MAX_DISCORD_CHARS:
        return message

    downloaded_limit = 40
    missing_limit = 40
    zero_file_limit = 40
    truncated_parts = [
        f"📅 웹툰 업데이트 확인 ({check_date})",
        "",
        "📁 오늘 다운로드된 폴더:",
        "\n".join(downloaded_rows[:downloaded_limit]) if downloaded_rows else "없음",
    ]

    if missing_rows:
        truncated_parts.extend([
            "",
            "❌ 다운로드 되지 않은 폴더:",
            "\n".join(missing_rows[:missing_limit]),
        ])

    if zero_file_rows:
        truncated_parts.extend([
            "",
            "⚠️ 다운로드 된 폴더의 오늘 수정된 파일 중 크기가 10KB 이하인 파일 목록:",
            "\n".join(zero_file_rows[:zero_file_limit]),
        ])
    omitted = []
    for label, total, limit in [
        ("수정 폴더", len(downloaded_rows), downloaded_limit),
        ("누락", len(missing_rows), missing_limit),
        ("10KB 이하 파일", len(zero_file_rows), zero_file_limit),
    ]:
        if total > limit:
            omitted.append(f"{label} {total - limit}개")
    if omitted:
        truncated_parts.extend(["", f"_Discord 길이 제한으로 {'; '.join(omitted)} 추가 생략._"])
    return "\n".join(truncated_parts)[:MAX_DISCORD_CHARS]


def post_discord(webhook_url: str, content: str) -> int:
    payload = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Hermes Webtoon Checker/2.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord webhook failed: HTTP {e.code}: {body}") from e


def main() -> int:
    log.info("[RUN] webtoon_checker 시작")
    try:
        report = build_report()
        content = build_message(report)

        dry_run = os.environ.get("WEBTOON_CHECKER_DRY_RUN") == "1"
        if dry_run:
            print(content)
            status = 0
        else:
            status = post_discord(WEBTOON_WEBHOOK_URL, content)

        if os.environ.get("WEBTOON_CHECKER_VERBOSE") == "1":
            print(f"DISCORD_STATUS={status}")
            print(f"WEBHOOK_PURPOSE=webtoon")
            print(f"NAVER_WEEK={report.week}")
            print(f"NAVER_UP_COUNT={report.naver_up_count}")
            print(f"ID_LIST_COUNT={report.id_list_count}")
            print(f"MATCHED_UPDATED_WEBTOONS={len(report.updated_watched)}")
            print(f"DOWNLOADED_FOLDERS={len(report.downloaded)}")
            print(f"MISSING_FOLDERS={len(report.missing)}")
            print(f"ZERO_KB_FILES_IN_DOWNLOADED_FOLDERS={len(report.zero_kb_files_in_downloaded_folders)}")

        url_ok = sum(1 for d in report.downloaded if d.url)
        log.info(f"[RUN] webtoon_checker 종료 (discord_status={status}, 다운로드폴더={len(report.downloaded)}, "
                 f"바로가기url_성공={url_ok}/{len(report.downloaded)}, 누락={len(report.missing)})")
        return 0
    except Exception:
        log.exception("[RUN] webtoon_checker 실행 중 예외 발생")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
