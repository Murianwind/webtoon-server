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
**fork도 clone도 필요 없이, [`docker-compose.yml`](./docker-compose.yml) 하나만 있으면 됩니다.**
Portainer가 있든 없든, CLI로 직접 `docker compose`를 쓰는 환경이든 동일하게 씁니다.

1. `docker-compose.yml` 파일을 받아옵니다 (다운로드하거나 저장소를 clone)
2. 필요한 값을 채웁니다. 최소한 이 두 개는 필요합니다:
   - `LIBRARY_NAVER_PATH` — 실제 웹툰 폴더 경로 (예: `D:/Webtoons/naver`)
   - `LIBRARY_KAKAO_PATH` — 실제 웹툰 폴더 경로
   - 폴더가 naver/kakao 두 개가 아니라면 아래 "웹툰 폴더 여러 개 추가하기"를 먼저 보고
     `docker-compose.yml`의 volumes를 원하는 대로 고친 뒤 진행하세요.
   - CLI라면 같은 폴더에 `.env` 파일을 만들어 값을 채우고, Portainer라면 스택의
     **Environment variables**에 같은 값을 입력하면 됩니다.
3. 실행합니다:
   - **CLI**: `docker-compose.yml`이 있는 폴더에서 `docker compose up -d`
   - **Portainer**: Stacks → Add stack → Web editor에 파일 내용을 그대로 붙여넣거나,
     Repository 방식으로 이 저장소를 그대로 지정 → Deploy the stack
4. `http://localhost:8000` (또는 지정한 `HOST_PORT`) 접속

CLI로 `.env`를 쓰신다면 절대 커밋하지 마세요 (`.gitignore`에 포함되어 있습니다). 개인
경로/도메인이 여기 들어갑니다.

### 자동 업데이트

`docker-compose.yml`의 `webtoon-server` 서비스에 이미
`com.centurylinklabs.watchtower.enable=true` 라벨이 붙어 있습니다. **이미 Watchtower를
쓰고 계시면** `--label-enable` 옵션만 켜져 있으면 별다른 설정 없이 자동으로 인식되어,
새 이미지가 올라올 때마다 알아서 pull하고 재시작합니다. Watchtower가 없다면 이 라벨은
그냥 무시되니 지울 필요 없고, 필요할 때 수동으로 `docker compose pull && docker compose up -d`
(또는 Portainer의 "Pull and redeploy")만 눌러주면 됩니다.

### 기존에 build 방식으로 이미 배포해두셨다면

Repository 방식으로 이 저장소를 이미 연결해두셨다면, 저장소를 다시 pull해서
`docker-compose.yml`을 갱신한 뒤 Portainer에서 **Pull and redeploy**만 누르면 됩니다.
컨테이너 이름이 동일해서 자동으로 새 정의(이미지 기반)로 교체됩니다. 예전에 로컬에서
빌드됐던 이미지는 더 이상 안 쓰이니, Portainer의 Images 메뉴에서 나중에 한 번
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
      - "${LIBRARY_ROOT_PATH}:/library"
      - "webtoon_data:/data"
```

**폴더들이 서로 다른 위치에 흩어져 있는 경우**

호스트의 실제 경로가 제각각이라면(예: 네이버는 D드라이브, 카카오는 다른 폴더),
원하는 만큼 줄을 자유롭게 추가하면 됩니다:

```yaml
    volumes:
      - "${LIBRARY_NAVER_PATH}:/library/naver"
      - "${LIBRARY_KAKAO_PATH}:/library/kakao"
      - "${LIBRARY_LEZHIN_PATH}:/library/레진"       # 원하는 만큼 추가
      - "${LIBRARY_MYSTUFF_PATH}:/library/무엇이든"   # 이름도 자유
      - "webtoon_data:/data"
```

이렇게 줄을 추가했다면, Portainer의 **Environment variables**에도 그 변수명
(`LIBRARY_LEZHIN_PATH` 등)을 새로 추가해서 실제 경로를 넣어주면 됩니다.

## 환경변수

| 변수 | 필수 | 설명 |
|---|---|---|
| `LIBRARY_NAVER_PATH` 등 | 예 | 호스트의 실제 웹툰 폴더 경로. volumes에 적어둔 만큼 필요 |
| `HOST_PORT` | 아니오 (기본 8000) | 컨테이너를 노출할 호스트 포트 |
| `PUBLIC_BASE_URL` | 아니오 | 외부 알림 스크립트 등에서 바로가기 링크를 만들 때 쓰는 기준 주소. 비워두면 링크 생성을 생략 |
| `RESCAN_INTERVAL_SECONDS` | 아니오 (기본 7200) | 자동 재스캔 주기(초). 0 이하면 자동 재스캔 비활성화 |

수동 재스캔은 `POST /api/rescan` 또는 목록 화면 우측 상단 새로고침 버튼으로 가능합니다.

## 라이선스

MIT License — [LICENSE](./LICENSE) 참고. 이 라이선스는 이 저장소의 소스 코드에만 적용되며,
이 소프트웨어로 열어보는 콘텐츠의 저작권과는 무관합니다.
