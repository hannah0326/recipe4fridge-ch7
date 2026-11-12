# 작성: 2026-10-24 23:03 KST
# 3단계 화면 흐름 테스트(가입·로그인·구글·로그인 유지·저장·내 레시피·프로필·계정 삭제). 저장소는 conftest 의 메모리 저장소다.
import datetime as dt
import types

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import account_ui
import auth
import repository
import services

import os

APP = os.path.join(os.path.dirname(__file__), "..", "src", "app.py")
PW = "pa55word*!"
CONFIRMED = [{"name": "달걀", "quantity": "6개", "confidence": "high"}, {"name": "양배추", "quantity": "", "confidence": "high"}]


def recipe_dict(title="달걀 볶음", names=("달걀", "양배추"), minutes=20, difficulty="easy"):
    return {"title": title, "summary": "간단한 요리", "cuisine": "한식", "servings": 2, "time_minutes": minutes,
            "difficulty": difficulty, "ingredients": [{"name": n, "amount": "1개", "have": True} for n in names],
            "missing": ["소금"], "steps": ["볶는다.", "담는다."]}


def result(*recipes):
    recipes = list(recipes) or [recipe_dict("달걀 볶음"), recipe_dict("양배추 전", names=("양배추", "달걀", "당근"))]
    return {"recipes": recipes, "excluded": [], "model": "nvidia/nemotron-3-ultra-550b-a55b:free", "requested": len(recipes), "note": ""}


@pytest.fixture(autouse=True)
def google_off(monkeypatch):
    monkeypatch.setattr(account_ui, "google_configured", lambda: False)
    monkeypatch.setattr(account_ui, "google_identity", lambda: None)


@pytest.fixture
def at():
    return AppTest.from_file(APP, default_timeout=60)


def button(app, label, key=None):
    found = [b for b in app.button if b.label == label and (key is None or b.key == key)]
    assert found, f"'{label}' 단추가 없음. 있는 단추: {[b.label for b in app.button]}"
    return found[0]


def texts(app):
    parts = [str(e.value) for e in (*app.markdown, *app.caption, *app.info, *app.success, *app.warning, *app.error)]
    return " ".join(parts)


def errors(app):
    return " ".join(e.value for e in app.error)


def go(app, menu):
    app.session_state["menu"] = menu
    return app.run()


def join(app, email="hannah@example.com", pw=PW, again=None, name=""):
    go(app, account_ui.MENU_LOGIN)
    app.text_input(key="join_email").set_value(email)
    app.text_input(key="join_name").set_value(name)
    app.text_input(key="join_password").set_value(pw)
    app.text_input(key="join_password2").set_value(pw if again is None else again)
    button(app, "가입하고 로그인").click().run()
    return app


def login_user(app, repo, email="hannah@example.com", provider="password"):
    """화면 없이 로그인한 상태로 만든다(저장소에 사용자를 만들고 화면 상태에 넣는다)."""
    user = repo.create_user(email, provider, "한나", password_hash=auth.hash_password(PW) if provider == "password" else None)
    app.session_state["user"] = user
    app.session_state["cookie_checked"] = True
    return user


def sidebar_html(app):
    return [e for e in app.sidebar.children.values() if getattr(e, "type", "") == "html"]


# --- 로그아웃 상태 ---------------------------------------------------------------------------
def test_logged_out_menu_offers_main_and_login_only(at):
    at.run()
    assert not at.exception
    assert at.sidebar.radio[0].options == [account_ui.MENU_MAIN, account_ui.MENU_LOGIN]
    assert "로그인하면 프로필과 저장한 레시피를 쓸 수 있어요" in texts(at.sidebar) or any("로그인하면" in c.value for c in at.sidebar.caption)


def test_login_page_has_login_and_join_forms(at):
    go(at, account_ui.MENU_LOGIN)
    assert [t.label for t in at.tabs] == ["로그인", "가입"]
    assert {b.label for b in at.button} >= {"로그인", "가입하고 로그인"}
    assert any("구글 로그인은 아직 설정되지 않아" in c.value for c in at.caption)


def test_google_button_appears_only_when_configured(at, monkeypatch):
    monkeypatch.setattr(account_ui, "google_configured", lambda: True)
    go(at, account_ui.MENU_LOGIN)
    assert "🔐 구글로 로그인" in [b.label for b in at.button]


# --- 가입 ------------------------------------------------------------------------------------
def test_join_creates_the_account_logs_in_and_returns_to_the_main_page(at, memory_repo):
    join(at, "Hannah@Example.com", name="한나")
    assert not at.exception
    user = at.session_state["user"]
    assert user["email"] == "hannah@example.com" and user["display_name"] == "한나"
    assert at.session_state["menu"] == account_ui.MENU_MAIN and at.title[0].value.startswith("🧊")
    assert memory_repo.users[user["id"]]["password_hash"].startswith("$argon2id$")
    assert at.sidebar.radio[0].options == [account_ui.MENU_MAIN, account_ui.MENU_MY, account_ui.MENU_PROFILE]
    assert any("hannah@example.com" in c.value for c in at.sidebar.caption)
    assert "로그아웃" in [b.label for b in at.sidebar.button]


def test_join_sets_the_stay_logged_in_cookie_with_only_a_hash_stored_on_the_server(at, memory_repo):
    join(at)
    token = at.session_state["session_token"]
    script = sidebar_html(at)
    assert len(script) == 1
    body = script[0].proto.body if hasattr(script[0], "proto") else str(script[0])
    assert f"fridge_session={token}" in body and "Max-Age=2592000" in body and "SameSite=Lax" in body
    assert auth.hash_token(token) in memory_repo.sessions and token not in memory_repo.sessions


@pytest.mark.parametrize("kwargs,part", [
    ({"email": "엉뚱"}, "이메일 형식"), ({"pw": "short"}, "8자 이상"), ({"again": "다른비밀번호*1"}, "확인이 달라요"),
    ({"pw": "password"}, "흔하거나")])
def test_join_shows_a_clear_error_and_does_not_log_in(at, kwargs, part):
    join(at, **kwargs)
    assert part in errors(at) and "user" not in at.session_state
    assert at.session_state["join_password"] == "" and at.session_state["join_password2"] == ""   # 비밀번호는 화면 상태에 남기지 않는다


def test_join_with_an_existing_email_is_refused(at, memory_repo):
    memory_repo.create_user("hannah@example.com", "password", "x", password_hash="h")
    join(at)
    assert "이미 가입" in errors(at) and "user" not in at.session_state


def test_join_with_a_google_email_points_to_google_login(at, memory_repo):
    memory_repo.create_user("hannah@example.com", "google", "x", google_sub="s")
    join(at)
    assert "구글 로그인으로 가입되어 있어요" in errors(at) and "user" not in at.session_state


# --- 로그인 ------------------------------------------------------------------------------------
def make_account(memory_repo, email="hannah@example.com", pw=PW):
    return auth.register(memory_repo, email, pw, "한나")


def do_login(app, email, pw):
    go(app, account_ui.MENU_LOGIN)
    app.text_input(key="login_email").set_value(email)
    app.text_input(key="login_password").set_value(pw)
    button(app, "로그인").click().run()


def test_login_succeeds_and_clears_the_password_field(at, memory_repo):
    make_account(memory_repo)
    do_login(at, "HANNAH@example.com", PW)
    assert at.session_state["user"]["email"] == "hannah@example.com" and at.session_state["login_password"] == ""
    assert "auth_error" not in at.session_state and at.session_state["menu"] == account_ui.MENU_MAIN


def test_wrong_password_and_unknown_email_show_the_same_error(at, memory_repo):
    make_account(memory_repo)
    do_login(at, "hannah@example.com", "wrong-password*1")
    wrong = errors(at)
    do_login(at, "nobody@example.com", PW)
    assert auth.GENERIC_LOGIN_ERROR in wrong and auth.GENERIC_LOGIN_ERROR in errors(at) and "user" not in at.session_state


def test_repeated_failures_lock_the_login(at, memory_repo):
    make_account(memory_repo)
    for _ in range(auth.MAX_FAILURES):
        do_login(at, "hannah@example.com", "wrong-password*1")
    do_login(at, "hannah@example.com", PW)                      # 맞는 비밀번호여도 잠겨 있다
    assert "연달아 실패" in errors(at) and "user" not in at.session_state


def test_login_page_while_logged_in_is_not_offered(at, memory_repo):
    join(at)
    assert account_ui.MENU_LOGIN not in at.sidebar.radio[0].options


# --- 구글 로그인 -----------------------------------------------------------------------------
def test_google_login_creates_a_google_account_without_a_cookie_session(at, monkeypatch, memory_repo):
    monkeypatch.setattr(account_ui, "google_identity",
                        lambda: {"email": "G@Example.com", "name": "구글 한나", "sub": "sub-1", "email_verified": True})
    at.run()
    user = at.session_state["user"]
    assert user["email"] == "g@example.com" and user["provider"] == "google" and "session_token" not in at.session_state
    assert memory_repo.users[user["id"]]["password_hash"] is None and sidebar_html(at) == []
    assert any("구글 로그인" in c.value for c in at.sidebar.caption)


def test_google_login_into_a_password_account_is_refused_and_google_is_logged_out(at, monkeypatch, memory_repo):
    make_account(memory_repo, "g@example.com")
    monkeypatch.setattr(account_ui, "google_identity",
                        lambda: {"email": "g@example.com", "name": "x", "sub": "s", "email_verified": True})
    logged_out = []
    monkeypatch.setattr(st, "logout", lambda: logged_out.append(True))
    at.run()
    assert "user" not in at.session_state and logged_out == [True]           # 같은 오류가 반복되지 않게 구글 쪽 로그인도 끊는다
    assert auth.PASSWORD_ACCOUNT in " ".join(i.value for i in at.info)
    assert memory_repo.users[1]["provider"] == "password"                    # 계정이 합쳐지지 않았다


def test_unverified_google_email_does_not_log_in(at, monkeypatch, memory_repo):
    monkeypatch.setattr(account_ui, "google_identity",
                        lambda: {"email": "g@example.com", "name": "x", "sub": "s", "email_verified": False})
    monkeypatch.setattr(st, "logout", lambda: None)
    at.run()
    assert "user" not in at.session_state and memory_repo.users == {}


def test_google_logout_also_logs_out_of_streamlit(at, monkeypatch, memory_repo):
    login_user(at, memory_repo, "g@example.com", provider="google")
    logged_out = []
    monkeypatch.setattr(st, "logout", lambda: logged_out.append(True))
    at.run()
    button(at, "로그아웃").click().run()
    assert logged_out == [True] and "user" not in at.session_state


# --- 새로고침 후 로그인 유지(쿠키) ----------------------------------------------------------
def with_cookie(monkeypatch, value):
    cookies = {} if value is None else {auth.COOKIE_NAME: value}
    monkeypatch.setattr(st, "context", types.SimpleNamespace(cookies=cookies))


def test_a_valid_cookie_logs_the_user_back_in_after_a_refresh(at, monkeypatch, memory_repo):
    user = make_account(memory_repo)
    token = auth.start_session(memory_repo, user["id"])
    with_cookie(monkeypatch, token)
    at.run()
    assert at.session_state["user"]["email"] == "hannah@example.com" and at.session_state["session_token"] == token
    assert at.sidebar.radio[0].options[1] == account_ui.MENU_MY


@pytest.mark.parametrize("cookie", [None, "엉뚱한 값", "A" * 43])
def test_missing_or_unknown_cookies_do_not_log_anyone_in(at, monkeypatch, memory_repo, cookie):
    make_account(memory_repo)
    with_cookie(monkeypatch, cookie)
    at.run()
    assert "user" not in at.session_state


def test_an_expired_cookie_session_does_not_log_in(at, monkeypatch):
    now = [dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc)]
    repo = repository.MemoryRepository(now=lambda: now[0])
    monkeypatch.setattr(services, "get_repository", lambda: (repo, None))
    user = auth.register(repo, "hannah@example.com", PW)
    token = auth.start_session(repo, user["id"], now=now[0])
    now[0] += dt.timedelta(days=31)
    with_cookie(monkeypatch, token)
    at.run()
    assert "user" not in at.session_state


# --- 로그아웃 --------------------------------------------------------------------------------
def test_logout_clears_the_screen_state_and_the_server_session_and_the_cookie(at, memory_repo):
    join(at)
    token = at.session_state["session_token"]
    at.session_state["rows"] = [{"id": 0, "name": "달걀", "quantity": "", "confidence": "high"}]
    at.session_state["confirmed"] = CONFIRMED
    at.session_state["recipe_result"] = result()
    at.session_state["opt_allergies"] = ["우유"]
    at.session_state["calls"] = [1.0, 2.0]
    at.run()
    button(at, "로그아웃").click().run()
    assert not at.exception
    for key in ("user", "session_token", "rows", "confirmed", "recipe_result", "opt_allergies"):
        assert key not in at.session_state, key
    assert at.session_state["calls"] == [1.0, 2.0]                  # 요청 제한 기록은 로그아웃으로 피하지 못한다
    assert auth.hash_token(token) not in memory_repo.sessions and auth.user_from_token(memory_repo, token) is None
    body = str(sidebar_html(at)[0].proto.body) if sidebar_html(at) else ""
    assert "Max-Age=0" in body and f"fridge_session={token}" not in body    # 쿠키를 지우는 스크립트
    assert at.sidebar.radio[0].options == [account_ui.MENU_MAIN, account_ui.MENU_LOGIN]


def test_the_next_person_does_not_inherit_the_previous_users_defaults(at, memory_repo):
    join(at, "a@example.com")
    memory_repo.update_profile(at.session_state["user"]["id"], "A", 5, ["비건"], ["우유"])
    at.session_state["user"] = memory_repo.get_user(at.session_state["user"]["id"])
    at.session_state["confirmed"] = CONFIRMED
    at.run()
    assert at.session_state["opt_servings"] == 5 and at.session_state["opt_allergies"] == ["우유"]
    button(at, "로그아웃").click().run()
    join(at, "b@example.com")
    at.session_state["confirmed"] = CONFIRMED
    at.run()
    assert at.session_state["opt_servings"] == 2 and at.session_state["opt_allergies"] == []


# --- 레시피 저장 -----------------------------------------------------------------------------
def prepare_results(app):
    app.session_state["confirmed"] = CONFIRMED
    app.session_state["recipe_result"] = result()
    return app.run()


def test_save_buttons_for_a_logged_in_user_store_the_recipe_with_the_pantry(at, memory_repo):
    user = login_user(at, memory_repo)
    prepare_results(at)
    assert [b.label for b in at.button if b.label.startswith("💾")] == ["💾 저장", "💾 저장"]
    button(at, "💾 저장", key="save_0").click().run()
    saved = memory_repo.list_recipes(user["id"])
    assert [r["title"] for r in saved] == ["달걀 볶음"] and saved[0]["pantry"] == ["달걀", "양배추"]
    assert any("저장했어요" in s.value for s in at.success)
    assert [b.key for b in at.button if b.label == "💾 저장"] == ["save_1"]     # 저장한 카드는 단추가 사라지고 표시로 바뀐다
    assert any("저장됨" in c.value for c in at.caption)


def test_saving_the_same_recipe_again_says_it_is_already_saved(at, memory_repo):
    user = login_user(at, memory_repo)
    memory_repo.save_recipe(user["id"], recipe_dict("달걀 볶음"), [])
    prepare_results(at)
    assert [b.key for b in at.button if b.label == "💾 저장"] == ["save_1"]
    at.session_state["recipe_result"] = result(recipe_dict("달걀 볶음"))
    at.run()
    assert not [b for b in at.button if b.label == "💾 저장"]               # 이미 저장한 것은 단추가 없다


def count_calls(monkeypatch, memory_repo, name):
    calls = []
    original = getattr(memory_repo, name)
    monkeypatch.setattr(memory_repo, name, lambda *a, **k: calls.append(a) or original(*a, **k))
    return calls


def test_a_recipe_deleted_on_my_recipes_can_be_saved_again_on_the_main_page(at, memory_repo):
    user = login_user(at, memory_repo)
    prepare_results(at)
    button(at, "💾 저장", key="save_0").click().run()
    assert [b.key for b in at.button if b.label == "💾 저장"] == ["save_1"]
    rid = memory_repo.list_recipes(user["id"])[0]["id"]
    go(at, account_ui.MENU_MY)
    button(at, "삭제 확인", key=f"mr_del_{rid}").click().run()
    go(at, account_ui.MENU_MAIN)
    assert [b.key for b in at.button if b.label == "💾 저장"] == ["save_0", "save_1"]   # 바로 '저장됨'이 풀린다


def test_logged_out_save_asks_to_log_in_and_saves_right_after_logging_in(at, memory_repo):
    prepare_results(at)
    button(at, "💾 저장 (로그인 필요)", key="save_0").click().run()
    assert at.session_state["menu"] == account_ui.MENU_LOGIN and at.session_state["pending_save"] == 0
    assert any("먼저 로그인해 주세요" in i.value for i in at.info)
    assert at.session_state["recipe_result"]["recipes"][0]["title"] == "달걀 볶음"      # 레시피는 그대로 남아 있다
    at.text_input(key="join_email").set_value("hannah@example.com")
    at.text_input(key="join_password").set_value(PW)
    at.text_input(key="join_password2").set_value(PW)
    button(at, "가입하고 로그인").click().run()
    assert at.session_state["menu"] == account_ui.MENU_MAIN and "pending_save" not in at.session_state
    user_id = at.session_state["user"]["id"]
    assert [r["title"] for r in memory_repo.list_recipes(user_id)] == ["달걀 볶음"]        # 이어서 저장됐다
    assert any("저장했어요" in s.value for s in at.success)


def test_saving_when_the_storage_is_down_says_so(at, memory_repo, monkeypatch):
    login_user(at, memory_repo)
    prepare_results(at)
    memory_repo.fail = repository.RepositoryError("저장소가 응답하지 않아요.")
    monkeypatch.setattr(services, "get_repository", lambda: (None, "저장소(DB)에 연결하지 못했어요."))
    at.run()
    assert not [b for b in at.button if b.label == "💾 저장"]
    assert any("저장소에 연결할 수 없어" in c.value for c in at.caption)


def test_the_save_limit_message_is_shown(at, memory_repo, monkeypatch):
    user = login_user(at, memory_repo)
    monkeypatch.setattr(repository, "MAX_RECIPES_PER_USER", 1)
    memory_repo.save_recipe(user["id"], recipe_dict("다른 요리", names=("당근",)), [])
    prepare_results(at)
    button(at, "💾 저장", key="save_0").click().run()
    assert "1개예요" in errors(at) and memory_repo.count_recipes(user["id"]) == 1


# --- 내 레시피 -------------------------------------------------------------------------------
def seed(memory_repo, user, *titles):
    for i, t in enumerate(titles):
        memory_repo.save_recipe(user["id"], recipe_dict(t, names=(f"재료{i}", "달걀")), [f"재료{i}"])


def test_my_recipes_lists_only_my_saved_recipes_newest_first(at, memory_repo):
    me = login_user(at, memory_repo)
    other = memory_repo.create_user("other@example.com", "password", "다른 사람", password_hash="h")
    seed(memory_repo, me, "첫째", "둘째")
    seed(memory_repo, other, "남의 레시피")
    go(at, account_ui.MENU_MY)
    assert not at.exception
    shown = " ".join(m.value for m in at.markdown)
    assert "둘째" in shown and "첫째" in shown and "남의 레시피" not in shown
    assert shown.index("둘째") < shown.index("첫째")
    assert any("저장한 레시피 2개" in c.value for c in at.caption)


def test_my_recipes_search_and_filters(at, memory_repo):
    me = login_user(at, memory_repo)
    memory_repo.save_recipe(me["id"], recipe_dict("달걀 볶음", names=("달걀",), minutes=15), [])
    memory_repo.save_recipe(me["id"], recipe_dict("양배추 전", names=("양배추",), minutes=40, difficulty="hard"), [])
    go(at, account_ui.MENU_MY)
    at.text_input(key="mr_query").set_value("양배추").run()
    titles = " ".join(m.value for m in at.markdown)
    assert "양배추 전" in titles and "달걀 볶음" not in titles
    at.text_input(key="mr_query").set_value("").run()
    at.selectbox(key="mr_time").set_value("15분 이내").run()
    assert "달걀 볶음" in " ".join(m.value for m in at.markdown) and "양배추 전" not in " ".join(m.value for m in at.markdown)
    at.selectbox(key="mr_time").set_value("제한 없음").run()
    at.selectbox(key="mr_diff").set_value("hard").run()
    assert "양배추 전" in " ".join(m.value for m in at.markdown) and "달걀 볶음" not in " ".join(m.value for m in at.markdown)
    at.selectbox(key="mr_diff").set_value(None).run()
    at.text_input(key="mr_query").set_value("없는 요리").run()
    assert any("조건에 맞는 저장한 레시피가 없어요" in w.value for w in at.warning)


def test_my_recipes_reads_the_storage_once_per_screen(at, memory_repo, monkeypatch):
    me = login_user(at, memory_repo)
    seed(memory_repo, me, "달걀 볶음", "양배추 전", "두부 조림")
    listed = count_calls(monkeypatch, memory_repo, "list_recipes")
    counted = count_calls(monkeypatch, memory_repo, "count_recipes")
    go(at, account_ui.MENU_MY)
    at.text_input(key="mr_query").set_value("양배추").run()
    assert len(listed) == 2 and counted == []                           # 화면마다 한 번, 개수는 따로 묻지 않는다
    assert all(c == (me["id"],) for c in listed)                        # 내 레시피만 묻는다
    assert any("저장한 레시피 3개" in c.value for c in at.caption)        # 검색해도 전체 개수는 그대로다
    titles = " ".join(m.value for m in at.markdown)
    assert "양배추 전" in titles and "달걀 볶음" not in titles


def test_my_recipes_empty_state(at, memory_repo):
    login_user(at, memory_repo)
    go(at, account_ui.MENU_MY)
    assert any("아직 저장한 레시피가 없어요" in i.value for i in at.info)


def test_memo_is_saved_and_survives_a_new_visit(at, memory_repo):
    me = login_user(at, memory_repo)
    seed(memory_repo, me, "메모 시험")
    rid = memory_repo.list_recipes(me["id"])[0]["id"]
    go(at, account_ui.MENU_MY)
    at.text_area(key=f"memo_{rid}").set_value("소금을 줄이자").run()
    button(at, "메모 저장", key=f"memo_save_{rid}").click().run()
    assert memory_repo.list_recipes(me["id"])[0]["memo"] == "소금을 줄이자"
    go(at, account_ui.MENU_MAIN)
    go(at, account_ui.MENU_MY)
    assert at.text_area(key=f"memo_{rid}").value == "소금을 줄이자"


def test_delete_a_saved_recipe(at, memory_repo):
    me = login_user(at, memory_repo)
    seed(memory_repo, me, "지울 레시피", "남길 레시피")
    rid = next(r["id"] for r in memory_repo.list_recipes(me["id"]) if r["title"] == "지울 레시피")
    go(at, account_ui.MENU_MY)
    button(at, "삭제 확인", key=f"mr_del_{rid}").click().run()
    assert [r["title"] for r in memory_repo.list_recipes(me["id"])] == ["남길 레시피"]
    assert "지울 레시피" not in " ".join(m.value for m in at.markdown)


def test_my_recipes_shows_a_clear_error_when_storage_fails(at, memory_repo):
    login_user(at, memory_repo)
    memory_repo.fail = account_ui.db.StorageError("저장소(DB)에 연결하지 못했어요.")
    go(at, account_ui.MENU_MY)
    assert not at.exception and "불러오지 못했어요" in errors(at)


# --- 프로필 ----------------------------------------------------------------------------------
def test_profile_is_saved_and_becomes_the_default_for_recipe_conditions(at, memory_repo):
    me = login_user(at, memory_repo)
    go(at, account_ui.MENU_PROFILE)
    at.text_input(key="profile_name").set_value("새 이름")
    at.number_input(key="profile_servings").set_value(4)
    at.multiselect(key="profile_allergies").set_value(["우유", "복숭아"])
    at.multiselect(key="profile_diet").set_value(["채식"])
    button(at, "프로필 저장").click().run()
    assert not at.exception and any("프로필을 저장했어요" in s.value for s in at.success)
    saved = memory_repo.get_user(me["id"])
    assert (saved["display_name"], saved["default_servings"], saved["allergies"], saved["diet_restrictions"]) == \
        ("새 이름", 4, ["우유", "복숭아"], ["채식"])
    assert at.session_state["user"]["default_servings"] == 4
    at.session_state["confirmed"] = CONFIRMED
    go(at, account_ui.MENU_MAIN)
    assert at.number_input(key="opt_servings").value == 4
    assert at.multiselect(key="opt_allergies").value == ["우유", "복숭아"] and at.multiselect(key="opt_diet").value == ["채식"]


def test_profile_defaults_do_not_override_a_choice_the_user_already_made(at, memory_repo):
    me = login_user(at, memory_repo)
    memory_repo.update_profile(me["id"], "한나", 4, [], ["우유"])
    at.session_state["user"] = memory_repo.get_user(me["id"])
    at.session_state["confirmed"] = CONFIRMED
    at.session_state["opt_servings"] = 3
    at.run()
    assert at.number_input(key="opt_servings").value == 3 and at.multiselect(key="opt_allergies").value == ["우유"]


def test_changing_conditions_on_the_main_page_does_not_change_the_profile(at, memory_repo):
    me = login_user(at, memory_repo)
    memory_repo.update_profile(me["id"], "한나", 4, [], ["우유"])
    at.session_state["user"] = memory_repo.get_user(me["id"])
    at.session_state["confirmed"] = CONFIRMED
    at.run()
    at.number_input(key="opt_servings").set_value(2).run()
    assert memory_repo.get_user(me["id"])["default_servings"] == 4


def test_profile_save_failure_is_reported(at, memory_repo):
    login_user(at, memory_repo)
    go(at, account_ui.MENU_PROFILE)
    memory_repo.fail = account_ui.db.StorageError("저장소(DB)에 연결하지 못했어요.")
    button(at, "프로필 저장").click().run()
    assert "저장하지 못했어요" in errors(at)


# --- 계정 삭제 -------------------------------------------------------------------------------
def test_account_deletion_needs_the_email_and_the_password(at, memory_repo):
    join(at)
    me = at.session_state["user"]
    seed(memory_repo, me, "내 레시피")
    go(at, account_ui.MENU_PROFILE)
    at.text_input(key="delete_email").set_value("other@example.com")
    at.text_input(key="delete_password").set_value(PW)
    button(at, "계정 삭제").click().run()
    assert "이메일이 계정의 이메일과 달라요" in errors(at) and memory_repo.get_user(me["id"]) is not None
    at.text_input(key="delete_email").set_value("hannah@example.com")
    at.text_input(key="delete_password").set_value("wrong-password*1")
    button(at, "계정 삭제").click().run()
    assert "비밀번호가 올바르지 않아요" in errors(at) and memory_repo.get_user(me["id"]) is not None


def test_account_deletion_removes_everything_and_logs_out(at, memory_repo):
    join(at)
    me = at.session_state["user"]
    seed(memory_repo, me, "내 레시피")
    go(at, account_ui.MENU_PROFILE)
    at.text_input(key="delete_email").set_value(" Hannah@Example.com ")
    at.text_input(key="delete_password").set_value(PW)
    button(at, "계정 삭제").click().run()
    assert not at.exception
    assert memory_repo.get_user(me["id"]) is None and memory_repo.recipes == {} and memory_repo.sessions == {}
    assert "user" not in at.session_state and at.session_state["menu"] == account_ui.MENU_LOGIN
    assert any("모두 삭제했어요" in i.value for i in at.info)
    assert "delete_password" not in at.session_state                  # 비밀번호가 화면 상태에 남지 않는다


# --- 저장소 장애 -----------------------------------------------------------------------------
def test_main_page_keeps_working_when_the_storage_is_down(at, monkeypatch):
    monkeypatch.setattr(services, "get_repository", lambda: (None, "저장소(DB)에 연결하지 못했어요. 잠시 후 다시 시도해 주세요."))
    at.session_state["confirmed"] = CONFIRMED
    at.run()
    assert not at.exception
    assert "레시피 생성" in [b.label for b in at.button]                      # 1·2단계는 그대로 쓸 수 있다
    assert any("연결하지 못했어요" in w.value for w in at.sidebar.warning)
    go(at, account_ui.MENU_LOGIN)
    assert not at.exception and "연결하지 못했어요" in errors(at)


def test_not_configured_storage_is_explained(at, monkeypatch):
    monkeypatch.setattr(services, "get_repository", lambda: (None, services.NOT_CONFIGURED))
    go(at, account_ui.MENU_LOGIN)
    assert "저장소가 설정되지 않아" in errors(at)


def test_login_attempt_while_the_storage_is_down_reports_it(at, monkeypatch, memory_repo):
    make_account(memory_repo)
    go(at, account_ui.MENU_LOGIN)
    at.text_input(key="login_email").set_value("hannah@example.com")
    at.text_input(key="login_password").set_value(PW)
    monkeypatch.setattr(services, "get_repository", lambda: (None, "저장소(DB)에 연결하지 못했어요."))
    button(at, "로그인").click().run()
    assert "연결하지 못했어요" in errors(at) and "user" not in at.session_state


def test_an_unexpected_error_during_login_is_reported_not_crashed(at, memory_repo, monkeypatch):
    make_account(memory_repo)
    monkeypatch.setattr(auth, "login", lambda *a, **k: (_ for _ in ()).throw(ZeroDivisionError("secret-detail")))
    do_login(at, "hannah@example.com", PW)
    assert not at.exception and "예상하지 못한 오류" in errors(at) and "ZeroDivisionError" in errors(at)
    assert "secret-detail" not in errors(at)


# --- 메뉴를 오가도 편집 중인 재료가 유지된다 ---------------------------------------------------
def test_edited_ingredients_survive_switching_menus(at):
    at.session_state["rows"] = [{"id": 0, "name": "달걀", "quantity": "6개", "confidence": "high"},
                                {"id": 1, "name": "우유", "quantity": "", "confidence": "low"}]
    at.session_state["next_id"] = 2
    at.run()
    at.text_input(key="name_0").set_value("유정란").run()
    at.text_input(key="qty_1").set_value("1팩").run()
    go(at, account_ui.MENU_LOGIN)                                        # 다른 메뉴로 갔다가
    go(at, account_ui.MENU_MAIN)                                         # 돌아온다
    assert at.text_input(key="name_0").value == "유정란" and at.text_input(key="qty_1").value == "1팩"
    assert at.text_input(key="name_1").value == "우유"
