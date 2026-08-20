# webtoon-server

Komga를 대체하기 위한 개인용 웹툰 뷰어. zip으로 저장된 웹툰 회차를 폴더 기반으로 스캔해서
세로 스크롤로 읽을 수 있는 웹 서버.

## 지금 단계 (MVP)

- 라이브러리 폴더 스캔 (플랫폼 폴더 → 시리즈 폴더 → 회차 zip)
- 시리즈 목록 그리드 UI
- 회차 목록 → 세로 스크롤 리더
- 읽음 진행률 저장, 디스코드 연동은 다음 단계에서 추가 예정

## 폴더 구조 규칙

```
LIBRARY_ROOT (컨테이너 내부 /library)
 ├ naver/                 ← 플랫폼 폴더 (이름은 자유, UI에 태그로 표시됨)
 │   └ 마법사랑해/          ← 시리즈 폴더 = 웹툰 한 편
 │       ├ 103 ... 100화 - ...zip
 │       └ 104 ... 번외편 ...zip
 └ kakao/
     └ 나 혼자만 레벨업/
         ├ 0003_프롤로그#48.zip
         └ 0004_1화#64.zip
```

- 회차 정렬은 zip 파일명 맨 앞 숫자 기준
- 표시 라벨은 파일명에서 "N화" 패턴을 찾아 사용, 없으면 파일명에서 앞 번호만 제거해서 사용

## 로컬에서 돌려보기

`docker-compose.yml`의 volumes 왼쪽 경로를 실제 웹툰 폴더로 바꾼 뒤:

```
docker compose up --build
```

`http://localhost:25601` 접속.

폴더를 새로 추가/삭제한 경우 재스캔은 컨테이너 재시작으로 반영됩니다
(재스캔 API: `POST /api/rescan`).

## Portainer로 올리기

1. 이 저장소를 GitHub에 push
2. Portainer → Stacks → Add stack → Repository 방식으로 이 저장소 연결
3. `docker-compose.yml`의 volumes 경로를 실제 PC 경로로 맞춰서 배포
4. 코드 수정 후에는 Portainer에서 "Pull and redeploy"

## 다음 단계

1. 읽음 진행률 저장/기기 간 동기화
2. 디스코드 바로가기 URL을 이 서버 주소로 전환 (`webtoon_manager.py` / `webtoon_checker.py`)
3. 프리페치, 썸네일 캐싱 등 폴리싱
