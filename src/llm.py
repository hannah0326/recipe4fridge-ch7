# 작성: 2026-10-22 22:30 KST
# OpenRouter 호출의 공통 부분. 이미지 인식(recognizer.py)과 레시피 생성(recipe.py)이 함께 쓴다.
# 429·5xx 한 번 재시도, 모델이 없거나 쓸 수 없을 때의 구분, 대체 모델 순서대로 시도하기를 담당한다.
# 실패 원인(하루 한도 소진, 모든 모델 종료, 형식 오류)을 구분해서 사용자에게 맞는 안내를 준다.
import datetime as dt
import json
import re
import time

import requests

URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_SEC = 120
# 429·5xx 는 한 번만 더 시도하고, 그래도 안 되면 다음 모델로 넘어간다.
RETRY_DELAYS = (2,)
KST = dt.timezone(dt.timedelta(hours=9))

MSG_BUSY = "지금 요청이 몰려 있습니다. 잠시 후 다시 시도해 주세요."
# 5xx 는 요청이 몰린 것이 아니라 AI 제공사 서버 쪽 문제다. 원인에 맞게 따로 안내한다.
MSG_SERVER = "AI 서비스 서버에 일시적인 문제가 있어요. 잠시 후 다시 시도해 주세요."
MSG_ALL_UNAVAILABLE = (
    "지금 이 서비스가 쓰는 AI 모델을 모두 사용할 수 없어요. 모델 서비스가 종료되었을 수 있습니다. "
    "시간이 지나도 같은 결과일 수 있으니 관리자에게 알려 주세요."
)
MSG_ALL_PARSE = "AI가 올바른 형식으로 답하지 못했어요. 잠시 후 다시 시도해 주세요."


class ServiceError(Exception):
    """사용자에게 그대로 보여 줘도 되는 메시지를 담는 오류."""


class ParseError(ServiceError):
    """모델 응답을 해석하지 못했다. snippet 은 응답 앞부분(원인 파악용)이다."""

    def __init__(self, message, snippet=""):
        super().__init__(message)
        self.snippet = snippet


class TruncatedError(ParseError):
    """응답이 max_tokens 에서 잘렸다. 같은 모델에 다시 물어도 똑같이 잘리므로 재요청하지 않는다."""


class RateLimitedError(ServiceError):
    """요청이 몰려 429 등이 계속된 경우. 잠시 뒤에는 풀릴 수 있다."""


class ServerError(RateLimitedError):
    """제공사 서버 오류(5xx)가 계속된 경우. 요청 제한과 같이 다시 시도·대체 모델로 넘어가되 안내 문구만 다르다."""


def format_reset(reset_ms):
    """OpenRouter 의 초기화 시각(밀리초 에포크)을 '한국 시각 10월 25일 오전 9시'로 바꾼다. 못 읽으면 일반 문구."""
    try:
        when = dt.datetime.fromtimestamp(int(reset_ms) / 1000, tz=KST)
    except (TypeError, ValueError, OverflowError, OSError):
        return "한국 시각 오전 9시"
    ampm = "오전" if when.hour < 12 else "오후"
    text = f"{when.month}월 {when.day}일 {ampm} {when.hour % 12 or 12}시"
    if when.minute:
        text += f" {when.minute}분"
    return f"한국 시각 {text}"


class DailyLimitError(RateLimitedError):
    """계정의 하루 무료 호출 한도를 모두 썼다. 다시 시도하거나 다른 무료 모델로 바꿔도 같은 오류가 난다."""

    def __init__(self, limit=None, reset_ms=None):
        self.limit, self.reset_ms = limit, reset_ms
        used = f"{limit}회를 " if limit else "횟수를 "
        super().__init__(
            f"오늘 쓸 수 있는 무료 호출 {used}모두 사용했어요. {format_reset(reset_ms)}에 초기화되며, "
            "그때까지는 다시 시도해도 같은 오류가 납니다."
        )


class ModelUnavailableError(ServiceError):
    """모델이 없거나(404), 이 키로 쓸 수 없거나(403·410), 이 요청을 받지 못하는(400) 경우. 다음 모델로 넘어갈 수 있다."""


class AllModelsUnavailableError(ServiceError):
    """시도한 모델이 모두 사용할 수 없는 상태(서비스 종료 등)였다. 기다려도 풀리지 않을 수 있다."""


def notify(on_event, kind, **info):
    """진행 상황을 화면 등에 알린다. on_event 가 없으면 아무 일도 하지 않는다."""
    if on_event:
        on_event({"kind": kind, **info})


def snippet(text, limit=60):
    """형식 오류의 원인 파악용으로 응답 앞부분만 한 줄로 줄여 둔다."""
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


_snippet = snippet


def extract_json(text):
    """응답 텍스트에서 JSON 객체를 꺼낸다. 코드 블록 표시와 앞뒤 설명 문장은 허용한다."""
    cleaned = re.sub(r"```(?:json)?", "", text or "")
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ParseError("모델 응답에서 JSON 을 찾지 못했습니다.", _snippet(text))
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        raise ParseError("모델 응답이 올바른 JSON 이 아닙니다.", _snippet(text))


def _status_of(response):
    """HTTP 상태를 돌려준다. OpenRouter 는 200 본문에 error 객체를 담아 보내기도 하므로 그 코드를 상태로 본다."""
    status = response.status_code
    if status != 200:
        return status, None
    try:
        body = response.json()
    except ValueError:
        return status, None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        code = body["error"].get("code")
        return (code if isinstance(code, int) else 502), body
    return status, body


def _daily_limit(response, body):
    """429 가 계정의 하루 무료 한도 때문이면 (한도, 초기화 시각)을, 아니면 None 을 돌려준다.

    분당 한도나 제공사 혼잡도 429 라서 상태 코드만으로는 구분할 수 없다. 응답 본문의 limit_source 와 메시지로 구분한다.
    """
    if body is None:
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - 본문이 JSON 이 아니어도 하루 한도 판별만 건너뛰면 된다
            body = None
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    meta = error.get("metadata") if isinstance(error.get("metadata"), dict) else {}
    if meta.get("limit_source") != "openrouter_free_tier_daily" and "free-models-per-day" not in str(error.get("message") or ""):
        return None
    headers = {}
    for source in (meta.get("headers"), getattr(response, "headers", None)):
        if source:
            headers.update({str(k).lower(): v for k, v in dict(source).items()})
    return headers.get("x-ratelimit-limit"), headers.get("x-ratelimit-reset")


def chat(model, messages, api_key, max_tokens, post=requests.post, sleep=time.sleep, on_event=None,
         timeout=TIMEOUT_SEC, extra=None):
    """모델 하나를 호출하고 응답 텍스트를 돌려준다. 429·5xx 는 RETRY_DELAYS 만큼만 재시도한다.

    extra 는 요청 본문에 더할 항목(예: {"reasoning": {"enabled": False}})이다.
    요청마다 request·response(상태와 걸린 시간) 이벤트를 알린다. 하루 한도 소진이면 재시도도 대체 모델도 쓰지 않는다.
    """
    body = {"model": model, "max_tokens": max_tokens, "messages": messages, **(extra or {})}
    headers = {"Authorization": f"Bearer {api_key}"}
    for attempt in range(len(RETRY_DELAYS) + 1):
        notify(on_event, "request", model=model, attempt=attempt + 1)
        started = time.time()
        try:
            r = post(URL, headers=headers, json=body, timeout=timeout)
        except requests.Timeout:
            raise ServiceError("응답이 너무 오래 걸립니다. 잠시 후 다시 시도해 주세요.")
        except requests.RequestException:
            raise ServiceError("서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.")
        status, data = _status_of(r)
        notify(on_event, "response", model=model, status=status, seconds=round(time.time() - started, 1))
        if status == 200:
            try:
                choice = (data if data is not None else r.json())["choices"][0]
                content = choice["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError):
                raise ParseError("모델 응답에 choices 가 없습니다.")
            # 잘림을 먼저 본다. 추론이 토큰을 다 쓰면 내용이 비어 있어도 원인은 '빈 응답'이 아니라 '잘림'이다.
            if isinstance(choice, dict) and choice.get("finish_reason") == "length":
                raise TruncatedError("응답이 최대 길이에서 잘렸습니다.", _snippet(content or ""))
            if not content:
                raise ParseError("모델이 빈 응답을 보냈습니다.")
            return content
        if status == 429:
            daily = _daily_limit(r, data)
            if daily:
                notify(on_event, "daily_limit", model=model, reset=format_reset(daily[1]))
                raise DailyLimitError(daily[0], daily[1])
        if status == 429 or status >= 500:
            if attempt < len(RETRY_DELAYS):
                notify(on_event, "retry", model=model, status=status, wait=RETRY_DELAYS[attempt])
                sleep(RETRY_DELAYS[attempt])
                continue
            raise ServerError(MSG_SERVER) if status >= 500 else RateLimitedError(MSG_BUSY)
        if status == 401:
            # 키 설정 문제는 사용자에게 원인을 알리지 않는다. 같은 키라 다른 모델로 바꿔도 소용없다.
            # 키 값은 어디에도 남기지 않는다.
            raise ServiceError("서비스 설정에 문제가 있습니다. 관리자에게 문의해 주세요.")
        if status in (400, 403, 404, 410):
            raise ModelUnavailableError("선택한 모델을 지금 사용할 수 없습니다.")
        raise ServiceError(f"요청에 실패했습니다. (HTTP {status})")


def run_with_fallback(models, attempt, on_event=None, fall_on=(RateLimitedError, ModelUnavailableError)):
    """models 를 차례로 attempt(model)로 시도한다. fall_on 오류면 다음 모델로 넘어가고 fallback 을 알린다.

    성공하면 (사용한 모델, attempt 의 결과)를 돌려준다. 하루 한도 소진(DailyLimitError)과 401 같은 그 밖의 오류는
    모델을 바꿔도 소용없으므로 그대로 올린다. 모두 실패하면 원인에 맞는 오류를 낸다.
    모두 사용 불가면 AllModelsUnavailableError, 모두 형식 오류면 ParseError, 요청 제한(429) 없이 서버 오류(5xx)가 있었으면
    ServerError, 그 밖에는 RateLimitedError 다.
    """
    errors = []
    for i, model in enumerate(models):
        try:
            return model, attempt(model)
        except DailyLimitError:
            raise
        except fall_on as e:
            errors.append(e)
            if isinstance(e, ModelUnavailableError):
                reason = "unavailable"
            elif isinstance(e, ParseError):
                reason = "parse"
            elif isinstance(e, ServerError):
                reason = "server"
            else:
                reason = "rate_limited"
            origin = e.__cause__ or e  # 형식 오류는 마지막 시도의 구체적인 원인을 함께 알린다
            notify(on_event, "fallback", model=model, reason=reason, unavailable=reason == "unavailable",
                   next_model=models[i + 1] if i + 1 < len(models) else None,
                   cause=str(origin), snippet=getattr(origin, "snippet", ""))
    last = errors[-1] if errors else None
    if errors and all(isinstance(e, ModelUnavailableError) for e in errors):
        raise AllModelsUnavailableError(MSG_ALL_UNAVAILABLE) from last
    if errors and all(isinstance(e, ParseError) for e in errors):
        raise ParseError(MSG_ALL_PARSE) from last
    busy = any(isinstance(e, RateLimitedError) and not isinstance(e, ServerError) for e in errors)
    if not busy and any(isinstance(e, ServerError) for e in errors):
        raise ServerError(MSG_SERVER) from last
    raise RateLimitedError(MSG_BUSY) from last
