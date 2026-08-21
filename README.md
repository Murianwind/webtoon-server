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

## 설치

이 저장소는 push할 때마다 GitHub Actions가 이미지를 빌드해서 GHCR에 공개로 올려둡니다.
**fork도 clone도 필요 없습니다.** 아래 내용을 그대로 복사해서, 표시된 줄만 실제 값으로
고친 뒤 Portainer의 **Web editor**에 붙여넣거나 `docker-compose.yml`로 저장해서 CLI에서
`docker compose up -d`로 실행하면 됩니다. 별도로 `.env`나 Portainer의 Environment
variables를 채울 필요가 없습니다 — 이 파일 자체가 곧 스택입니다.

```yaml
services:
  webtoon-server:
    image: ghcr.io/murianwind/webtoon-server:latest
    container_name: webtoon-server
    labels:
      - "com.centurylinklabs.watchtower.enable=true"
    ports:
      - "8000:8000"                                    # <-- 여기 수정: 왼쪽 숫자만 원하는 포트로
    environment:
      - "PUBLIC_BASE_URL="                              # <-- 여기 수정(선택): 외부 알림 링크가 필요할 때만 도메인 입력
      - "RESCAN_INTERVAL_SECONDS=7200"                  # <-- 여기 수정(선택): 자동 재스캔 주기(초)
    volumes:
      - "/path/to/your/naver-webtoons:/library/naver"   # <-- 여기 수정: 실제 웹툰 폴더 경로
      - "/path/to/your/kakao-webtoons:/library/kakao"   # <-- 여기 수정: 실제 웹툰 폴더 경로
      - "webtoon_data:/data"
    restart: unless-stopped

volumes:
  webtoon_data:
```

폴더가 naver/kakao 두 개가 아니라면, 붙여넣기 전에 아래 "웹툰 폴더 여러 개 추가하기"를
먼저 보고 `volumes`를 원하는 대로 고치세요.

배포 후 `http://localhost:8000` (또는 위에서 바꾼 포트) 접속.

### 자동 업데이트

위 `labels`에 있는 `com.centurylinklabs.watchtower.enable=true`는 Watchtower용 표시입니다.
**이미 Watchtower를 쓰고 계시면** `--label-enable` 옵션만 켜져 있으면 별다른 설정 없이
자동으로 인식되어, 새 이미지가 올라올 때마다 알아서 pull하고 재시작합니다. Watchtower가
없다면 이 라벨은 그냥 무시되니 지울 필요 없고, 필요할 때 수동으로
`docker compose pull && docker compose up -d` (또는 Portainer의 "Pull and redeploy")만
눌러주면 됩니다.

### 기존에 다른 방식으로 이미 배포해두셨다면

Repository 연결이나 로컬 빌드 방식으로 이미 스택을 만들어두셨다면, 그 스택을 지우고
위 내용으로 Web editor 방식의 새 스택을 만드는 걸 권장합니다. 컨테이너 이름이
동일(`webtoon-server`)해서 기존 컨테이너는 새로 만든 스택이 대체합니다. 예전에 쓰던
이미지는 더 이상 필요 없으니, Portainer의 Images 메뉴에서 나중에 한 번
정리(prune)해주면 깔끔합니다.

## 웹툰 폴더 여러 개 추가하기

`volumes`의 `/library/<이름>`으로 마운트되는 한 줄이 UI에서 태그로 보이는 "플랫폼 폴더"
하나입니다. 이름은 naver/kakao로 고정된 게 아니라 원하는 대로 자유롭게 지어도 됩니다.

**이미 하나의 폴더 아래에 플랫폼별 하위 폴더가 정리되어 있는 경우**

예를 들어 `D:\Webtoons\` 아래에 `naver\`, `kakao\`, `lezhin\` 등이 이미 나뉘어 있다면,
그 상위 폴더 하나만 통째로 마운트하면 됩니다. 이 경우 `/library` 바로 아래의 모든
하위 폴더가 자동으로 각각의 플랫폼으로 인식되고, 나중에 폴더를 새로 추가해도
compose를 건드릴 필요 없이 재스캔만 되면 자동 반영됩니다.

```yaml
    volumes:
      - "D:/Webtoons:/library"
      - "webtoon_data:/data"
```

**폴더들이 서로 다른 위치에 흩어져 있는 경우**

호스트의 실제 경로가 제각각이라면(예: 네이버는 D드라이브, 카카오는 다른 폴더),
원하는 만큼 줄을 자유롭게 추가하면 됩니다:

```yaml
    volumes:
      - "/path/to/naver-webtoons:/library/naver"
      - "/path/to/kakao-webtoons:/library/kakao"
      - "/path/to/lezhin-webtoons:/library/레진"       # 원하는 만큼 추가
      - "/path/to/other-webtoons:/library/무엇이든"     # 이름도 자유
      - "webtoon_data:/data"
```

## 환경변수 (compose 파일에서 직접 수정)

| 항목 | 위치 | 설명 |
|---|---|---|
| 웹툰 폴더 경로 | `volumes`의 왼쪽 경로 | 호스트의 실제 웹툰 폴더. 필요한 만큼 줄 추가 가능 |
| 포트 | `ports`의 왼쪽 숫자 | 컨테이너를 노출할 호스트 포트 (기본 8000) |
| `PUBLIC_BASE_URL` | `environment` | 외부 알림 스크립트 등에서 바로가기 링크를 만들 때 쓰는 기준 주소. 비워두면 링크 생성을 생략 |
| `RESCAN_INTERVAL_SECONDS` | `environment` | 자동 재스캔 주기(초), 기본 7200(2시간). 0 이하면 자동 재스캔 비활성화 |

수동 재스캔은 `POST /api/rescan` 또는 목록 화면 우측 상단 새로고침 버튼으로 가능합니다.

## 라이선스

MIT License — [LICENSE](./LICENSE) 참고. 이 라이선스는 이 저장소의 소스 코드에만 적용되며,
이 소프트웨어로 열어보는 콘텐츠의 저작권과는 무관합니다.
