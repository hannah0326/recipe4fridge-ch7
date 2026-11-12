# 작성: 2026-10-24 22:57 KST
# 3단계 화면: 로그인·가입(아이디=이메일+비밀번호, 구글), 프로필, 레시피 저장, 내 레시피.
#
# 로그인·저장·삭제처럼 상태를 바꾸는 동작은 버튼의 on_click 콜백으로 처리한다. 콜백은 화면을 다시 그리기 전에 실행되므로
# 메뉴 이동 같은 위젯 상태를 안전하게 바꿀 수 있다. 실패는 숨기지 않고 화면에 남긴다.
import datetime as dt
import logging

import streamlit as st

import auth
import db
import llm
import recipe
import repository
import services
from ui_common import (DIFFICULTY_LABEL, TIME_CHOICES, md_safe, recipe_caption, render_recipe_body,
                       render_recipe_summary, unexpected_message)

logger = logging.getLogger("fridge_app")
KST = dt.timezone(dt.timedelta(hours=9))

MENU_MAIN = "🧊 냉장고 레시피"
MENU_MY = "📖 내 레시피"
MENU_PROFILE = "👤 프로필"
MENU_LOGIN = "🔑 로그인·가입"

# 로그아웃·계정 삭제 때 비울 화면 상태. 이전 사용자의 재료·레시피·조건이 다음 사람에게 남지 않게 한다.
# ("calls"(요청 제한 기록)는 로그아웃으로 제한을 피하지 못하게 남겨 둔다)
CLEAR_KEYS = ("rows", "next_id", "confirmed", "run_info", "progress_log", "recognize_error", "pending_save",
              "save_notice", "auth_error", "auth_notice", "profile_notice", "user", "session_token")
CLEAR_PREFIXES = ("name_", "qty_", "del_", "opt_", "memo_", "mr_", "recipe_", "save_", "profile_", "delete_")
GOOGLE_KEYS = ("client_id", "client_secret", "redirect_uri", "cookie_secret", "server_metadata_url")
BASE_DIETS = ["채식", "비건", "페스코"]


def kst(when):
    return when.astimezone(KST).strftime("%Y-%m-%d %H:%M") if when else "-"


def clear_screen_state():
    for key in list(st.session_state.keys()):
        if key in CLEAR_KEYS or str(key).startswith(CLEAR_PREFIXES):
            del st.session_state[key]


# --- 구글 ----------------------------------------------------------------------------------
def google_configured():
    """구글 로그인에 필요한 설정(.streamlit/secrets.toml 의 [auth])이 모두 있는지."""
    try:
        cfg = st.secrets.get("auth")
        return bool(cfg) and all(cfg.get(k) for k in GOOGLE_KEYS)
    except Exception:  # noqa: BLE001 - secrets 파일이 없거나 형식이 틀리면 "설정 없음"으로 본다
        return False


def google_identity():
    """구글로 로그인한 상태면 {email, name, sub, email_verified}, 아니면 None."""
    try:
        if not st.user.is_logged_in:
            return None
        return {"email": st.user.get("email"), "name": st.user.get("name"), "sub": st.user.get("sub"),
                "email_verified": st.user.get("email_verified")}
    except Exception:  # noqa: BLE001 - 구글 설정이 없으면 로그인하지 않은 것으로 본다
        return None


def start_google_login():
    try:
        st.login()
    except Exception as e:  # noqa: BLE001
        logger.error("구글 로그인 시작 실패: %s", type(e).__name__)
        st.session_state["auth_error"] = "구글 로그인을 시작하지 못했어요. 설정을 확인해 주세요."


# --- 현재 사용자 ---------------------------------------------------------------------------
def _begin(repo, user):
    """로그인에 성공했을 때: 로그인 유지용 세션을 만들고 화면 상태를 정리한다."""
    token = auth.start_session(repo, user["id"])
    st.session_state.update(user=user, session_token=token, cookie_checked=True, menu=MENU_MAIN)
    st.session_state.pop("auth_error", None)
    st.session_state.pop("auth_notice", None)


def current_user(repo):
    """이번 화면의 로그인 사용자. 구글 로그인 → 이미 확인한 세션 → 새로고침 후 쿠키 순서로 찾는다."""
    user = st.session_state.get("user")
    if user or repo is None:
        return user
    identity = google_identity()
    if identity:
        try:
            user = auth.login_with_google(repo, identity)
        except auth.AuthError as e:
            st.session_state["auth_notice"] = str(e)
            st.session_state["menu"] = MENU_LOGIN
            st.logout()  # 구글 쪽 로그인 상태를 지워서 같은 오류가 반복되지 않게 한다
            return None
        except db.StorageError:
            return None
        st.session_state.update(user=user, cookie_checked=True)
        return user
    if not st.session_state.get("cookie_checked"):
        st.session_state["cookie_checked"] = True
        token = st.context.cookies.get(auth.COOKIE_NAME)
        try:
            user = auth.user_from_token(repo, token)
        except db.StorageError:
            user = None
        if user:
            st.session_state.update(user=user, session_token=token)
    return st.session_state.get("user")


def render_cookie_script():
    """로그인 유지 쿠키를 브라우저에 심거나(로그인 중) 지운다(방금 로그아웃). 토큰은 무작위 URL 안전 글자라 그대로 넣어도 된다."""
    token = st.session_state.get("session_token")
    clear = st.session_state.pop("cookie_clear", False)
    if not token and not clear:
        return
    if token:
        cookie = f'"{auth.COOKIE_NAME}={token}; Max-Age={auth.SESSION_DAYS * 86400}; Path=/; SameSite=Lax" + secure'
    else:
        cookie = f'"{auth.COOKIE_NAME}=; Max-Age=0; Path=/; SameSite=Lax" + secure'
    st.html(
        "<script>(function(){var secure = location.protocol === 'https:' ? '; Secure' : '';"
        f"document.cookie = {cookie};}})();</script>",
        unsafe_allow_javascript=True,
    )


def render_sidebar(user, repo_error):
    """메뉴와 로그인 상태를 사이드바에 그리고, 고른 메뉴를 돌려준다."""
    with st.sidebar:
        st.markdown("#### 🧊 냉장고 레시피")
        options = [MENU_MAIN, MENU_MY, MENU_PROFILE] if user else [MENU_MAIN, MENU_LOGIN]
        if st.session_state.get("menu") not in options:
            st.session_state["menu"] = options[0]
        menu = st.radio("메뉴", options, key="menu", label_visibility="collapsed")
        st.divider()
        if user:
            st.markdown(f"**{md_safe(user['display_name'] or user['email'])}**")
            st.caption(f"{md_safe(user['email'])} · {'구글' if user['provider'] == 'google' else '아이디·비밀번호'} 로그인")
            st.button("로그아웃", on_click=do_logout, key="logout_btn")
        else:
            st.caption("로그인하면 프로필과 저장한 레시피를 쓸 수 있어요.")
        if repo_error:
            st.warning(f"⚠️ {repo_error}")
        render_cookie_script()
    return menu


def render_page(menu, repo, repo_error, user):
    """냉장고 레시피(메인) 말고 다른 메뉴의 화면을 그린다."""
    if menu == MENU_MY and user:
        my_recipes_page(repo, repo_error, user)
    elif menu == MENU_PROFILE and user:
        profile_page(repo, repo_error, user)
    else:
        login_page(repo, repo_error)


# --- 로그인·가입·로그아웃 (콜백) ------------------------------------------------------------
def _fail(message):
    st.session_state["auth_error"] = message


def do_login():
    ss = st.session_state
    email, password = ss.get("login_email", ""), ss.get("login_password", "")
    ss["login_password"] = ""  # 비밀번호는 화면 상태에 남기지 않는다
    ss.pop("auth_error", None)
    repo, repo_error = services.get_repository()
    if repo is None:
        return _fail(repo_error)
    try:
        _begin(repo, auth.login(repo, email, password, services.login_limiter()))
    except llm.ServiceError as e:
        _fail(str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("로그인 중 예상하지 못한 오류")
        _fail(unexpected_message(e))


def do_join():
    ss = st.session_state
    email, name = ss.get("join_email", ""), ss.get("join_name", "")
    password, again = ss.get("join_password", ""), ss.get("join_password2", "")
    ss["join_password"] = ss["join_password2"] = ""
    ss.pop("auth_error", None)
    repo, repo_error = services.get_repository()
    if repo is None:
        return _fail(repo_error)
    if password != again:
        return _fail("비밀번호와 비밀번호 확인이 달라요.")
    try:
        _begin(repo, auth.register(repo, email, password, name))
    except llm.ServiceError as e:
        _fail(str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("가입 중 예상하지 못한 오류")
        _fail(unexpected_message(e))


def _leave(notice=None):
    """로그아웃·계정 삭제 뒤 화면 상태를 비운다. 로그인 유지 쿠키도 지운다."""
    clear_screen_state()
    st.session_state.update(cookie_clear=True, cookie_checked=True, menu=MENU_LOGIN if notice else MENU_MAIN)
    if notice:
        st.session_state["auth_notice"] = notice


def do_logout():
    user, token = st.session_state.get("user"), st.session_state.get("session_token")
    repo, _ = services.get_repository()
    if repo is not None and token:
        try:
            auth.end_session(repo, token)
        except Exception:  # noqa: BLE001 - 서버 쪽 세션을 못 지워도 로그아웃 자체는 계속한다
            logger.exception("로그아웃 중 세션 삭제 실패")
    _leave()
    if user and user.get("provider") == "google":
        st.logout()


def login_page(repo, repo_error):
    st.title("🔑 로그인 · 가입")
    if st.session_state.get("auth_notice"):
        st.info(st.session_state["auth_notice"])
    if repo is None:
        st.error(f"❌ {repo_error}")
        return
    tab_in, tab_join = st.tabs(["로그인", "가입"])
    with tab_in:
        with st.form("login_form", border=False):
            st.text_input("이메일(아이디)", key="login_email", autocomplete="username")
            st.text_input("비밀번호", type="password", key="login_password", autocomplete="current-password")
            st.form_submit_button("로그인", type="primary", on_click=do_login)
    with tab_join:
        with st.form("join_form", border=False):
            st.text_input("이메일(아이디)", key="join_email", autocomplete="username")
            st.text_input("표시 이름 (선택)", key="join_name", max_chars=repository.MAX_NAME_LEN)
            st.text_input(f"비밀번호 ({auth.MIN_PASSWORD}자 이상)", type="password", key="join_password", autocomplete="new-password")
            st.text_input("비밀번호 확인", type="password", key="join_password2", autocomplete="new-password")
            st.form_submit_button("가입하고 로그인", type="primary", on_click=do_join)
    if st.session_state.get("auth_error"):
        st.error(f"❌ {st.session_state['auth_error']}")
    st.divider()
    if google_configured():
        st.button("🔐 구글로 로그인", on_click=start_google_login, key="google_btn")
        st.caption("구글 계정으로 처음 로그인하면 계정이 자동으로 만들어져요.")
    else:
        st.caption("구글 로그인은 아직 설정되지 않아 쓸 수 없어요.")


# --- 레시피 저장 -------------------------------------------------------------------------
def _notice(level, message):
    st.session_state["save_notice"] = (level, message)


def save_recipe_now(index):
    """결과의 index 번째 레시피를 로그인한 사용자 계정에 저장한다."""
    ss = st.session_state
    recipes = (ss.get("recipe_result") or {}).get("recipes", [])
    user = ss.get("user")
    if user is None or not 0 <= index < len(recipes):
        return _notice("error", "저장할 레시피를 찾지 못했어요. 레시피를 다시 만들어 주세요.")
    repo, repo_error = services.get_repository()
    if repo is None:
        return _notice("error", f"저장하지 못했어요. {repo_error}")
    r = recipes[index]
    pantry = [i["name"] for i in ss.get("confirmed", [])]
    try:
        repo.save_recipe(user["id"], r, pantry)
    except repository.DuplicateRecipeError:
        return _notice("info", f"'{r['title']}'은(는) 이미 저장했어요.")
    except (repository.RecipeLimitError, db.StorageError) as e:
        return _notice("error", f"저장하지 못했어요. {e}")
    except Exception as e:  # noqa: BLE001
        logger.exception("레시피 저장 중 예상하지 못한 오류")
        return _notice("error", f"저장하지 못했어요. {unexpected_message(e)}")
    _notice("success", f"'{r['title']}'을(를) 저장했어요. '내 레시피'에서 볼 수 있어요.")


def request_login_to_save(index):
    st.session_state.update(pending_save=index, menu=MENU_LOGIN,
                            auth_notice="레시피를 저장하려면 먼저 로그인해 주세요. 로그인하면 바로 저장해 드려요.")


def apply_pending_save(user):
    """로그인하러 갔다 돌아왔으면, 저장하려던 레시피를 이어서 저장한다."""
    index = st.session_state.pop("pending_save", None)
    if index is not None and user:
        save_recipe_now(index)


def saved_fingerprints(repo, user):
    # 화면을 다시 그릴 때마다 DB 에 묻는다. 세션에 기억해 두면 다른 탭·기기에서 바꾼 저장 상태가 늦게 반영되어
    # 실제와 다른 '저장됨'이 보이므로, 질의 1회를 아끼는 것보다 정확한 표시를 택했다.
    if repo is None or user is None:
        return set()
    try:
        return repo.recipe_fingerprints(user["id"])
    except db.StorageError:
        return set()


def show_save_notice():
    level, message = st.session_state.get("save_notice") or (None, None)
    if level:
        {"success": st.success, "info": st.info, "error": st.error}[level](md_safe(message) if level != "error" else message)


def save_controls(index, r, user, repo, saved):
    """레시피 카드 아래의 저장 단추(또는 저장됨 표시)."""
    if user is None:
        st.button("💾 저장 (로그인 필요)", key=f"save_{index}", on_click=request_login_to_save, args=(index,))
    elif repo is None:
        st.caption("저장소에 연결할 수 없어 지금은 저장할 수 없어요.")
    elif repository.recipe_fingerprint(r) in saved:
        st.caption("✅ 저장됨 · '내 레시피'에서 볼 수 있어요")
    else:
        st.button("💾 저장", key=f"save_{index}", on_click=save_recipe_now, args=(index,))


def apply_profile_defaults(user):
    """로그인했으면 프로필 값을 레시피 조건의 기본값으로 채운다(이미 사용자가 고른 값은 건드리지 않는다)."""
    if not user:
        return
    ss = st.session_state
    if "opt_servings" not in ss:
        ss["opt_servings"] = user["default_servings"]
    if "opt_allergies" not in ss:
        ss["opt_allergies"] = list(user["allergies"])
    if "opt_diet" not in ss:
        ss["opt_diet"] = list(user["diet_restrictions"])


# --- 내 레시피 ---------------------------------------------------------------------------
def save_memo(recipe_id):
    user, (repo, _) = st.session_state.get("user"), services.get_repository()
    if user is None or repo is None:
        return st.toast("저장소에 연결할 수 없어요.", icon="⚠️")
    try:
        ok = repo.update_memo(user["id"], recipe_id, st.session_state.get(f"memo_{recipe_id}", ""))
    except db.StorageError as e:
        return st.toast(str(e), icon="⚠️")
    st.toast("메모를 저장했어요." if ok else "레시피를 찾지 못했어요.", icon="✅" if ok else "⚠️")


def delete_saved(recipe_id):
    user, (repo, _) = st.session_state.get("user"), services.get_repository()
    if user is None or repo is None:
        return st.toast("저장소에 연결할 수 없어요.", icon="⚠️")
    try:
        ok = repo.delete_recipe(user["id"], recipe_id)
    except db.StorageError as e:
        return st.toast(str(e), icon="⚠️")
    st.session_state.pop(f"memo_{recipe_id}", None)
    st.toast("레시피를 지웠어요." if ok else "이미 지워졌거나 찾을 수 없어요.", icon="🗑️" if ok else "⚠️")


def my_recipes_page(repo, repo_error, user):
    st.title("📖 내 레시피")
    if repo is None:
        st.error(f"❌ {repo_error}")
        return
    c1, c2, c3 = st.columns([3, 2, 2], vertical_alignment="bottom")
    query = c1.text_input("제목 검색", key="mr_query", placeholder="예: 달걀")
    time_name = c2.selectbox("조리 시간", [c[0] for c in TIME_CHOICES], key="mr_time")
    difficulty = c3.selectbox("난이도", [None, *recipe.DIFFICULTIES], key="mr_diff",
                              format_func=lambda v: "전체" if v is None else DIFFICULTY_LABEL[v])
    _, min_minutes, max_minutes = next(c for c in TIME_CHOICES if c[0] == time_name)
    try:
        # 위젯을 만질 때마다 다시 그려지므로 DB 왕복을 한 번으로 줄인다. 검색·필터는 원래도 받아 온 뒤 거르므로
        # 전체 목록을 한 번 받아 개수를 세고 같은 규칙(filter_recipes)으로 거르면 결과가 같다. (질의는 user_id 로 제한된다)
        all_rows = repo.list_recipes(user["id"])
    except db.StorageError as e:
        st.error(f"❌ 저장한 레시피를 불러오지 못했어요. {e}")
        return
    total = len(all_rows)
    rows = repository.filter_recipes(all_rows, query, min_minutes, max_minutes, difficulty)
    st.caption(f"저장한 레시피 {total}개 (최대 {repository.MAX_RECIPES_PER_USER}개)")
    if total == 0:
        st.info("아직 저장한 레시피가 없어요. '냉장고 레시피'에서 레시피를 만들고 💾 저장을 눌러 보세요.")
        return
    if not rows:
        st.warning("조건에 맞는 저장한 레시피가 없어요. 검색어나 조건을 바꿔 보세요.")
        return
    for row in rows:
        r, rid = row["recipe"], row["id"]
        with st.container(border=True):
            st.markdown(f"**{md_safe(row['title'])}**")
            st.caption(f"{recipe_caption(r)} · 저장 {kst(row['created_at'])}")
            render_recipe_summary(r)
            with st.expander("자세히 보기", key=f"mr_detail_{rid}"):
                render_recipe_body(r)
                if row["pantry"]:
                    st.caption("저장할 때 가지고 있던 재료: " + ", ".join(md_safe(p) for p in row["pantry"]))
            st.text_area("메모", value=row["memo"], key=f"memo_{rid}", max_chars=repository.MAX_MEMO_LEN, height=80,
                         placeholder="이 레시피에 대한 메모를 남겨 보세요")
            b1, b2 = st.columns([1, 1])
            b1.button("메모 저장", key=f"memo_save_{rid}", on_click=save_memo, args=(rid,))
            with b2.popover("🗑 삭제"):
                st.write("이 레시피를 지울까요? 되돌릴 수 없어요.")
                st.button("삭제 확인", key=f"mr_del_{rid}", on_click=delete_saved, args=(rid,), type="primary")


# --- 프로필 ------------------------------------------------------------------------------
def save_profile():
    ss = st.session_state
    user, (repo, repo_error) = ss.get("user"), services.get_repository()
    if user is None:
        return
    if repo is None:
        ss["profile_notice"] = ("error", f"저장하지 못했어요. {repo_error}")
        return
    try:
        updated = repo.update_profile(user["id"], ss.get("profile_name", ""), ss.get("profile_servings", 2),
                                      ss.get("profile_diet", []), ss.get("profile_allergies", []))
    except db.StorageError as e:
        ss["profile_notice"] = ("error", f"저장하지 못했어요. {e}")
        return
    if updated:
        ss["user"] = updated
        for key in ("opt_servings", "opt_allergies", "opt_diet"):  # 레시피 조건의 기본값을 새 프로필로 다시 채운다
            ss.pop(key, None)
        ss["profile_notice"] = ("success", "프로필을 저장했어요. 다음 레시피 추천부터 기본 조건으로 쓰여요.")


def delete_account():
    ss = st.session_state
    user, (repo, repo_error) = ss.get("user"), services.get_repository()
    if user is None:
        return
    ss.pop("delete_error", None)
    if repo is None:
        ss["delete_error"] = repo_error
        return
    if str(ss.get("delete_email", "")).strip().lower() != user["email"]:
        ss["delete_error"] = "확인용 이메일이 계정의 이메일과 달라요."
        return
    try:
        if user["provider"] == "password":
            cred = repo.get_credentials(user["email"])
            if not cred or not auth.verify_password(cred["password_hash"], ss.get("delete_password", "")):
                ss["delete_error"] = "비밀번호가 올바르지 않아요."
                return
        repo.delete_user(user["id"])
    except db.StorageError as e:
        ss["delete_error"] = str(e)
        return
    ss["delete_password"] = ""
    google = user["provider"] == "google"
    _leave("계정과 저장한 레시피를 모두 삭제했어요.")
    if google:
        st.logout()


def profile_page(repo, repo_error, user):
    st.title("👤 프로필")
    if repo is None:
        st.error(f"❌ {repo_error}")
        return
    st.caption(f"{md_safe(user['email'])} · {'구글' if user['provider'] == 'google' else '아이디·비밀번호'} 로그인 · 가입 {kst(user['created_at'])}")
    st.text_input("표시 이름", value=user["display_name"], key="profile_name", max_chars=repository.MAX_NAME_LEN)
    st.number_input("기본 인원수", min_value=1, max_value=10, step=1, value=user["default_servings"], key="profile_servings")
    st.multiselect("식단 제한", list(dict.fromkeys(BASE_DIETS + user["diet_restrictions"])), default=user["diet_restrictions"],
                   accept_new_options=True, key="profile_diet", placeholder="없음")
    st.multiselect("알레르기", list(dict.fromkeys(list(recipe.ALLERGEN_TERMS) + user["allergies"])), default=user["allergies"],
                   accept_new_options=True, key="profile_allergies", placeholder="없음")
    st.button("프로필 저장", type="primary", on_click=save_profile, key="profile_save")
    level, message = st.session_state.get("profile_notice", (None, None))
    if level:
        {"success": st.success, "error": st.error}[level](message)
    st.caption("저장한 식단 제한·알레르기·인원수는 레시피 추천의 기본 조건으로 채워져요. 추천할 때 바꿀 수도 있어요.")
    st.divider()
    with st.expander("계정 삭제", key="profile_delete_box"):
        st.warning("계정을 지우면 저장한 레시피도 모두 사라지고 되돌릴 수 없어요.")
        st.text_input("확인을 위해 이메일을 입력하세요", key="delete_email")
        if user["provider"] == "password":
            st.text_input("비밀번호", type="password", key="delete_password")
        st.button("계정 삭제", type="primary", on_click=delete_account, key="delete_account_btn")
        if st.session_state.get("delete_error"):
            st.error(f"❌ {st.session_state['delete_error']}")
