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
컨테이너 안 /library
 ├ naver/                 ← docker-compose.yml의 volumes에서 정한 이름 = 화면에 표시되는 태그
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
      - "8000:8000"                                     # <-- 여기 수정: 왼쪽 숫자만 원하는 포트로
    environment:
      - "PUBLIC_BASE_URL="                               # <-- 여기 수정(선택): 외부 알림 링크가 필요할 때만 도메인 입력
      - "RESCAN_INTERVAL_SECONDS=7200"                   # <-- 여기 수정(선택): 자동 재스캔 주기(초)
    volumes:
      - "/path/to/your/naver-webtoons:/library/naver"    # <-- 여기 수정: 실제 웹툰 폴더 경로, 오른쪽 이름이 태그
      - "/path/to/your/kakao-webtoons:/library/kakao"    # <-- 여기 수정: 실제 웹툰 폴더 경로, 오른쪽 이름이 태그
      - "webtoon_data:/data"
    restart: unless-stopped

volumes:
  webtoon_data:
```

**`volumes` 한 줄 = 웹툰 사이트 하나.** 콜론(`:`) **왼쪽**은 내 컴퓨터에 실제로 있는
웹툰 폴더 경로, **오른쪽**(`/library/` 뒤)은 화면에 그대로 표시되는 태그 이름입니다.
오른쪽 이름은 완전히 자유롭게 지어도 됩니다.

예를 들어 카카오웹툰을 `D:/Downloads/Webtoon/카카오 웹툰` 폴더에 받아두셨다면:

```
- "D:/Downloads/Webtoon/카카오 웹툰:/library/카카오"
```

이렇게 적으면 화면에 "카카오"라는 태그로 표시됩니다.

사이트가 두 개보다 많거나 적으면, `volumes`에 줄을 자유롭게 추가/삭제하면 됩니다:

```yaml
    volumes:
      - "/path/to/naver-webtoons:/library/naver"
      - "/path/to/kakao-webtoons:/library/카카오"
      - "/path/to/lezhin-webtoons:/library/레진"       # 원하는 만큼 추가
      - "webtoon_data:/data"
```

배포 후 `http://localhost:8000` (또는 위에서 바꾼 포트) 접속.

### 자동 업데이트

위 `labels`에 있는 `com.centurylinklabs.watchtower.enable=true`는 Watchtower용 표시입니다.
**이미 Watchtower를 쓰고 계시면** `--label-enable` 옵션만 켜져 있으면 별다른 설정 없이
자동으로 인식되어, 새 이미지가 올라올 때마다 알아서 pull하고 재시작합니다. Watchtower가
없다면 이 라벨은 그냥 무시되니 지울 필요 없고, 필요할 때 수동으로
`docker compose pull && docker compose up -d` (또는 Portainer의 "Pull and redeploy")만
눌러주면 됩니다.

라이브러리 재스캔은 기본 2시간마다 자동으로 돌고, 바로 반영하고 싶으면 목록 화면
우측 상단 새로고침 버튼(또는 `POST /api/rescan`)을 누르면 됩니다.

## 라이선스

MIT License — [LICENSE](./LICENSE) 참고. 이 라이선스는 이 저장소의 소스 코드에만 적용되며,
이 소프트웨어로 열어보는 콘텐츠의 저작권과는 무관합니다.
