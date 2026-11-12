# 작성: 2026-10-22 22:35 KST
# Streamlit 화면 흐름 테스트. 모델 호출(recipe.generate_recipes)은 가짜로 바꿔 끼운다.
import os
import sys

import pytest
from streamlit.testing.v1 import AppTest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, SRC)
import llm  # noqa: E402
import recipe  # noqa: E402
import recognizer  # noqa: E402

APP = os.path.join(SRC, "app.py")
CONFIRMED = [{"name": "달걀", "quantity": "6개", "confidence": "high"}, {"name": "양배추", "quantity": "", "confidence": "high"}]


def fake_recipe(title="달걀 볶음", **kw):
    r = {"title": title, "summary": "간단한 요리", "cuisine": "한식", "servings": 2, "time_minutes": 20, "difficulty": "easy",
         "ingredients": [{"name": "달걀", "amount": "2개", "have": True}, {"name": "대파", "amount": "1대", "have": False}],
         "missing": ["대파"], "steps": ["볶는다.", "담는다."]}
    r.update(kw)
    return r


def fake_result(*recipes, model=None, excluded=(), requested=None):
    recipes = list(recipes) or [fake_recipe()]
    return {"recipes": recipes, "excluded": list(excluded), "model": model or recipe.TEXT_MODEL,
            "requested": requested or len(recipes)}


@pytest.fixture
def app():
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["confirmed"] = CONFIRMED
    return at


def button(at, label):
    return [b for b in at.button if b.label == label][0]


def texts(at):
    return " ".join(str(m.value) for m in at.markdown) + " " + " ".join(str(c.value) for c in at.caption)


def test_recipe_section_only_appears_after_ingredients_are_confirmed():
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception and not [b for b in at.button if b.label == "레시피 생성"]
    at.session_state["confirmed"] = CONFIRMED
    at.run()
    assert [b for b in at.button if b.label == "레시피 생성"]


def test_options_and_safety_notice_are_shown(app):
    app.run()
    assert not app.exception
    assert [n.label for n in app.number_input] == ["인원수"]
    assert [s.label for s in app.selectbox] == ["요리 종류", "난이도", "조리 시간"]
    assert [s.value for s in app.selectbox] == ["제한 없음", "제한 없음", "제한 없음"]   # 기본은 모두 제한 없음
    assert app.selectbox[2].options == ["제한 없음", "15분 이내", "30분 이내", "30분 이상"]   # 60분 이내는 없다
    assert [m.label for m in app.multiselect] == ["알레르기", "식단 제한"]
    assert app.radio[0].value == "소량" and app.slider[0].value == 3
    assert "직접 꼭 확인" in texts(app)          # 알레르기 고지는 항상 보인다
    assert "달걀" in texts(app)                  # 확정한 재료가 칩으로 보인다


def test_generate_passes_chosen_options_and_shows_cards(app, monkeypatch):
    seen = {}

    def fake(ingredients, options, key, on_event=None, **kw):
        seen.update(ingredients=ingredients, options=options)
        on_event({"kind": "request", "model": "m/a", "attempt": 1})
        return fake_result(fake_recipe("달걀 볶음"), fake_recipe("양배추 국", time_minutes=15))

    monkeypatch.setattr(recipe, "generate_recipes", fake)
    app.run()
    app.number_input[0].set_value(3)
    app.multiselect[0].set_value(["견과류"])
    app.radio[0].set_value("불가")
    app.slider[0].set_value(2)
    button(app, "레시피 생성").click().run()
    assert not app.exception
    assert seen["options"]["servings"] == 3 and seen["options"]["allergies"] == ["견과류"]
    assert seen["options"]["extra"] == "불가" and seen["options"]["count"] == 2 and seen["options"]["max_minutes"] is None
    assert [i["name"] for i in seen["ingredients"]] == ["달걀", "양배추"]
    t = texts(app)
    assert "1. 달걀 볶음" in t and "2. 양배추 국" in t and "부족한 재료 1개" in t and "대파" in t
    assert button(app, "다시 생성")              # 결과가 있으면 단추 이름이 바뀐다


def test_unrestricted_cuisine_and_difficulty_reach_the_generator_as_none(app, monkeypatch):
    # 화면에는 "제한 없음" 글자를 값으로 쓰지만, 레시피 생성에는 None(제한 없음)으로 넘겨야 한다.
    seen = {}

    def fake(ingredients, options, key, on_event=None, **kw):
        seen["options"] = options
        return fake_result()

    monkeypatch.setattr(recipe, "generate_recipes", fake)
    app.run()
    button(app, "레시피 생성").click().run()
    assert not app.exception
    assert (seen["options"]["cuisine"], seen["options"]["difficulty"]) == (None, None)


def test_cuisine_difficulty_and_time_choices_reach_the_generator(app, monkeypatch):
    seen = {}

    def fake(ingredients, options, key, on_event=None, **kw):
        seen["options"] = options
        return fake_result()

    monkeypatch.setattr(recipe, "generate_recipes", fake)
    app.run()
    assert app.selectbox[0].options == ["제한 없음", "한식", "양식", "중식", "일식"]
    assert app.selectbox[1].options == ["제한 없음", "초급", "중급", "고급"]
    app.selectbox[0].set_value("중식")
    app.selectbox[1].set_value("hard")
    app.selectbox[2].set_value("30분 이상")
    button(app, "레시피 생성").click().run()
    assert not app.exception
    o = seen["options"]
    assert (o["cuisine"], o["difficulty"]) == ("중식", "hard")
    assert (o["min_minutes"], o["max_minutes"]) == (30, None)         # "이상"은 최소 시간
    # "이내"는 최대 시간으로 간다
    app.selectbox[2].set_value("15분 이내")
    button(app, "다시 생성").click().run()
    assert (seen["options"]["min_minutes"], seen["options"]["max_minutes"]) == (None, 15)
    assert "한식 · ⏱ 20분 · 초급" in texts(app)                      # 카드에 요리 종류·난이도 이름이 보인다


def test_failure_is_shown_honestly_and_clears_the_previous_result(app, monkeypatch):
    app.session_state["recipe_result"] = fake_result(fake_recipe("옛 레시피"))

    def boom(*a, **k):
        raise llm.RateLimitedError("지금 요청이 몰려 있습니다. 잠시 후 다시 시도해 주세요.")

    monkeypatch.setattr(recipe, "generate_recipes", boom)
    app.run()
    assert "옛 레시피" in texts(app)
    button(app, "다시 생성").click().run()
    assert not app.exception
    assert "recipe_result" not in app.session_state          # 이전 결과를 이번 결과처럼 보이지 않게 한다
    assert "옛 레시피" not in texts(app)
    assert any("레시피 생성에 실패했어요" in e.value for e in app.error)


def test_daily_limit_message_is_shown_as_is_and_not_as_a_busy_message(app, monkeypatch):
    def boom(*a, on_event=None, **k):
        on_event({"kind": "daily_limit", "model": "m/a", "reset": "한국 시각 10월 23일 오전 9시"})
        raise llm.DailyLimitError(50, 1792713600000)

    monkeypatch.setattr(recipe, "generate_recipes", boom)
    app.run()
    button(app, "레시피 생성").click().run()
    assert not app.exception
    shown = " ".join(e.value for e in app.error)
    assert "무료 호출 50회를 모두 사용했어요" in shown and "10월 23일 오전 9시" in shown
    assert "잠시 후 다시" not in shown
    assert any("하루 무료 호출 한도" in line for line in app.session_state["recipe_log"])


def test_all_models_gone_message_is_shown(app, monkeypatch):
    def boom(*a, **k):
        raise llm.AllModelsUnavailableError(llm.MSG_ALL_UNAVAILABLE)

    monkeypatch.setattr(recipe, "generate_recipes", boom)
    app.run()
    button(app, "레시피 생성").click().run()
    shown = " ".join(e.value for e in app.error)
    assert "모두 사용할 수 없어요" in shown and "잠시 후" not in shown


def test_progress_log_shows_response_time_bad_format_cause_and_total_time(app, monkeypatch):
    def fake(ingredients, options, key, on_event=None, **kw):
        on_event({"kind": "request", "model": "m/a", "attempt": 1})
        on_event({"kind": "response", "model": "m/a", "status": 200, "seconds": 12.3})
        on_event({"kind": "parse_retry", "model": "m/a", "cause": "모델이 빈 응답을 보냈습니다.", "snippet": ""})
        on_event({"kind": "parse_retry", "model": "m/a", "cause": "모델 응답에서 JSON 을 찾지 못했습니다.", "snippet": "죄송합니다"})
        return fake_result()

    monkeypatch.setattr(recipe, "generate_recipes", fake)
    app.run()
    button(app, "레시피 생성").click().run()
    log = app.session_state["recipe_log"]
    assert "↳ 200 응답 (12.3초)" in log
    assert any("원인: 모델이 빈 응답을 보냈습니다" in line for line in log)
    assert any("응답 앞부분 “죄송합니다”" in line for line in log)
    assert log[-1].startswith("⏱ 총 ") and log[-1].endswith("초")


def test_partial_result_explains_why_it_stopped(app, monkeypatch):
    res = fake_result(requested=3)
    res["note"] = "나머지 레시피는 받지 못했어요. 오늘 쓸 수 있는 무료 호출 50회를 모두 사용했어요."
    monkeypatch.setattr(recipe, "generate_recipes", lambda *a, **k: res)
    app.run()
    button(app, "레시피 생성").click().run()
    assert any("나머지 레시피는 받지 못했어요" in w.value for w in app.warning)


def test_model_output_is_not_rendered_as_markdown_links(app, monkeypatch):
    evil = fake_recipe("[공짜 쿠폰](http://evil.example)", summary="**굵게** $수식$", steps=["[클릭](http://evil.example)"])
    monkeypatch.setattr(recipe, "generate_recipes", lambda *a, **k: fake_result(evil))
    app.run()
    button(app, "레시피 생성").click().run()
    assert not app.exception
    t = texts(app)
    assert "[공짜 쿠폰](http" not in t and r"\[공짜 쿠폰\]" in t
    assert r"\$수식\$" in t


def test_fallback_model_notice_and_excluded_list(app, monkeypatch):
    res = fake_result(fake_recipe(), model="nvidia/nemotron-3.5-super-vl:free", requested=3,
                      excluded=[("너트 샐러드", "알레르기(견과류)에 걸리는 재료가 있음")])
    monkeypatch.setattr(recipe, "generate_recipes", lambda *a, **k: res)
    app.run()
    button(app, "레시피 생성").click().run()
    assert any("대체 모델" in i.value for i in app.info)
    assert "요청한 3개 중 1개만" in texts(app)
    assert "너트 샐러드" in texts(app)


def test_zero_results_tell_the_user_to_relax_conditions(app, monkeypatch):
    monkeypatch.setattr(recipe, "generate_recipes", lambda *a, **k: {"recipes": [], "excluded": [("A", "시간")], "model": recipe.TEXT_MODEL, "requested": 3})
    app.run()
    button(app, "레시피 생성").click().run()
    assert any("조건에 맞는 레시피가 없어요" in w.value for w in app.warning)


def test_reconfirming_ingredients_clears_old_recipes(app):
    app.session_state["recipe_result"] = fake_result()
    app.session_state["rows"] = [{"id": 0, "name": "달걀", "quantity": "", "confidence": "high"}]
    app.session_state["next_id"] = 1
    app.run()
    button(app, "이 목록으로 확정").click().run()
    assert "recipe_result" not in app.session_state


def test_rate_limit_blocks_generation_without_calling_the_model(app, monkeypatch):
    called = []
    monkeypatch.setattr(recipe, "generate_recipes", lambda *a, **k: called.append(1) or fake_result())
    import time as _t
    app.session_state["calls"] = [_t.time()] * 5
    app.run()
    button(app, "레시피 생성").click().run()
    assert called == [] and any("너무 잦습니다" in e.value for e in app.error)
    app.run()   # 다른 조작으로 화면이 다시 그려져도 실패 표시가 남아 있어야 한다
    assert any("너무 잦습니다" in e.value for e in app.error)


# --- 실패는 정직하게 화면에 표시한다 ---
def png_bytes():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (120, 80), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


def recognize_click(at):
    at.file_uploader[0].upload("fridge.png", png_bytes(), "image/png")
    at.run()
    [b for b in at.button if b.label == "식재료 인식"][0].click().run()


@pytest.fixture
def photo_app():
    return AppTest.from_file(APP, default_timeout=30)


def test_recipe_failure_hides_the_previous_result_and_keeps_the_message_across_reruns(app, monkeypatch):
    app.session_state["recipe_result"] = fake_result(fake_recipe("옛 레시피"))
    monkeypatch.setattr(recipe, "generate_recipes", lambda *a, **k: (_ for _ in ()).throw(llm.RateLimitedError(llm.MSG_BUSY)))
    app.run()
    button(app, "다시 생성").click().run()
    assert "recipe_result" not in app.session_state and "옛 레시피" not in texts(app)
    app.run()
    app.run()                                                 # 화면이 여러 번 다시 그려져도
    assert any("레시피 생성에 실패했어요" in e.value for e in app.error)
    assert not any("옛 레시피" in str(m.value) for m in app.markdown)


def test_unexpected_error_during_recipes_is_reported_as_a_failure_not_a_crash(app, monkeypatch):
    def boom(*a, **k):
        raise KeyError("secret-detail-/home/user/path")

    monkeypatch.setattr(recipe, "generate_recipes", boom)
    app.run()
    button(app, "레시피 생성").click().run()
    assert not app.exception                                   # 앱이 거친 오류 화면으로 죽지 않는다
    shown = " ".join(e.value for e in app.error)
    assert "레시피 생성에 실패했어요" in shown and "예상하지 못한 오류" in shown and "KeyError" in shown
    assert "secret-detail" not in shown                       # 오류 내용(경로 등)은 화면에 내지 않는다
    assert any(line.startswith("❌") for line in app.session_state["recipe_log"])
    assert "recipe_result" not in app.session_state


def test_recognition_failure_message_stays_visible_after_other_interactions(photo_app, monkeypatch):
    def boom(*a, **k):
        raise llm.RateLimitedError(llm.MSG_BUSY)

    monkeypatch.setattr(recognizer, "recognize_detailed", boom)
    photo_app.session_state["confirmed"] = CONFIRMED          # 이전 결과가 있던 상태에서 실패해도
    photo_app.session_state["rows"] = [{"id": 0, "name": "달걀", "quantity": "", "confidence": "high"}]
    photo_app.session_state["next_id"] = 1
    photo_app.run()
    recognize_click(photo_app)
    assert not photo_app.exception
    assert any("사진 인식에 실패했어요" in e.value and "이전 인식 결과는 보여 주지 않습니다" in e.value for e in photo_app.error)
    assert "rows" not in photo_app.session_state and "confirmed" not in photo_app.session_state
    assert not [b for b in photo_app.button if b.label == "레시피 생성"]   # 이전 재료로 레시피를 만들 수 없다
    photo_app.toggle[0].set_value(True).run()                  # 다른 조작으로 다시 그려져도
    photo_app.toggle[0].set_value(False).run()
    assert any("사진 인식에 실패했어요" in e.value for e in photo_app.error)


def test_recognition_unexpected_error_is_reported(photo_app, monkeypatch):
    monkeypatch.setattr(recognizer, "recognize_detailed", lambda *a, **k: (_ for _ in ()).throw(ZeroDivisionError("x")))
    photo_app.run()
    recognize_click(photo_app)
    assert not photo_app.exception
    shown = " ".join(e.value for e in photo_app.error)
    assert "사진 인식에 실패했어요" in shown and "예상하지 못한 오류" in shown and "ZeroDivisionError" in shown
    assert "rows" not in photo_app.session_state


def test_recognition_rate_limit_is_a_visible_failure_that_stays(photo_app, monkeypatch):
    import time as _t
    called = []
    monkeypatch.setattr(recognizer, "recognize_detailed", lambda *a, **k: called.append(1))
    photo_app.session_state["calls"] = [_t.time()] * 5
    photo_app.run()
    recognize_click(photo_app)
    assert called == []
    photo_app.run()
    assert any("너무 잦습니다" in e.value for e in photo_app.error)


def test_a_new_successful_recognition_clears_the_old_failure_message(photo_app, monkeypatch):
    state = {"fail": True}

    def flaky(*a, on_event=None, **k):
        if state["fail"]:
            raise llm.RateLimitedError(llm.MSG_BUSY)
        return {"items": [{"name": "달걀", "quantity": "1개", "confidence": "high"}], "model": recognizer.VISION_MODEL,
                "original_bytes": 1000, "sent_bytes": 500}

    monkeypatch.setattr(recognizer, "recognize_detailed", flaky)
    photo_app.run()
    recognize_click(photo_app)
    assert any("사진 인식에 실패했어요" in e.value for e in photo_app.error)
    state["fail"] = False
    [b for b in photo_app.button if b.label == "식재료 인식"][0].click().run()
    assert not photo_app.error                                 # 성공하면 지난 실패 표시는 사라진다
    assert photo_app.session_state["rows"][0]["name"] == "달걀"



# --- 사용자 경험: 원인에 맞는 문구·이스케이프·진행 기록 ---
def test_retry_and_fallback_lines_name_the_right_cause(app, monkeypatch):
    def fake(ingredients, options, key, on_event=None, **kw):
        on_event({"kind": "retry", "model": "m/a", "status": 429, "wait": 2})
        on_event({"kind": "retry", "model": "m/a", "status": 503, "wait": 2})
        on_event({"kind": "fallback", "model": "m/a", "reason": "server", "next_model": "m/b"})
        return fake_result()

    monkeypatch.setattr(recipe, "generate_recipes", fake)
    app.run()
    button(app, "레시피 생성").click().run()
    log = app.session_state["recipe_log"]
    assert "⏳ HTTP 429 요청 제한 — 2초 뒤 다시 시도" in log
    assert "⏳ HTTP 503 서버 오류 — 2초 뒤 다시 시도" in log
    assert any("서버 오류가 계속됨" in line for line in log)


def test_excluded_titles_in_the_progress_log_are_escaped(app, monkeypatch):
    def fake(ingredients, options, key, on_event=None, **kw):
        on_event({"kind": "excluded", "title": "[쿠폰](http://evil.example)", "reason": "시간"})
        return fake_result()

    monkeypatch.setattr(recipe, "generate_recipes", fake)
    app.run()
    button(app, "레시피 생성").click().run()
    line = [x for x in app.session_state["recipe_log"] if x.startswith("🚫")][0]
    assert "[쿠폰](http" not in line and r"\[쿠폰\]" in line


def test_confirmed_ingredient_names_are_escaped():
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["confirmed"] = [{"name": "[클릭](http://evil.example)", "quantity": "", "confidence": "high"}]
    at.run()
    shown = " ".join(s.value for s in at.success)
    assert "확정됨" in shown and "[클릭](http" not in shown and r"\[클릭\]" in shown


def test_rate_limited_recognition_does_not_show_the_previous_progress_log(photo_app, monkeypatch):
    import time as _t
    monkeypatch.setattr(recognizer, "recognize_detailed", lambda *a, **k: None)
    photo_app.session_state["progress_log"] = ["✅ m/a 인식 완료 (3개)"]   # 직전에 성공한 기록
    photo_app.session_state["calls"] = [_t.time()] * 5
    photo_app.run()
    recognize_click(photo_app)
    assert any("너무 잦습니다" in e.value for e in photo_app.error)
    assert "progress_log" not in photo_app.session_state
    assert not [ex for ex in photo_app.expander if ex.label == "진행 기록"]


def test_recipe_card_does_not_invent_a_difficulty(app, monkeypatch):
    monkeypatch.setattr(recipe, "generate_recipes", lambda *a, **k: fake_result(fake_recipe(difficulty=None)))
    app.run()
    button(app, "레시피 생성").click().run()
    captions = " ".join(str(c.value) for c in app.caption)
    assert "한식 · ⏱ 20분 · 2인분" in captions and "중급" not in captions
