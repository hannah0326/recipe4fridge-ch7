# 작성: 2026-10-22 22:31 KST
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import llm  # noqa: E402

MESSAGES = [{"role": "user", "content": "안녕"}]


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def ok(text="안녕하세요"):
    return Resp(200, {"choices": [{"message": {"content": text}}]})


class FakePost:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def chat(post, **kw):
    return llm.chat("m/a:free", MESSAGES, "KEY", 100, post=post, sleep=lambda s: None, **kw)


def test_chat_returns_content_and_sends_expected_body():
    post = FakePost([ok("결과")])
    assert chat(post) == "결과"
    sent = post.calls[0]
    assert sent["json"] == {"model": "m/a:free", "max_tokens": 100, "messages": MESSAGES}
    assert sent["headers"]["Authorization"] == "Bearer KEY"


def test_chat_merges_extra_body_for_reasoning_off():
    post = FakePost([ok()])
    chat(post, extra={"reasoning": {"enabled": False}})
    assert post.calls[0]["json"]["reasoning"] == {"enabled": False}


def test_error_object_inside_http_200_is_treated_as_its_code():
    # OpenRouter 는 200 본문에 error 를 담아 보내기도 한다.
    post = FakePost([Resp(200, {"error": {"code": 429, "message": "x"}}), ok("재시도 성공")])
    assert chat(post) == "재시도 성공"
    assert len(post.calls) == 2


def test_error_object_with_unavailable_code_marks_model_unavailable():
    with pytest.raises(llm.ModelUnavailableError):
        chat(FakePost([Resp(200, {"error": {"code": 404}})]))


def test_error_object_without_numeric_code_is_retried_as_server_error():
    post = FakePost([Resp(200, {"error": {"message": "?"}}), Resp(200, {"error": {"message": "?"}})])
    with pytest.raises(llm.RateLimitedError):
        chat(post)
    assert len(post.calls) == 2


@pytest.mark.parametrize("status", [400, 403, 404, 410])
def test_bad_request_and_missing_model_fall_back_to_next_model(status):
    with pytest.raises(llm.ModelUnavailableError):
        chat(FakePost([Resp(status)]))


def test_missing_choices_or_null_content_is_parse_error():
    for body in ({}, {"choices": []}, {"choices": [{"message": {"content": None}}]}):
        with pytest.raises(llm.ParseError):
            chat(FakePost([Resp(200, body)]))


def test_retry_once_then_rate_limited():
    post = FakePost([Resp(429), Resp(503)])
    waits = []
    with pytest.raises(llm.RateLimitedError):
        llm.chat("m", MESSAGES, "K", 10, post=post, sleep=waits.append)
    assert waits == [2] and len(post.calls) == 2


def test_401_message_hides_details():
    with pytest.raises(llm.ServiceError) as e:
        chat(FakePost([Resp(401)]))
    assert "401" not in str(e.value) and "KEY" not in str(e.value)


# --- 대체 모델 순서 ---
def test_run_with_fallback_returns_first_success_without_events():
    events = []
    model, result = llm.run_with_fallback(["a", "b"], lambda m: m.upper(), events.append)
    assert (model, result) == ("a", "A") and events == []


def test_run_with_fallback_walks_models_and_reports_reasons():
    def attempt(m):
        if m == "a":
            raise llm.RateLimitedError("x")
        if m == "b":
            raise llm.ModelUnavailableError("x")
        return "성공"

    events = []
    model, result = llm.run_with_fallback(["a", "b", "c"], attempt, events.append)
    assert (model, result) == ("c", "성공")
    assert [(e["model"], e["reason"], e["next_model"]) for e in events] == [("a", "rate_limited", "b"), ("b", "unavailable", "c")]


def test_run_with_fallback_does_not_catch_other_errors():
    def attempt(m):
        raise llm.ServiceError("키 문제")

    with pytest.raises(llm.ServiceError) as e:
        llm.run_with_fallback(["a", "b"], attempt)
    assert not isinstance(e.value, llm.RateLimitedError)


def test_run_with_fallback_can_also_fall_back_on_parse_error():
    def attempt(m):
        if m == "a":
            raise llm.ParseError("x")
        return "ok"

    events = []
    model, _ = llm.run_with_fallback(
        ["a", "b"], attempt, events.append, fall_on=(llm.RateLimitedError, llm.ModelUnavailableError, llm.ParseError)
    )
    assert model == "b" and events[0]["reason"] == "parse"


def test_run_with_fallback_all_failed_raises_friendly_error_with_cause():
    def attempt(m):
        raise llm.RateLimitedError("x")

    events = []
    with pytest.raises(llm.RateLimitedError) as e:
        llm.run_with_fallback(["a", "b"], attempt, events.append)
    assert "잠시 후" in str(e.value) and e.value.__cause__ is not None
    assert events[-1]["next_model"] is None


# --- 하루 한도 소진 ---
DAILY_BODY = {
    "error": {
        "message": "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day",
        "code": 429,
        "metadata": {
            "headers": {"X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1792713600000"},
            "limit_source": "openrouter_free_tier_daily",
        },
    }
}


def daily_response():
    return Resp(429, DAILY_BODY)


def test_daily_limit_gets_a_specific_message_with_korean_reset_time():
    events = []
    with pytest.raises(llm.DailyLimitError) as e:
        chat(FakePost([daily_response()]), on_event=events.append)
    msg = str(e.value)
    assert "50회" in msg and "한국 시각 10월 23일 오전 9시" in msg and "다시 시도해도 같은 오류" in msg
    assert "잠시 후" not in msg                      # 틀린 안내("잠시 후 다시")를 하지 않는다
    assert [x["kind"] for x in events] == ["request", "response", "daily_limit"]
    assert events[-1]["reset"] == "한국 시각 10월 23일 오전 9시"


def test_daily_limit_is_not_retried():
    post = FakePost([daily_response(), ok()])
    with pytest.raises(llm.DailyLimitError):
        chat(post)
    assert len(post.calls) == 1                      # 재시도하지 않는다


def test_daily_limit_is_recognized_from_message_only_and_inside_http_200():
    only_message = Resp(429, {"error": {"message": "Rate limit exceeded: free-models-per-day"}})
    with pytest.raises(llm.DailyLimitError) as e:
        chat(FakePost([only_message]))
    assert "횟수를 모두" in str(e.value) and "한국 시각 오전 9시" in str(e.value)   # 한도 숫자·시각을 모르면 일반 문구
    inside_200 = Resp(200, {"error": {**DAILY_BODY["error"]}})
    with pytest.raises(llm.DailyLimitError):
        chat(FakePost([inside_200]))


def test_daily_limit_reads_headers_of_the_response_too():
    r = Resp(429, {"error": {"message": "free-models-per-day"}})
    r.headers = {"X-RateLimit-Limit": "50", "X-RateLimit-Reset": "1792713600000"}
    with pytest.raises(llm.DailyLimitError) as e:
        chat(FakePost([r]))
    assert "50회" in str(e.value) and "10월 23일 오전 9시" in str(e.value)


def test_ordinary_429_is_still_a_busy_error_that_can_be_retried():
    other = Resp(429, {"error": {"message": "provider is busy", "metadata": {"limit_source": "provider"}}})
    post = FakePost([other, other])
    with pytest.raises(llm.RateLimitedError) as e:
        chat(post)
    assert not isinstance(e.value, llm.DailyLimitError) and "잠시 후" in str(e.value)
    assert len(post.calls) == 2


def test_run_with_fallback_stops_at_daily_limit_without_trying_other_models():
    tried = []

    def attempt(m):
        tried.append(m)
        raise llm.DailyLimitError(50, 1792713600000)

    with pytest.raises(llm.DailyLimitError):
        llm.run_with_fallback(["a", "b", "c"], attempt)
    assert tried == ["a"]                            # 다른 모델도 같은 한도라 시도해 봐야 소용없다


@pytest.mark.parametrize("ms,expected", [
    (1792713600000, "한국 시각 10월 23일 오전 9시"),
    (1792717200000, "한국 시각 10월 23일 오전 10시"),
    (1792762200000, "한국 시각 10월 23일 오후 10시 30분"),   # 오전 9시 + 13시간 30분
    (1792681200000, "한국 시각 10월 23일 오전 12시"),         # 자정
    (1792724400000, "한국 시각 10월 23일 오후 12시"),         # 정오
    (None, "한국 시각 오전 9시"), ("엉뚱", "한국 시각 오전 9시")])
def test_format_reset_time_is_korean_time(ms, expected):
    assert llm.format_reset(ms) == expected


# --- 모든 모델 종료·형식 오류 ---
def test_all_models_unavailable_says_so_instead_of_busy():
    def attempt(m):
        raise llm.ModelUnavailableError("x")

    with pytest.raises(llm.AllModelsUnavailableError) as e:
        llm.run_with_fallback(["a", "b"], attempt)
    msg = str(e.value)
    assert "모두 사용할 수 없" in msg and "관리자" in msg and "잠시 후" not in msg
    assert not isinstance(e.value, llm.RateLimitedError)


def test_mixed_failures_remain_a_busy_error():
    errors = {"a": llm.ModelUnavailableError("x"), "b": llm.RateLimitedError("y")}

    def attempt(m):
        raise errors[m]

    with pytest.raises(llm.RateLimitedError) as e:
        llm.run_with_fallback(["a", "b"], attempt)
    assert "잠시 후" in str(e.value)


def test_all_models_with_bad_format_says_the_format_was_wrong():
    def attempt(m):
        raise llm.ParseError("x")

    with pytest.raises(llm.ParseError) as e:
        llm.run_with_fallback(["a", "b"], attempt, fall_on=(llm.ParseError,))
    assert "올바른 형식" in str(e.value)


# --- 진행 기록: 응답 시간과 형식 오류 원인 ---
def test_every_request_reports_status_and_seconds():
    events = []
    chat(FakePost([Resp(429), ok()]), on_event=events.append)
    responses = [e for e in events if e["kind"] == "response"]
    assert [r["status"] for r in responses] == [429, 200]
    assert all(isinstance(r["seconds"], float) and r["seconds"] >= 0 for r in responses)


@pytest.mark.parametrize("body,part", [
    ({"choices": [{"message": {"content": ""}}]}, "빈 응답"),
    ({"choices": []}, "choices"),
    ({"choices": [{"message": {"content": '{"a": 1'}, "finish_reason": "length"}]}, "잘렸"),
])
def test_empty_missing_and_truncated_answers_get_distinct_causes(body, part):
    with pytest.raises(llm.ParseError) as e:
        chat(FakePost([Resp(200, body)]))
    assert part in str(e.value)


def test_json_errors_carry_the_beginning_of_the_answer_as_a_snippet():
    with pytest.raises(llm.ParseError) as e:
        llm.extract_json("죄송합니다.\n  재료를   찾을 수   없어요")
    assert "JSON" in str(e.value) and e.value.snippet == "죄송합니다. 재료를 찾을 수 없어요"
    with pytest.raises(llm.ParseError) as e2:
        llm.extract_json("{깨진: json}")
    assert "올바른 JSON" in str(e2.value) and e2.value.snippet.startswith("{깨진")
    assert len(llm.snippet("가" * 500)) == 60


def test_fallback_event_for_bad_format_carries_the_underlying_cause():
    def attempt(m):
        raise llm.ParseError("레시피 응답을 해석하지 못했습니다.") from llm.ParseError("모델이 빈 응답을 보냈습니다.", "")

    events = []
    with pytest.raises(llm.ParseError):
        llm.run_with_fallback(["a"], attempt, events.append, fall_on=(llm.ParseError,))
    assert events[0]["reason"] == "parse" and events[0]["cause"] == "모델이 빈 응답을 보냈습니다."


# --- JSON 추출 ---
@pytest.mark.parametrize(
    "text",
    ['{"a": 1}', '```json\n{"a": 1}\n```', '설명입니다.\n{"a": 1}\n끝'],
)
def test_extract_json_accepts_fences_and_prose(text):
    assert llm.extract_json(text) == {"a": 1}


@pytest.mark.parametrize("bad", ["", None, "없음", "{깨진", "[1, 2]"])
def test_extract_json_rejects_bad_text(bad):
    with pytest.raises(llm.ParseError):
        llm.extract_json(bad)



# --- 5xx 는 요청 제한이 아니라 서버 오류로 안내한다 ---
def test_server_errors_are_reported_as_server_problems_not_rate_limits():
    post = FakePost([Resp(502), Resp(503)])
    with pytest.raises(llm.ServerError) as e:
        llm.chat("m", MESSAGES, "K", 10, post=post, sleep=lambda s: None)
    assert str(e.value) == llm.MSG_SERVER and "몰려" not in str(e.value)
    assert isinstance(e.value, llm.RateLimitedError)               # 대체 모델로 넘어가는 동작은 그대로다


def test_repeated_429_is_still_a_busy_message():
    with pytest.raises(llm.RateLimitedError) as e:
        llm.chat("m", MESSAGES, "K", 10, post=FakePost([Resp(429), Resp(429)]), sleep=lambda s: None)
    assert not isinstance(e.value, llm.ServerError) and str(e.value) == llm.MSG_BUSY


def test_run_with_fallback_tells_server_errors_apart_from_rate_limits():
    def all_server(m):
        raise llm.ServerError(llm.MSG_SERVER)

    events = []
    with pytest.raises(llm.ServerError) as e:
        llm.run_with_fallback(["a", "b"], all_server, events.append)
    assert str(e.value) == llm.MSG_SERVER and [ev["reason"] for ev in events] == ["server", "server"]

    def mixed(m):
        raise llm.ServerError(llm.MSG_SERVER) if m == "a" else llm.RateLimitedError(llm.MSG_BUSY)

    with pytest.raises(llm.RateLimitedError) as e:
        llm.run_with_fallback(["a", "b"], mixed)
    assert str(e.value) == llm.MSG_BUSY                              # 요청 제한이 섞였으면 요청 제한으로 안내한다
