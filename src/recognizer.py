# 작성: 2026-10-22 12:22 KST
# 1단계: 냉장고 사진에서 식재료를 인식한다. (화면과 분리된 순수 로직)
import base64
import io
import os
import time

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

import llm

URL = llm.URL
# 기본 모델: 이미지와 JSON 형식이 정확하고 약 19초(2026-10-22, 같은 이미지로 시험, 실제 사진은 더 걸릴 수 있다).
VISION_MODEL = "nvidia/nemotron-3.5-super-vl:free"
# 기본 모델이 429 등으로 계속 실패하면 순서대로 시도한다.
# 제외한 모델: minimax-m3.1-flash(2026-10-22 무료 기간이 끝나 :free 가 404), nemotron-3-nano-omni(175초로 너무 느림),
# inkling 계열(agentic 전용이라 403), gemma-4(Google AI Studio 공유 풀 한도로 429가 계속됨)
FALLBACK_MODELS = ("dots-studio/dots-3-note-preview:free",)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# 모델에 보내는 사진은 작게 줄인다. 한 변은 MAX_SIDE 이하, 용량은 TARGET_BYTES 안팎을 목표로 한다.
MAX_SIDE = 1024
TARGET_BYTES = 300 * 1024
JPEG_QUALITIES = (85, 75, 65, 55)
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
# 추론형 모델이라 작게 잡으면 응답이 중간에 끊긴다.
MAX_TOKENS = 2000
# 추론을 끈다. 켜 두면 물건이 많은 사진에서 추론이 MAX_TOKENS 를 다 써서 내용 없이 잘린다(2026-11-12 확인).
EXTRA = {"reasoning": {"enabled": False}}
# 재시도·시간 제한은 llm.py 가 정한다(429·5xx 는 한 번만 더 시도하고 그래도 안 되면 다음 모델).
TIMEOUT_SEC = llm.TIMEOUT_SEC
RETRY_DELAYS = llm.RETRY_DELAYS

CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

PROMPT = (
    "이 냉장고 사진에 보이는 식재료를 찾아 아래 JSON 형식으로만 답하세요. "
    "다른 설명이나 코드 블록 표시는 쓰지 마세요.\n"
    '{"ingredients": [{"name": "달걀", "quantity": "6개", "confidence": "high"}]}\n'
    "- name: 한국어 일반 명칭\n"
    "- quantity: 대략적인 수량, 알 수 없으면 빈 문자열\n"
    "- confidence: high, medium, low 중 하나\n"
    "- 용기, 선반, 가구 등 식재료가 아닌 것은 제외하세요."
)


# 오류 클래스는 llm.py 와 같은 것을 가리킨다. 화면은 RecognitionError 하나로 이미지·레시피 오류를 모두 잡을 수 있다.
RecognitionError = llm.ServiceError
ParseError = llm.ParseError
RateLimitedError = llm.RateLimitedError
ModelUnavailableError = llm.ModelUnavailableError


class ImageError(RecognitionError):
    pass


def get_api_key():
    """환경변수를 먼저 보고, 없으면 프로젝트 루트 .env 를 본다. 값은 출력하지 않는다."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, ".env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("\"'") or None
    except OSError:
        pass
    return None


def prepare_image(raw):
    """업로드 바이트를 검증하고, 회전을 반영해 긴 변 MAX_SIDE 이하, 용량 TARGET_BYTES 안팎의 JPEG 로 줄인다."""
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ImageError("파일이 너무 큽니다. 10MB 이하의 사진을 올려 주세요.")
    try:
        img = Image.open(io.BytesIO(raw))
        fmt = img.format
        if fmt not in ALLOWED_FORMATS:
            raise ImageError("JPEG, PNG, WebP 형식의 사진만 올릴 수 있습니다.")
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    except RecognitionError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ImageError("이미지 파일을 읽을 수 없습니다. 다른 사진을 올려 주세요.")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    # 용량이 목표를 넘으면 화질을 낮추고, 그래도 크면 크기를 줄인다.
    for _ in range(4):
        for quality in JPEG_QUALITIES:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            if buf.tell() <= TARGET_BYTES:
                return buf.getvalue()
        img = img.resize((max(1, int(img.width * 0.8)), max(1, int(img.height * 0.8))))
    return buf.getvalue()


def parse_ingredients(text):
    """모델 응답 텍스트를 검증하고 이름 기준으로 중복을 합친 목록으로 만든다."""
    data = llm.extract_json(text)
    items = data.get("ingredients") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ParseError("모델 응답에 ingredients 목록이 없습니다.", llm.snippet(text))
    merged = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        quantity = str(item.get("quantity") or "").strip()
        conf = str(item.get("confidence") or "").strip().lower()
        if conf not in CONFIDENCE_ORDER:
            conf = "medium"
        if name in merged:
            cur = merged[name]
            if not cur["quantity"]:
                cur["quantity"] = quantity
            if CONFIDENCE_ORDER[conf] > CONFIDENCE_ORDER[cur["confidence"]]:
                cur["confidence"] = conf
        else:
            merged[name] = {"name": name, "quantity": quantity, "confidence": conf}
    return list(merged.values())


def call_model(jpeg_bytes, api_key, model=VISION_MODEL, post=requests.post, sleep=time.sleep, on_event=None):
    """비전 모델 하나를 호출하고 응답 텍스트를 돌려준다. 429·5xx 는 llm.RETRY_DELAYS 만큼만 재시도한다."""
    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]
    return llm.chat(model, messages, api_key, MAX_TOKENS, post=post, sleep=sleep, on_event=on_event, extra=EXTRA)


def _recognize_with_model(jpeg, api_key, model, post, sleep, on_event=None):
    """모델 하나로 인식한다. 응답 형식이 깨지면 같은 모델에 1회 다시 요청한다."""
    last_error = None
    for n in range(2):
        if n:  # 첫 응답이 형식 오류였다. 무엇이 문제였는지 함께 알린다.
            llm.notify(on_event, "parse_retry", model=model, cause=str(last_error), snippet=getattr(last_error, "snippet", ""))
        try:
            text = call_model(jpeg, api_key, model=model, post=post, sleep=sleep, on_event=on_event)
            return parse_ingredients(text)
        except llm.TruncatedError as e:
            last_error = e
            break  # 잘림은 다시 물어도 똑같이 잘린다. 한도를 아끼려고 바로 대체 모델로 넘긴다.
        except ParseError as e:
            last_error = e
    raise ParseError("모델 응답을 해석하지 못했습니다. 다시 시도해 주세요.") from last_error


def recognize_detailed(raw_image, api_key, post=requests.post, sleep=time.sleep, on_event=None):
    """업로드 바이트 → 결과 정보.

    기본 모델이 429·5xx(한 번 재시도 후)나 사용 불가로 실패하면 FALLBACK_MODELS 를 차례로 시도한다.
    돌려주는 값: items(식재료 목록), model(실제 사용한 모델), original_bytes, sent_bytes
    on_event 를 주면 진행 상황(resize, request, retry, parse_retry, fallback, done)을 dict 로 알려 준다.
    """
    if not api_key:
        raise RecognitionError("서비스 설정에 문제가 있습니다. 관리자에게 문의해 주세요.")
    jpeg = prepare_image(raw_image)
    llm.notify(on_event, "resize", original_bytes=len(raw_image), sent_bytes=len(jpeg))
    models = (VISION_MODEL, *FALLBACK_MODELS)
    # 형식 오류·잘림도 대체 모델로 넘긴다(다른 모델은 제대로 답할 수 있다).
    model, items = llm.run_with_fallback(
        models, lambda m: _recognize_with_model(jpeg, api_key, m, post, sleep, on_event), on_event,
        fall_on=(RateLimitedError, ModelUnavailableError, ParseError),
    )
    llm.notify(on_event, "done", model=model, count=len(items))
    return {"items": items, "model": model, "original_bytes": len(raw_image), "sent_bytes": len(jpeg)}


def recognize(raw_image, api_key, post=requests.post, sleep=time.sleep):
    """업로드 바이트 → 식재료 목록."""
    return recognize_detailed(raw_image, api_key, post=post, sleep=sleep)["items"]
