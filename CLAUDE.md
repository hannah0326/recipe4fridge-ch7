# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

<!-- 작성: 2026-10-22 12:01 KST -->

## 개요

냉장고 사진에서 식재료를 인식하고 레시피를 추천하는 Streamlit 앱(OpenRouter 무료 모델 사용)이다. 단계별 PRD는 `docs/`에 있고, 현재 1단계(식재료 인식), 2단계(레시피 생성), 3단계(계정·프로필·레시피 저장)까지 구현되어 있다. Streamlit Community Cloud 무료 배포를 전제로 한다.

- `src/app.py`: Streamlit 화면(업로드·카메라, 진행 단계 표시, 재료별 한 줄 수정, 확정, 조건 선택과 레시피 카드). 확정 목록은 `st.session_state["confirmed"]`, 레시피는 `recipe_result`·`recipe_error`·`recipe_log`.
- `src/llm.py`: OpenRouter 호출의 공통 부분(`chat()`: 429·5xx 한 번 재시도, 200 본문의 `error` 처리, 모델 사용 불가 구분 / `run_with_fallback()`: 대체 모델 순서대로 시도 / 오류 클래스 / JSON 추출).
- `src/recipe.py`: 2단계 레시피 생성 순수 로직(프롬프트, 응답 검증, 보유 재료·알레르기·식단·시간·추가 재료·제목 재검사, 모자라면 한 번 더 요청).
- `src/rows.py`: 식재료 행 관리 순수 로직(행 id 부여, 삭제, 수정값 수집). 행마다 고유 id를 위젯 key로 쓴다. 순번을 key로 쓰면 가운데 행을 지울 때 마지막 행이 지워진 것처럼 보인다.
- `src/recognizer.py`: 화면과 무관한 1단계 인식 로직(이미지 검증·축소, 프롬프트, JSON 파싱). 호출은 `llm.py`에 맡기고, 오류 클래스는 `llm.py`의 것을 같은 이름으로 다시 내보낸다.
- `src/db.py`: Supabase(Postgres) 연결 풀. 오래 안 쓴 연결은 확인 쿼리로 죽었는지 보고, 죽었으면 풀을 새로 만들어 한 번 재시도한다. 오류 문구에서 접속 문자열·비밀번호를 가린다(`redact`). `StorageError`는 화면에 그대로 보여도 되는 문구만 담는다.
- `src/repository.py`: 사용자·로그인 세션·저장 레시피 저장소. 실제 `PostgresRepository`(스키마 `app`, RLS 켜고 정책 없음, anon·authenticated 권한 회수)와 테스트용 `MemoryRepository`가 같은 메서드를 가진다. 레시피 질의는 모두 `user_id`로 제한한다.
- `src/auth.py`: 이메일 검증, 비밀번호 규칙, argon2id 해시, 가입·로그인(없는 이메일도 해시 확인 시간을 써서 계정 존재가 드러나지 않음), 로그인 실패 제한(10분 5회), 구글 신원 처리, 로그인 유지 토큰(서버에는 SHA-256 해시만 저장).
- `src/services.py`: 저장소·제한기를 앱 전체에서 하나만 만든다. `DATABASE_URL`은 `st.secrets` → 환경변수 → `.env` 순으로 읽는다.
- `src/account_ui.py`: 사이드바 메뉴(냉장고 레시피·내 레시피·프로필·로그인), 로그인·가입·구글 로그인, 프로필, 내 레시피(검색·필터·메모·삭제), 저장 버튼, 계정 삭제. `src/ui_common.py`는 앱과 이 화면이 함께 쓰는 문구 처리다.
- `src/test_openrouter.py`: API 연결을 확인하는 단발 스크립트.
- `tests/`: 단위 테스트. `docs/`: PRD(`PRD_step1.md`~`PRD_step3.md`).
- 루트: `.env`(키, git 제외), `.env.example`, `.streamlit/secrets.toml.example`, `requirements.txt`

## 명령

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt   # 최초 1회
.venv/Scripts/python -m streamlit run src/app.py                                   # 앱 실행
.venv/Scripts/python -m pytest tests -q                                            # 전체 테스트
.venv/Scripts/python -m pytest tests/test_recognizer.py -k parse -q                # 일부 테스트
PYTHONIOENCODING=utf-8 .venv/Scripts/python src/test_openrouter.py                 # API 연결 확인
```

- Windows 콘솔에서 한글이 깨지지 않도록 스크립트 실행 시 `PYTHONIOENCODING=utf-8`을 붙인다.
- 테스트는 가짜 응답으로 동작하므로 네트워크·키가 필요 없다.

## 구조와 주의점

- 모델: 이미지 기본 `nvidia/nemotron-3.5-super-vl:free`(`recognizer.VISION_MODEL`), 대체 `dots-studio/dots-3-note-preview:free`(`FALLBACK_MODELS`). 텍스트 기본 `nvidia/nemotron-3-ultra-550b-a55b:free`(안정성 우선), 대체 `nemotron-3.5-lightning`(빠르지만 형식 오류·개수 부족이 잦음) → `nemotron-3.5-super-vl` → `poolside/laguna-s-2.1`(`recipe.TEXT_MODEL`·`recipe.FALLBACK_MODELS`).
- `minimax/minimax-m3.1-flash:free`는 2026-10-22에 무료 기간이 끝나 404를 돌려준다(처음에 가장 빨라 기본 모델로 정했었다). 그래서 이미지 기본을 `nemotron-3.5-super-vl`로 바꿨다. 유료 모델(`minimax/minimax-m3.1-flash`)은 요금이 나가므로 쓰지 않는다. 앱은 404를 "사용 불가"로 보고 재시도 없이 대체 모델로 넘어간다. 무료 모델은 이처럼 하루 만에도 사라질 수 있으므로 모델 이름이 404를 내면 `/api/v1/models`로 다시 조사한다.
- 이미지 모델도 추론을 끄고 부른다(`recognizer.EXTRA`). 켜 두면 물건이 많은 냉장고 사진에서 추론이 `max_tokens` 2000을 다 써서 내용 없이 잘렸다(2026-11-12). 끄고 같은 사진을 부르니 10초, 재료 20개로 정상이었다. 단순 도형 이미지에는 `{}`만 답해 형식 오류로 처리된다.
- 텍스트 모델은 추론을 끄고(`reasoning: {"enabled": false}`) 부른다. 켜면 66초 이상 걸리고 토큰을 추론에 다 써서 응답이 잘린다. 추론을 끌 수 없는 모델(`recursive/flint`)은 400이 나므로 쓰지 않는다. 모델이 준 보유 재료·알레르기 준수·시간은 믿지 않고 `recipe.reject_reason()`이 다시 확인한다. 화면에 쓰는 모델 글자는 `md_safe()`로 이스케이프한다.
- 레시피 조건: 요리 종류(한식·양식·중식·일식), 난이도(초급·중급·고급 = easy·medium·hard), 조리 시간(제한 없음·15분 이내·30분 이내·30분 이상), 인원수, 알레르기, 식단 제한, 추가 재료 정책, 개수. 조리 시간은 `app.py`의 `TIME_CHOICES`가 (이름, 최소 분, 최대 분)으로 풀어 `recipe`에 `min_minutes`·`max_minutes`로 넘긴다("이상"=최소, "이내"=최대, 둘을 함께 주면 최대 우선). 요리 종류·난이도·시간은 모델 답을 믿지 않고 `recipe.reject_reason()`이 다시 확인한다. 모델이 `cuisine`을 밝히지 않으면 확인할 수 없어 통과시킨다.
- 2단계는 생성이 끝나면 `st.rerun()`으로 화면을 다시 그린다(단추 이름과 결과를 함께 갱신). 재료를 다시 인식·확정하면 `clear_recipes()`로 레시피를 지운다.
- **실패는 숨기지 않고 정직하게 화면에 표시한다(이미지 인식·레시피 모두).**
  - 새로 시도하면 이전 결과(`rows`·`confirmed`·`run_info`·레시피)를 먼저 지운다. 실패한 뒤에 이전 성공 결과가 이번 결과처럼 남아 보이면 안 된다.
  - 실패 메시지는 `recognize_error`·`recipe_error`에 담아 **다음 시도를 누를 때까지** 화면에 남긴다. 그 순간에만 `st.error`로 보여 주면 다른 위젯을 조작해 화면이 다시 그려질 때 사라진다.
  - "요청이 너무 잦습니다"(세션 제한)도 같은 실패 표시로 남긴다.
  - 예상하지 못한 예외도 `except Exception`으로 받아 "예상하지 못한 오류가 발생했어요. (오류 종류: …)"로 알리고, 오류 내용(경로·키 등)은 화면에 내지 않고 서버 로그(`fridge_app` 로거)에만 남긴다. Streamlit의 제어 예외(`st.rerun`)는 `BaseException`이라 이 처리에 걸리지 않는다.
  - 진행 상자는 실패하면 "인식 실패"·"레시피 생성 실패"(error 상태)로 바뀌고 진행 기록에 `❌ 원인`이 남는다.
- API 키 조회 순서: 앱은 `st.secrets` → 환경변수 → 프로젝트 루트 `.env`(`recognizer.get_api_key()`). 키 값은 화면·로그·오류 문구에 출력하지 않는다. `.env`와 `.streamlit/secrets.toml`은 git에서 제외된다.
- 업로드 이미지는 검증 후 긴 변 1024px·약 300KB 이하 JPEG로 줄여(`prepare_image`) base64 data URL로 보낸다. 이미지는 저장하지 않는다. 업로드 한도 10MB는 `.streamlit/config.toml`의 `maxUploadSize`가 담당한다.
- `max_tokens`는 2000이다. Nemotron VL 같은 추론형 모델은 답하기 전에 추론에 토큰을 많이 쓰므로, 300처럼 작게 잡으면 응답이 중간에 끊긴다.
- **실패 원인별 안내(`llm.py`):** 하루 무료 한도 소진은 `DailyLimitError`(429 본문의 `limit_source == "openrouter_free_tier_daily"` 또는 메시지 `free-models-per-day`로 판별, 한도 횟수·초기화 시각은 `X-RateLimit-Limit`·`X-RateLimit-Reset`에서 읽어 한국 시각으로 안내)로 올리고 재시도도 대체 모델도 시도하지 않는다. 모든 모델이 사용 불가면 `AllModelsUnavailableError`("AI 모델을 모두 사용할 수 없어요… 관리자에게"), 모두 형식 오류면 `ParseError`("올바른 형식으로 답하지 못했어요"), 그 밖에는 `RateLimitedError`("요청이 몰려 있습니다")다. 분당 한도는 아직 구분하지 않는다.
- **진행 기록:** 요청마다 `response` 이벤트(상태, 걸린 초)와 마지막 `⏱ 총 N초`가 남고, 형식 오류 재요청에는 원인(`cause`)과 응답 앞부분(`snippet`, 60자)이 붙는다. 원인은 빈 응답·`choices` 없음·응답 잘림(`finish_reason == "length"`)·JSON 없음·잘못된 JSON으로 구분된다.
- 모델이 형식에 어긋나게 답하면 같은 모델에 1회 다시 요청한다. 단, 응답이 잘렸으면(`llm.TruncatedError`, `finish_reason == "length"`) 다시 물어도 똑같으므로 재요청하지 않는다. 잘림은 내용이 비어 있어도 '빈 응답'이 아니라 '잘림'으로 안내한다. 429·5xx는 2초 뒤 한 번만 재시도하고, 그래도 실패하거나 모델이 없거나(403·404·410) 형식 오류·잘림이면 `recognizer.FALLBACK_MODELS`를 차례로 시도한다. 401(키 문제)은 대체하지 않는다. 진입점은 `recognize_detailed()`이며 사용한 모델과 용량 변화를 함께 돌려준다. `tests/`가 검증한다.
- 이미지 모델은 같은 이미지로 직접 시험한 응답 시간과 사용자가 실제 냉장고 사진으로 확인한 정확도로 정했다(2026-10-22, 모델당 1회: minimax 7초(종료), nemotron-3.5-super-vl 19초, dots 24초). 모델 가용성이 바뀌면 `/api/v1/models`에서 `:free` + 이미지 입력 모델을 다시 조사해 갱신한다. 제외한 모델: `nemotron-3-nano-omni`(175초), `thinkingmachines/inkling*`(agentic 전용이라 403), `google/gemma-4-*`(공유 풀 429). `dots`는 추론에 토큰을 많이 써서 `max_tokens`가 작으면 답이 잘려 파싱에 실패할 수 있다.
- 식재료가 아닌 단순 도형 이미지는 모델이 빈 목록을 돌려준다. 정상 동작이며 앱은 "찾지 못함" 안내를 보여 준다.
- Streamlit은 `import`한 모듈을 파일이 바뀌어도 다시 읽지 않는다. `llm.py`·`recipe.py`·`recognizer.py`·`rows.py`를 고친 뒤에는 실행 중인 서버를 재시작해야 반영된다(배포 앱은 Reboot app).
- 식재료 목록은 `st.data_editor`가 아니라 행마다 `st.columns`(경고·이름·수량·삭제)로 그린다. 좁은 화면에서도 한 줄이 유지되도록 `app.py`의 `ONE_LINE_CSS`가 `flex-wrap: nowrap`을 강제한다. Streamlit 버전이 바뀌어 `data-testid`가 달라지면 이 CSS를 다시 확인한다.
- `st.camera_input`은 만들어 두기만 해도 페이지를 열 때 카메라 권한을 요청하므로 토글을 켰을 때만 만든다.
- **3단계 계정·저장:** 아이디는 이메일이고 비밀번호 로그인(`provider=password`)과 구글 로그인(`provider=google`)을 함께 둔다. 같은 이메일이어도 두 방식 계정을 합치지 않는다(남의 이메일을 먼저 가입해 두는 선점 공격 방지). 충돌하면 어느 방식으로 가입했는지 안내하고, 구글 쪽은 `st.logout()`으로 끊는다. 구글 설정(`[auth]`)이 비어 있으면 구글 단추를 숨기고 안내만 보인다.
- 로그인 유지는 `fridge_session` 쿠키(30일)다. 쿠키는 `st.html` 스크립트로 심고(`st.context.cookies`로 읽음) 사이드바에서 토큰이 있는 동안 매번 다시 심는다. 로그아웃은 DB 세션·쿠키·화면 상태(재료·레시피·조건)를 모두 지운다.
- DB 접속은 Supabase **Session pooler(포트 5432)**를 쓴다(Transaction pooler 6543은 쓰지 않음). 비밀번호에 `*`·`!`·`@` 같은 문자가 있으면 URL 인코딩한다. 개발 중에도 SQLite로 대체하지 않는다. DB가 없거나 끊겨도 1·2단계는 동작하고 계정 기능에만 안내가 나온다.
- 테스트는 `tests/conftest.py`가 모든 테스트의 저장소를 메모리로 바꿔 끼운다(실수로 실제 DB에 닿지 않게). 실제 Postgres 시험은 `RUN_LIVE_DB=1`일 때만 임시 스키마를 만들어 돌리고 끝나면 지운다: `RUN_LIVE_DB=1 .venv/Scripts/python -m pytest tests/test_repository.py -q`.
- 위젯이 그려지지 않는 메뉴로 가면 그 위젯의 `session_state` 값이 사라진다. 그래서 식재료 행은 값을 `rows`에 되써 두고 메뉴를 오가도 유지한다.
- **배포(Streamlit Community Cloud):** 저장소 `hannah0326/Study_04`, 브랜치 `main`, 파일 `src/app.py`, Python 3.12. 주소는 `https://study04-bbwgqssengm8wappf5ylrt.streamlit.app`이다. Secrets는 최상위 `OPENROUTER_API_KEY`·`DATABASE_URL`을 먼저 쓰고 `[auth]`를 맨 뒤에 둔다. 값은 모두 큰따옴표로 감싼다(따옴표가 빠지면 "Invalid format: please enter valid TOML"이 뜬다). `[auth]`의 `redirect_uri`는 배포 주소의 `/oauth2callback`이고, 구글 콘솔의 승인된 리디렉션 URI에 로컬 주소와 함께 등록해야 한다(빠지면 `redirect_uri_mismatch`). 반영에 몇 분 걸린다.
- 구글 로그인에는 `Authlib`만으로 부족하고 `httpx`가 필요하다(없으면 `/auth/login`이 Internal Server Error). `requirements.txt`에 들어 있다.
- 요청 제한은 IP가 아니라 세션 기준이다(Streamlit에서 IP를 안정적으로 얻기 어려움).
- 무료 배포 서버의 디스크는 영구 저장소가 아니다. 그래서 3단계 저장은 외부 DB(Supabase)를 쓴다.

## 무료 모델 관련 알려진 동작

- `google/gemma-4-26b-a4b-it:free`, `google/gemma-4-31b-it:free`는 Google AI Studio 공유 풀 한도 때문에 반복적으로 HTTP 429가 났다. 키나 코드 문제가 아니다.
- `nvidia/nemotron-3-super-120b-a12b:free`는 Nvidia 쪽에서 404(함수 없음)가 났다.
- 429·404는 제공사 오류이고 인증 오류(401)와 구분된다.
- 429가 계정의 하루 무료 한도(50회) 때문이면 어떤 `:free` 모델로 바꿔도 같은 오류가 나고, 한국 시각 오전 9시(UTC 자정)에 초기화된다. 실제 호출 점검과 모델 비교 시험이 한도를 빨리 쓰므로 필요할 때만 한다.

## 저장소 관례

- 새 파일에는 KST(UTC+9) 기준 작성 일시를 주석으로 표시한다.
- 저장소에 올라가는 텍스트(주석, 커밋 메시지, 문서)는 한국어로 쓴다.
- `.gitignore`는 `.env`, `.env.*`(`.env.example` 제외), `.streamlit/secrets.toml`, `.venv/`, 캐시, macOS의 `.DS_Store`를 제외한다.

## 실행 시 확인 사항

앱은 OpenRouter API로 실제 AI 모델을 불러 냉장고 사진의 식재료를 인식하고(1단계) 레시피를 생성한다(2단계). 실제 모델을 실행했으면 **매번** API가 정확하게 작동했는지, AI 모델이 문제 없이 실행되었는지를 점검하고, 문제가 있었다면 무엇이었는지 보고한다.

- **API 동작**: 응답한 모델(기본/대체), 시도별 결과(200·429·404·형식 오류), 재시도와 대체 모델 전환 여부, 소요 시간
- **결과 품질**: JSON 해석 성공 여부, 재료명 깨짐, 정답을 아는 사진이면 인식률, 레시피의 알레르기·식단·시간·난이도·요리 종류·추가 재료 조건 준수
- **실패했을 때**: 원인(예: 하루 한도 소진, 모델 서비스 종료, 형식 오류)과 사용자에게 보인 안내 문구. 안내가 원인과 맞는지도 확인한다.

점검 방법:

- 앱 화면의 "진행 기록"(이미지 인식·레시피 각각)에서 모델, 상태, 전환 여부를 확인하고 시간은 직접 잰다.
- 무료 한도(계정당 하루 50회, 한국 시각 오전 9시 초기화)를 지키기 위해 실제 호출은 점검이 필요할 때만 하고, 코드 검증은 가짜 응답 테스트(`pytest`)로 한다. 429 응답에 `X-RateLimit-Remaining: 0`이 있으면 모델 문제가 아니라 하루 한도 소진이다.