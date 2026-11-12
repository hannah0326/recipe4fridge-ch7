# 작성: 2026-10-22 12:23 KST
import io
import os
import sys

import pytest
import requests
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import recognizer as rec  # noqa: E402


def make_image(size=(200, 100), fmt="PNG", color="white"):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format=fmt)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, status=200, content=None):
        self.status_code = status
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class FakePost:
    """호출할 때마다 미리 정한 응답을 순서대로 돌려준다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


GOOD = '{"ingredients": [{"name": "달걀", "quantity": "6개", "confidence": "high"}]}'


# --- 이미지 검증 ---
def test_prepare_image_resizes_long_side():
    out = Image.open(io.BytesIO(rec.prepare_image(make_image((3000, 1000)))))
    assert out.format == "JPEG"
    assert max(out.size) <= rec.MAX_SIDE


def test_prepare_image_shrinks_noisy_photo_under_target_size():
    # 무작위 잡음 사진은 압축이 잘 안 되어 용량이 크다. 그래도 목표 용량 안으로 줄어야 한다.
    noise = Image.frombytes("RGB", (1600, 1200), os.urandom(1600 * 1200 * 3))
    buf = io.BytesIO()
    noise.save(buf, format="PNG")
    out = rec.prepare_image(buf.getvalue())
    assert len(out) <= rec.TARGET_BYTES
    assert max(Image.open(io.BytesIO(out)).size) <= rec.MAX_SIDE


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_prepare_image_accepts_allowed_formats(fmt):
    assert rec.prepare_image(make_image(fmt=fmt))


def test_prepare_image_rejects_gif():
    with pytest.raises(rec.ImageError):
        rec.prepare_image(make_image(fmt="GIF"))


def test_prepare_image_rejects_non_image():
    with pytest.raises(rec.ImageError):
        rec.prepare_image(b"not an image")


def test_prepare_image_rejects_oversize():
    with pytest.raises(rec.ImageError):
        rec.prepare_image(b"0" * (rec.MAX_UPLOAD_BYTES + 1))


def test_prepare_image_applies_exif_rotation():
    img = Image.new("RGB", (200, 100), "white")
    exif = Image.Exif()
    exif[0x0112] = 6  # 시계 방향 90도 회전이 필요한 사진
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    out = Image.open(io.BytesIO(rec.prepare_image(buf.getvalue())))
    assert out.size == (100, 200)


# --- 응답 파싱 ---
def test_parse_plain_json():
    assert rec.parse_ingredients(GOOD) == [{"name": "달걀", "quantity": "6개", "confidence": "high"}]


def test_parse_json_inside_code_fence_and_text():
    text = "결과입니다.\n```json\n" + GOOD + "\n```\n끝"
    assert rec.parse_ingredients(text)[0]["name"] == "달걀"


def test_parse_merges_duplicates_by_name():
    text = (
        '{"ingredients": ['
        '{"name": "우유", "quantity": "", "confidence": "low"},'
        '{"name": "우유", "quantity": "1통", "confidence": "high"}]}'
    )
    assert rec.parse_ingredients(text) == [{"name": "우유", "quantity": "1통", "confidence": "high"}]


def test_parse_fixes_invalid_confidence_and_skips_bad_items():
    text = (
        '{"ingredients": [{"name": "당근", "confidence": "??"}, {"name": ""}, "문자열", '
        '{"name": "  양파  ", "quantity": null}]}'
    )
    assert rec.parse_ingredients(text) == [
        {"name": "당근", "quantity": "", "confidence": "medium"},
        {"name": "양파", "quantity": "", "confidence": "medium"},
    ]


def test_parse_empty_list_is_valid():
    assert rec.parse_ingredients('{"ingredients": []}') == []


@pytest.mark.parametrize("bad", ["", "설명뿐", "{깨진 json", '{"foods": []}', '{"ingredients": "x"}'])
def test_parse_rejects_bad_output(bad):
    with pytest.raises(rec.ParseError):
        rec.parse_ingredients(bad)


# --- 모델 호출·재시도 ---
def test_call_model_sends_image_and_large_max_tokens():
    post = FakePost([FakeResponse(200, GOOD)])
    rec.call_model(b"jpegbytes", "KEY", post=post, sleep=lambda s: None)
    sent = post.calls[0]
    assert sent["json"]["model"] == rec.VISION_MODEL
    assert sent["json"]["max_tokens"] >= 2000
    assert sent["json"]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert sent["headers"]["Authorization"] == "Bearer KEY"


def test_call_model_retries_429_once_then_succeeds():
    post = FakePost([FakeResponse(429), FakeResponse(200, GOOD)])
    waits = []
    assert rec.call_model(b"x", "K", post=post, sleep=waits.append) == GOOD
    assert waits == [2]


def test_call_model_raises_rate_limit_after_one_retry():
    post = FakePost([FakeResponse(429), FakeResponse(429)])
    waits = []
    with pytest.raises(rec.RateLimitedError):
        rec.call_model(b"x", "K", post=post, sleep=waits.append)
    assert waits == [2]
    assert len(post.calls) == 2


@pytest.mark.parametrize("status", [403, 404, 410])
def test_call_model_marks_model_unavailable(status):
    with pytest.raises(rec.ModelUnavailableError):
        rec.call_model(b"x", "K", post=FakePost([FakeResponse(status)]), sleep=lambda s: None)


def test_call_model_uses_given_model():
    post = FakePost([FakeResponse(200, GOOD)])
    rec.call_model(b"x", "K", model="other/model:free", post=post, sleep=lambda s: None)
    assert post.calls[0]["json"]["model"] == "other/model:free"


def test_call_model_auth_error_hides_details():
    post = FakePost([FakeResponse(401)])
    with pytest.raises(rec.RecognitionError) as e:
        rec.call_model(b"x", "SECRETKEY", post=post, sleep=lambda s: None)
    assert "SECRETKEY" not in str(e.value)
    assert "401" not in str(e.value)


def test_call_model_timeout_message():
    post = FakePost([requests.Timeout()])
    with pytest.raises(rec.RecognitionError):
        rec.call_model(b"x", "K", post=post, sleep=lambda s: None)


# --- 전체 흐름 ---
def test_recognize_retries_once_on_bad_format():
    post = FakePost([FakeResponse(200, "엉망"), FakeResponse(200, GOOD)])
    result = rec.recognize(make_image(), "K", post=post, sleep=lambda s: None)
    assert result[0]["name"] == "달걀"
    assert len(post.calls) == 2


def test_recognize_fails_when_every_model_answers_badly_twice():
    post = FakePost([FakeResponse(200, "엉망")] * (2 * (1 + len(rec.FALLBACK_MODELS))))
    with pytest.raises(rec.ParseError):
        rec.recognize(make_image(), "K", post=post, sleep=lambda s: None)


def test_bad_format_twice_falls_back_to_next_model():
    # 기본 모델이 두 번 다 형식에 어긋나게 답하면 대체 모델로 넘어간다(전에는 여기서 실패로 끝났다).
    post = FakePost([FakeResponse(200, "엉망"), FakeResponse(200, "또 엉망"), FakeResponse(200, GOOD)])
    result = rec.recognize_detailed(make_image(), "K", post=post, sleep=lambda s: None)
    assert models_used(post) == [rec.VISION_MODEL, rec.VISION_MODEL, rec.FALLBACK_MODELS[0]]
    assert result["model"] == rec.FALLBACK_MODELS[0]


class TruncatedResponse:
    """추론에 max_tokens 를 다 써서 내용 없이 잘린 응답(물건이 많은 냉장고 사진에서 실제로 일어났다)."""

    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}


def test_call_model_turns_reasoning_off():
    # 추론을 켜 두면 복잡한 사진에서 추론이 토큰을 다 써 버려 답이 비어 버린다.
    post = FakePost([FakeResponse(200, GOOD)])
    rec.call_model(b"x", "K", post=post, sleep=lambda s: None)
    assert post.calls[0]["json"]["reasoning"] == {"enabled": False}


def test_empty_truncated_answer_is_reported_as_truncation_not_empty():
    with pytest.raises(rec.ParseError) as e:
        rec.call_model(b"x", "K", post=FakePost([TruncatedResponse()]), sleep=lambda s: None)
    assert "잘렸" in str(e.value)


def test_truncated_answer_goes_straight_to_fallback_without_same_model_retry():
    # 잘림은 같은 모델에 다시 물어도 똑같이 잘리므로 한도만 쓴다. 바로 대체 모델로 간다.
    post = FakePost([TruncatedResponse(), FakeResponse(200, GOOD)])
    result = rec.recognize_detailed(make_image(), "K", post=post, sleep=lambda s: None)
    assert models_used(post) == [rec.VISION_MODEL, rec.FALLBACK_MODELS[0]]
    assert result["items"][0]["name"] == "달걀"


def models_used(post):
    return [c["json"]["model"] for c in post.calls]


def test_fallback_after_primary_429_twice():
    # 기본 모델: 429 → (한 번 재시도) 429 → 첫 대체 모델에서 성공
    post = FakePost([FakeResponse(429), FakeResponse(429), FakeResponse(200, GOOD)])
    result = rec.recognize_detailed(make_image(), "K", post=post, sleep=lambda s: None)
    assert models_used(post) == [rec.VISION_MODEL, rec.VISION_MODEL, rec.FALLBACK_MODELS[0]]
    assert result["model"] == rec.FALLBACK_MODELS[0]
    assert result["items"][0]["name"] == "달걀"


def test_no_fallback_when_primary_succeeds_after_one_retry():
    post = FakePost([FakeResponse(429), FakeResponse(200, GOOD)])
    result = rec.recognize_detailed(make_image(), "K", post=post, sleep=lambda s: None)
    assert result["model"] == rec.VISION_MODEL
    assert len(post.calls) == 2


def test_fallback_skips_unavailable_primary_model_without_retrying_it():
    # 기본 모델이 서비스를 끝내 404 를 주면 재시도하지 않고 바로 다음 모델로 넘어간다.
    post = FakePost([FakeResponse(404), FakeResponse(200, GOOD)])
    events = []
    result = rec.recognize_detailed(make_image(), "K", post=post, sleep=lambda s: None, on_event=events.append)
    assert models_used(post) == [rec.VISION_MODEL, rec.FALLBACK_MODELS[0]]
    assert result["model"] == rec.FALLBACK_MODELS[0]
    fb = [e for e in events if e["kind"] == "fallback"][0]
    assert fb["unavailable"] is True and fb["reason"] == "unavailable"


def test_fallback_walks_a_longer_list(monkeypatch):
    monkeypatch.setattr(rec, "FALLBACK_MODELS", ("a/x:free", "b/y:free"))
    post = FakePost([FakeResponse(404), FakeResponse(429), FakeResponse(429), FakeResponse(200, GOOD)])
    result = rec.recognize_detailed(make_image(), "K", post=post, sleep=lambda s: None)
    assert result["model"] == "b/y:free"


def test_all_models_rate_limited_gives_friendly_error():
    post = FakePost([FakeResponse(429)] * (2 * (1 + len(rec.FALLBACK_MODELS))))
    with pytest.raises(rec.RateLimitedError) as e:
        rec.recognize_detailed(make_image(), "K", post=post, sleep=lambda s: None)
    assert "잠시 후" in str(e.value)
    assert set(models_used(post)) == {rec.VISION_MODEL, *rec.FALLBACK_MODELS}


def test_auth_error_does_not_try_fallback_models():
    post = FakePost([FakeResponse(401)])
    with pytest.raises(rec.RecognitionError):
        rec.recognize_detailed(make_image(), "K", post=post, sleep=lambda s: None)
    assert len(post.calls) == 1


def test_recognize_detailed_reports_sizes():
    raw = make_image((3000, 2000), fmt="PNG")
    post = FakePost([FakeResponse(200, GOOD)])
    result = rec.recognize_detailed(raw, "K", post=post, sleep=lambda s: None)
    assert result["original_bytes"] == len(raw)
    assert 0 < result["sent_bytes"] <= rec.TARGET_BYTES


def test_events_report_resize_retry_fallback_done_in_order():
    post = FakePost([FakeResponse(429), FakeResponse(429), FakeResponse(200, GOOD)])
    events = []
    rec.recognize_detailed(make_image(), "K", post=post, sleep=lambda s: None, on_event=events.append)
    kinds = [e["kind"] for e in events]
    # 요청마다 응답(상태·걸린 시간)이 따라온다
    assert kinds == ["resize", "request", "response", "retry", "request", "response", "fallback",
                     "request", "response", "done"]
    assert events[1]["model"] == rec.VISION_MODEL and events[1]["attempt"] == 1
    assert events[2]["status"] == 429 and events[2]["seconds"] >= 0
    assert events[3]["status"] == 429 and events[3]["wait"] == 2
    assert events[4]["attempt"] == 2
    assert events[6]["next_model"] == rec.FALLBACK_MODELS[0]
    assert events[7]["model"] == rec.FALLBACK_MODELS[0]
    assert events[8]["status"] == 200
    assert events[9]["count"] == 1


def test_parse_retry_event_tells_why_the_first_answer_was_unusable():
    post = FakePost([FakeResponse(200, "죄송하지만 알 수 없습니다"), FakeResponse(200, GOOD)])
    events = []
    rec.recognize_detailed(make_image(), "K", post=post, sleep=lambda s: None, on_event=events.append)
    retry = [e for e in events if e["kind"] == "parse_retry"][0]
    assert "JSON" in retry["cause"] and retry["snippet"].startswith("죄송하지만")


class DailyLimitResponse:
    status_code = 429
    headers = {"X-RateLimit-Limit": "50", "X-RateLimit-Reset": "1792713600000"}

    def json(self):
        return {"error": {"message": "Rate limit exceeded: free-models-per-day", "code": 429,
                          "metadata": {"limit_source": "openrouter_free_tier_daily"}}}


def test_image_daily_limit_gives_the_exact_reason_and_does_not_try_other_models():
    post = FakePost([DailyLimitResponse()])
    with pytest.raises(rec.RateLimitedError) as e:
        rec.recognize_detailed(make_image(), "K", post=post, sleep=lambda s: None)
    msg = str(e.value)
    assert "50회" in msg and "10월 23일 오전 9시" in msg and "잠시 후" not in msg
    assert len(post.calls) == 1                         # 대체 모델도 같은 한도라 시도하지 않는다


def test_image_all_models_gone_says_models_are_unavailable():
    n = 1 + len(rec.FALLBACK_MODELS)
    post = FakePost([FakeResponse(404)] * n)
    with pytest.raises(rec.RecognitionError) as e:
        rec.recognize_detailed(make_image(), "K", post=post, sleep=lambda s: None)
    assert "모두 사용할 수 없" in str(e.value) and "잠시 후" not in str(e.value)


def test_events_not_required():
    post = FakePost([FakeResponse(200, GOOD)])
    assert rec.recognize_detailed(make_image(), "K", post=post, sleep=lambda s: None)["items"]


def test_recognize_without_key_does_not_call_api():
    post = FakePost([])
    with pytest.raises(rec.RecognitionError):
        rec.recognize(make_image(), None, post=post, sleep=lambda s: None)
    assert post.calls == []


def test_get_api_key_prefers_environment(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    assert rec.get_api_key() == "from-env"
