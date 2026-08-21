# webtoon-server

zip으로 저장해둔 웹툰 회차를 폴더 기반으로 인식해서, 세로 스크롤로 읽을 수 있게 해주는
개인용(셀프호스팅) 웹툰 뷰어입니다. Komga 같은 범용 만화 서버 대신, 웹툰 특유의
"세로 스크롤 + 무한 이어보기 + 기기 간 진행률 동기화"에 맞춰 만들었습니다.

> ⚠️ **이 저장소는 뷰어 소프트웨어만 담고 있습니다.** 웹툰 콘텐츠(zip 파일)는 포함되어 있지
> 않으며, 어디서도 제공하지 않습니다. 각자 정당하게 보유한 콘텐츠로만 사용해주세요.

## 주요 기능

- 폴더 기반 라이브러리 스캔 (플랫폼 폴더 → 시리즈 폴더 → 회차 zip)
- 시리즈 목록 그리드 UI + 검색 + 정렬(최근 업데이트순 / 안읽은 회차 많은순 / 제목순 / 읽음 상태별 필터)
- 시리즈 클릭 시 이전에 읽던 위치(또는 처음 회차)로 바로 이동
- 세로 스크롤 리더: 회차 끝에 도달하면 자동으로 다음 화 이어서 로드(무한 스크롤), 다음 화 프리페치
- 리더 사이드 패널: 전체 회차 목록 + 현재 보고 있는 회차 표시
- 회차 단위 읽음/안읽음 수동 표시 (전체 또는 특정 회차 기준)
- 읽음 진행률은 SQLite에 저장되어 여러 기기에서 접속해도 이어보기 가능
- 라이브러리 자동/수동 재스캔
- PWA 아이콘 지원 (아이폰/안드로이드 홈 화면에 추가 가능)
- 외부 알림 스크립트 등에서 특정 회차로 바로 연결되는 링크를 만들 수 있는 조회 API

## 폴더 구조 규칙

```
LIBRARY_ROOT (컨테이너 내부 /library)
 ├ naver/                 ← 플랫폼 폴더 (이름은 자유, UI에 태그로 표시됨)
 │   └ 어떤 웹툰 제목/       ← 시리즈 폴더 = 웹툰 한 편
 │       ├ 103 ... 100화 - ...zip
 │       └ 104 ... 번외편 ...zip
 └ kakao/
     └ 다른 웹툰 제목/
         ├ 0003_프롤로그#48.zip
         └ 0004_1화#64.zip
```

- 회차 정렬은 zip 파일명 맨 앞 숫자 기준
- 표시 라벨은 파일명에서 "N화" 패턴을 찾아 사용, 없으면 파일명에서 앞 번호만 제거해서 사용
- 플랫폼 폴더 이름과 개수는 자유입니다 (`docker-compose.yml`의 volumes에서 원하는 만큼 추가/제거)

## 로컬에서 돌려보기

1. 이 저장소를 clone
2. 저장소 루트에 `.env` 파일을 만들고 아래 값을 채웁니다 (예시):

   ```env
   HOST_PORT=8000
   LIBRARY_NAVER_PATH=/path/to/your/naver-webtoons
   LIBRARY_KAKAO_PATH=/path/to/your/kakao-webtoons
   PUBLIC_BASE_URL=
   RESCAN_INTERVAL_SECONDS=7200
   ```

3. 실행:

   ```
   docker compose up --build
   ```

4. `http://localhost:8000` (또는 지정한 `HOST_PORT`) 접속

`.env`는 절대 커밋하지 마세요 (`.gitignore`에 포함되어 있습니다). 개인 경로/도메인이 여기 들어갑니다.

## Portainer로 올리기 (포크/클론 불필요)

**직접 코드를 고칠 게 아니라면 fork나 clone 없이 이 저장소를 그대로 가리키기만 해도 됩니다.**
Portainer가 자신의 서버에서 이 저장소를 clone → build → 실행까지 전부 알아서 처리합니다.

1. Portainer → Stacks → Add stack → Repository 방식 선택
2. Repository URL에 이 저장소 주소를 그대로 입력
3. Portainer의 **Environment variables** 섹션에 아래 값을 직접 입력
   (저장소의 `docker-compose.yml`에는 개인 정보가 전혀 들어가지 않으므로, 실제 경로/도메인은
   반드시 Portainer 쪽에만 저장됩니다)
4. Deploy the stack
5. 코드 수정 후에는 Portainer에서 "Pull and redeploy"

## 자동 업데이트

**메인테이너가 아닌 일반 이용자** 입장에서는 이 저장소에 웹훅을 등록할 권한이 없으므로,
"push하면 자동 반영"은 표준 웹훅 방식으로는 안 됩니다. 대신 두 가지 방법이 있습니다.

**방법 A — 직접 build (기본 `docker-compose.yml`)**
가장 간단하지만 자동 업데이트는 안 되고, Portainer에서 수동으로 "Pull and redeploy"를 눌러야
새 버전이 반영됩니다.

**방법 B — GHCR 이미지 + Watchtower (`docker-compose.ghcr.yml`)**
이 저장소는 GitHub Actions로 push할 때마다 이미지를 빌드해서 GHCR(`ghcr.io/.../webtoon-server`)에
자동으로 올려둡니다. Portainer 스택 생성 시 Compose path를 `docker-compose.ghcr.yml`로 지정하면
로컬 빌드 없이 이 이미지를 그대로 pull해서 씁니다. 이 compose 파일에는 Watchtower가 같이 포함되어
있어서, 새 이미지가 올라올 때마다(기본 1시간마다 확인) 자동으로 pull 후 재시작합니다.
이미 다른 Watchtower를 쓰고 계시다면 이 파일에서 `watchtower` 서비스는 지우고 라벨만 남겨도 됩니다.

## 환경변수

| 변수 | 필수 | 설명 |
|---|---|---|
| `LIBRARY_NAVER_PATH` 등 | 예 | 호스트의 실제 웹툰 폴더 경로 (플랫폼마다 하나씩) |
| `HOST_PORT` | 아니오 (기본 8000) | 컨테이너를 노출할 호스트 포트 |
| `PUBLIC_BASE_URL` | 아니오 | 외부 알림 스크립트 등에서 바로가기 링크를 만들 때 쓰는 기준 주소. 비워두면 링크 생성을 생략 |
| `RESCAN_INTERVAL_SECONDS` | 아니오 (기본 7200) | 자동 재스캔 주기(초). 0 이하면 자동 재스캔 비활성화 |

수동 재스캔은 `POST /api/rescan` 또는 목록 화면 우측 상단 새로고침 버튼으로 가능합니다.

## 라이선스

MIT License — [LICENSE](./LICENSE) 참고. 이 라이선스는 이 저장소의 소스 코드에만 적용되며,
이 소프트웨어로 열어보는 콘텐츠의 저작권과는 무관합니다.
