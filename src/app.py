# 작성: 2026-10-22 12:23 KST
# 1·2단계 화면: 냉장고 사진 업로드 → 식재료 인식 → 수정·확정 → 조건 선택 → 레시피 생성
import logging
import time

import streamlit as st

import account_ui
import llm
import recipe
import recognizer
import rows as rows_mod
import services
from ui_common import (DIFFICULTY_LABEL, TIME_CHOICES, md_safe, recipe_caption, render_recipe_body,
                       render_recipe_summary, unexpected_message)

RATE_LIMIT_COUNT = 5
RATE_LIMIT_WINDOW_SEC = 60
TOO_OFTEN = "요청이 너무 잦습니다. 1분 뒤에 다시 시도해 주세요."

logger = logging.getLogger("fridge_app")


def get_key():
    """배포 환경의 st.secrets 를 먼저 보고, 없으면 환경변수·.env 를 본다."""
    try:
        key = st.secrets["OPENROUTER_API_KEY"]
        if key:
            return key
    except Exception:
        pass
    return recognizer.get_api_key()


def rate_limited():
    """세션당 분당 호출 수를 제한한다. 공유 무료 한도를 보호하기 위함이다."""
    now = time.time()
    calls = [t for t in st.session_state.get("calls", []) if now - t < RATE_LIMIT_WINDOW_SEC]
    st.session_state["calls"] = calls
    if len(calls) >= RATE_LIMIT_COUNT:
        return True
    calls.append(now)
    return False


def cause_text(event):
    """형식 오류의 원인(어떤 문제였는지와 응답 앞부분)을 한 줄로 만든다. 모델 글자라서 이스케이프한다."""
    text = md_safe(event.get("cause") or "알 수 없음")
    if event.get("snippet"):
        text += f", 응답 앞부분 “{md_safe(event['snippet'])}”"
    return text


def start_progress(title, log):
    """진행 상자를 만들고, 이벤트를 한 줄씩 기록하는 함수와 끝내는 함수를 돌려준다. 총 소요 시간도 남긴다."""
    status = st.status(title, expanded=True)
    started = time.time()

    def on_event(event):
        line = describe_event(event)
        log.append(line)
        status.write(line)

    def finish(label, state):
        seconds = time.time() - started
        line = f"⏱ 총 {seconds:.1f}초"
        log.append(line)
        status.write(line)
        status.update(label=f"{label} · {seconds:.0f}초", state=state)

    return on_event, finish


def describe_event(event):
    """인식 진행 이벤트를 화면에 보일 한 줄로 바꾼다."""
    kind = event["kind"]
    if kind == "resize":
        return f"📐 사진 축소: {event['original_bytes'] // 1024}KB → {event['sent_bytes'] // 1024}KB"
    if kind == "request":
        return f"📡 {event['model']} 요청 ({event['attempt']}차 시도)"
    if kind == "response":
        return f"↳ {event['status']} 응답 ({event['seconds']}초)"
    if kind == "retry":
        # 429 는 요청 제한, 5xx 는 제공사 서버 오류다. 원인을 섞어 부르지 않는다.
        what = "요청 제한" if event["status"] == 429 else "서버 오류"
        return f"⏳ HTTP {event['status']} {what} — {event['wait']}초 뒤 다시 시도"
    if kind == "parse_retry":
        return f"🔁 {event['model']} 응답 형식 오류 — 다시 요청 (원인: {cause_text(event)})"
    if kind == "daily_limit":
        return (f"⛔ 하루 무료 호출 한도를 모두 썼어요 — 다른 모델도 같은 한도를 쓰므로 더 시도하지 않습니다 "
                f"({event['reset']}에 초기화)")
    if kind == "fallback":
        reason = {"unavailable": "사용 불가", "server": "서버 오류가 계속됨",
                  "parse": f"응답 형식 오류가 계속됨 (원인: {cause_text(event)})"}.get(
            event.get("reason"), "요청 제한이 계속됨"
        )
        nxt = event["next_model"]
        return f"🔀 {event['model']} {reason} → 대체 모델 {nxt}로 전환" if nxt else f"🔀 {event['model']} {reason} — 더 시도할 모델이 없음"
    if kind == "round":
        return f"🍳 레시피 {event['need']}개 요청 ({event['round']}번째)" if event["round"] > 1 else f"🍳 레시피 {event['need']}개 요청"
    if kind == "excluded":
        # 제목은 모델이 만든 글자라서 이스케이프한다(링크·서식으로 그려지지 않게).
        return f"🚫 '{md_safe(event['title'])}' 제외: {md_safe(event['reason'])}"
    if kind == "done":
        return f"✅ {event['model']} {event.get('what', '인식')} 완료 ({event['count']}개)"
    return str(event)


def delete_row(row_id):
    """삭제 단추의 콜백. 지운 행의 위젯 값도 함께 정리한다."""
    st.session_state["rows"] = rows_mod.remove_row(st.session_state["rows"], row_id)
    st.session_state.pop(rows_mod.name_key(row_id), None)
    st.session_state.pop(rows_mod.qty_key(row_id), None)


def add_row():
    row_id = st.session_state.get("next_id", 0)
    st.session_state["rows"].append(rows_mod.blank_row(row_id))
    st.session_state["next_id"] = row_id + 1


RECIPE_STATE_KEYS = ("recipe_result", "recipe_error", "recipe_log", "save_notice", "pending_save")


def clear_recipes():
    """재료가 바뀌면 이전 레시피는 이번 재료의 결과가 아니므로 모두 지운다."""
    for key in RECIPE_STATE_KEYS:
        st.session_state.pop(key, None)


# 레시피 조건 위젯의 key. 다른 메뉴(로그인·프로필 등)로 가면 Streamlit 이 이 값들을 지우므로 OPTION_STORE 에 되써 둔다.
OPTION_KEYS = ("opt_cuisine", "opt_difficulty", "opt_time", "opt_servings", "opt_allergies", "opt_diet", "opt_extra", "opt_count")
OPTION_STORE = account_ui.OPTION_STORE
# 위젯에 value·index 를 주지 않고 상태에 기본값을 넣는다(둘 다 주면 Streamlit 이 경고를 띄운다).
ANY = "제한 없음"  # 요리 종류·난이도를 고르지 않음
OPTION_DEFAULTS = {"opt_servings": 2, "opt_extra": recipe.EXTRA_CHOICES[1], "opt_count": 3}


def restore_options(user):
    """메뉴를 오가느라 지워진 레시피 조건을 되살린다.

    로그인하기 전에 고른 조건이면, 로그인한 사람 프로필의 알레르기·식단 제한을 더한다(직접 넣은 값은 빼지 않는다).
    알레르기 조건이 말없이 사라지거나 바뀌면 안 되기 때문이다.
    """
    ss = st.session_state
    # 다른 메뉴에 다녀오면 서버에는 값이 남아 있어도, 새로 그려진 위젯은 그 값을 받지 못하고 위젯 기본값(1, 빈 목록,
    # 첫 선택지)을 보인다. 화면과 실제 조건이 어긋나 알레르기가 빠진 것처럼 보이므로, 값을 상태에 다시 넣어
    # Streamlit 이 위젯에 보내게 한다(Streamlit 문서의 위젯 상태 유지 방법).
    for key in OPTION_KEYS:
        if key in ss:
            ss[key] = ss[key]
    store = ss.get(OPTION_STORE)
    if not store:
        return
    for key, value in store["values"].items():
        if key not in ss:
            ss[key] = list(value) if isinstance(value, list) else value
    if user and store.get("user_id") != user["id"]:
        for key, field in (("opt_allergies", "allergies"), ("opt_diet", "diet_restrictions")):
            ss[key] = list(dict.fromkeys([*ss.get(key, []), *user[field]]))


def remember_options(user):
    ss = st.session_state
    ss[OPTION_STORE] = {"user_id": user["id"] if user else None,
                        "values": {k: (list(ss[k]) if isinstance(ss[k], list) else ss[k]) for k in OPTION_KEYS if k in ss}}


def recipe_options():
    """레시피 조건 입력 위젯을 그리고 선택값을 돌려준다. 로그인했으면 프로필 값이 기본값으로 채워진다.

    메뉴를 오가도 고른 조건은 유지된다(restore_options). 처음이면 프로필 값, 그것도 없으면 OPTION_DEFAULTS 를 쓴다.
    """
    user = st.session_state.get("user")
    restore_options(user)
    account_ui.apply_profile_defaults(user)
    ss = st.session_state
    for key, value in OPTION_DEFAULTS.items():
        ss.setdefault(key, value)
    # 선택지에 None 을 두면 상태로 값을 다시 넣을 때 위젯이 '선택 안 함'(Choose an option)으로 그려진다.
    # 그래서 "제한 없음" 글자를 값으로 쓰고, 조건으로 넘길 때만 None 으로 바꾼다.
    for key in ("opt_cuisine", "opt_difficulty"):
        if ss.get(key, ANY) is None:
            ss[key] = ANY
    st.selectbox("요리 종류", [ANY, *recipe.CUISINES], key="opt_cuisine")
    st.selectbox(
        "난이도", [ANY, *recipe.DIFFICULTIES],
        format_func=lambda v: v if v == ANY else DIFFICULTY_LABEL[v], key="opt_difficulty",
    )
    st.selectbox("조리 시간", [c[0] for c in TIME_CHOICES], key="opt_time")
    st.number_input("인원수", min_value=1, max_value=10, step=1, key="opt_servings")
    st.multiselect(
        "알레르기", list(dict.fromkeys([*recipe.ALLERGEN_TERMS, *ss.get("opt_allergies", [])])),
        accept_new_options=True, key="opt_allergies", placeholder="없음 (직접 입력도 가능)",
    )
    st.multiselect(
        "식단 제한", list(dict.fromkeys(["채식", "비건", "페스코", *ss.get("opt_diet", [])])),
        accept_new_options=True, key="opt_diet", placeholder="없음",
    )
    st.radio(
        "추가 재료 사용", recipe.EXTRA_CHOICES, horizontal=True, key="opt_extra",
        help="불가: 가진 재료만 / 소량: 소금·간장 같은 기본 양념류만 / 허용: 자유롭게",
    )
    st.slider("추천 개수", 1, 5, key="opt_count")
    remember_options(user)
    _, min_minutes, max_minutes = next(c for c in TIME_CHOICES if c[0] == st.session_state["opt_time"])
    return {
        "cuisine": None if st.session_state["opt_cuisine"] == ANY else st.session_state["opt_cuisine"],
        "difficulty": None if st.session_state["opt_difficulty"] == ANY else st.session_state["opt_difficulty"],
        "servings": st.session_state["opt_servings"],
        "min_minutes": min_minutes,
        "max_minutes": max_minutes,
        "allergies": st.session_state["opt_allergies"],
        "restrictions": st.session_state["opt_diet"],
        "extra": st.session_state["opt_extra"],
        "count": st.session_state["opt_count"],
    }


def generate_recipes_clicked(confirmed, options):
    """레시피 생성 단추를 눌렀을 때. 진행 단계를 보여 주고 결과나 실패를 session_state 에 남긴다."""
    clear_recipes()
    if rate_limited():
        st.session_state["recipe_error"] = TOO_OFTEN  # 이어지는 화면 그리기에서 실패로 보여 준다
        return
    log = []
    st.session_state["recipe_log"] = log
    on_event, finish = start_progress("레시피를 만드는 중입니다. 최대 1~2분 걸릴 수 있어요.", log)

    def fail(message):
        log.append(f"❌ {message}")
        st.session_state["recipe_error"] = message
        finish("레시피 생성 실패", "error")

    try:
        result = recipe.generate_recipes(confirmed, options, get_key(), on_event=on_event)
    except llm.ServiceError as e:
        fail(str(e))
    except Exception as e:  # noqa: BLE001 - 예상하지 못한 오류도 숨기지 않고 실패했다고 알린다
        logger.exception("레시피 생성 중 예상하지 못한 오류")
        fail(unexpected_message(e))
    else:
        st.session_state["recipe_result"] = result
        finish(f"레시피 {len(result['recipes'])}개 완료", "complete")
    # 단추 이름("다시 생성")과 결과·진행 기록을 한 번에 새로 그린다. 진행 기록은 접힌 영역에 남는다.
    st.rerun()


def render_recipes(result):
    recipes = result["recipes"]
    user = st.session_state.get("user")
    repo, _ = services.get_repository() if user else (None, None)
    saved = account_ui.saved_fingerprints(repo, user)
    st.subheader(f"추천 레시피 {len(recipes)}개")
    account_ui.show_save_notice()
    if result["model"] and result["model"] != recipe.TEXT_MODEL:
        st.info(f"기본 모델을 쓸 수 없어 대체 모델({result['model']})로 만들었습니다. 내용을 꼼꼼히 확인해 주세요.")
    if not recipes:
        st.warning("조건에 맞는 레시피가 없어요. 조건을 완화하고 다시 생성해 보세요.")
    elif len(recipes) < result["requested"]:
        st.caption(f"요청한 {result['requested']}개 중 {len(recipes)}개만 조건에 맞았어요.")
    if result.get("note"):  # 하루 한도 소진 등으로 일부만 받고 멈춘 이유
        st.warning(md_safe(result["note"]))
    for i, r in enumerate(recipes):
        with st.container(border=True):
            st.markdown(f"**{i + 1}. {md_safe(r['title'])}**")
            st.caption(recipe_caption(r))
            render_recipe_summary(r)
            with st.expander("자세히 보기", key=f"recipe_detail_{i}"):
                render_recipe_body(r)
            account_ui.save_controls(i, r, user, repo, saved)
    if result["excluded"]:
        with st.expander(f"조건 때문에 제외한 레시피 {len(result['excluded'])}개", key="recipe_excluded"):
            for title, reason in result["excluded"]:
                st.write(f"- {md_safe(title)}: {md_safe(reason)}")


def recipe_section(confirmed):
    """2단계: 확정된 재료로 조건을 고르고 레시피를 생성한다."""
    st.divider()
    st.subheader("🍳 레시피 추천")
    st.markdown("가진 재료: " + " ".join(f":gray-badge[{md_safe(i['name'])}]" for i in confirmed))
    options = recipe_options()
    st.caption("⚠️ 알레르기·식단 정보는 AI가 틀릴 수 있어요. 재료와 조리법을 직접 꼭 확인하세요.")
    has_result = "recipe_result" in st.session_state or "recipe_error" in st.session_state
    if st.button("다시 생성" if has_result else "레시피 생성", type="primary", key="recipe_go"):
        generate_recipes_clicked(confirmed, options)
    if st.session_state.get("recipe_log"):
        with st.expander("레시피 진행 기록", key="recipe_log_box"):
            for line in st.session_state["recipe_log"]:
                st.write(line)
    if "recipe_error" in st.session_state:
        st.error(f"❌ 레시피 생성에 실패했어요. {st.session_state['recipe_error']} 이전 결과는 보여 주지 않습니다.")
    if "recipe_result" in st.session_state:
        render_recipes(st.session_state["recipe_result"])


# 좁은 화면(휴대폰)에서도 이름·수량·삭제가 한 줄로 유지되게 한다. Streamlit 은 기본적으로 세로로 쌓는다.
ONE_LINE_CSS = """
<style>
div[data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: 0.5rem !important; }
div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] { min-width: 0 !important; }
</style>
"""

st.set_page_config(page_title="냉장고 재료 인식", page_icon="🧊")
st.markdown(ONE_LINE_CSS, unsafe_allow_html=True)

# 3단계: 로그인 상태를 확인하고 메뉴를 그린다. 저장소(DB)에 연결하지 못해도 이 메인 화면(1·2단계)은 그대로 쓸 수 있다.
repo, repo_error = services.get_repository()
user = account_ui.current_user(repo)
menu = account_ui.render_sidebar(user, repo_error)
if menu != account_ui.MENU_MAIN:
    account_ui.render_page(menu, repo, repo_error, user)
    st.stop()
account_ui.apply_pending_save(user)  # 저장하려다 로그인하러 갔다면 돌아온 지금 이어서 저장한다

st.title("🧊 냉장고 재료 인식")
st.caption("냉장고 사진을 올리면 안에 있는 식재료를 찾아 드립니다.")

uploaded = st.file_uploader("냉장고 사진 (JPEG, PNG, WebP · 10MB 이하)", type=["jpg", "jpeg", "png", "webp"])
# 켜기 전에는 camera_input 을 만들지 않는다. 만들면 페이지를 열 때마다 카메라 권한을 요청한다.
shot = st.camera_input("카메라로 촬영") if st.toggle("카메라로 직접 촬영") else None

source = uploaded or shot
if source is not None:
    st.image(source, caption="미리보기", width=360)

just_ran = False
if st.button("식재료 인식", type="primary", disabled=source is None):
    # 새로 인식하면 이전 결과는 이번 사진의 결과가 아니므로 모두 지운다. 실패해도 이전 결과가 남아 보이지 않게 한다.
    st.session_state.pop("confirmed", None)
    st.session_state.pop("rows", None)
    st.session_state.pop("run_info", None)
    st.session_state.pop("recognize_error", None)
    # 이전 진행 기록도 지운다. 요청 제한에 걸려 새 기록이 생기지 않을 때 직전 성공 기록이 이번 시도처럼 보이면 안 된다.
    st.session_state.pop("progress_log", None)
    clear_recipes()
    if rate_limited():
        st.session_state["recognize_error"] = TOO_OFTEN
    else:
        log = []
        st.session_state["progress_log"] = log
        on_event, finish = start_progress("사진을 분석하는 중입니다. 최대 1~2분 걸릴 수 있어요.", log)

        def fail(message):
            st.session_state.pop("rows", None)
            st.session_state["recognize_error"] = message
            log.append(f"❌ {message}")
            finish("인식 실패", "error")

        try:
            result = recognizer.recognize_detailed(source.getvalue(), get_key(), on_event=on_event)
            # id 는 계속 늘려서, 이전 인식 결과의 위젯 값이 새 행에 섞이지 않게 한다.
            new_rows, st.session_state["next_id"] = rows_mod.make_rows(
                result["items"], st.session_state.get("next_id", 0)
            )
            st.session_state["rows"] = new_rows
            st.session_state["run_info"] = result
            finish(f"인식 완료 ({len(result['items'])}개)", "complete")
            just_ran = True
        except recognizer.RecognitionError as e:
            fail(str(e))
        except Exception as e:  # noqa: BLE001 - 예상하지 못한 오류도 숨기지 않고 실패했다고 알린다
            logger.exception("사진 인식 중 예상하지 못한 오류")
            fail(unexpected_message(e))

# 실패 메시지는 다음 인식을 누를 때까지 화면에 남긴다. (다른 것을 조작해 화면이 다시 그려져도 사라지지 않는다)
if "recognize_error" in st.session_state:
    st.error(f"❌ 사진 인식에 실패했어요. {st.session_state['recognize_error']} 이전 인식 결과는 보여 주지 않습니다.")

# 새로 실행하지 않은 화면(표 수정 등)에서도 직전 진행 기록을 볼 수 있게 한다.
if not just_ran and st.session_state.get("progress_log"):
    with st.expander("진행 기록"):
        for line in st.session_state["progress_log"]:
            st.write(line)

if "rows" in st.session_state:
    rows_state = st.session_state["rows"]
    info = st.session_state.get("run_info")
    st.subheader("인식 결과")
    if info:
        st.caption(
            f"사진을 {info['original_bytes'] // 1024}KB → {info['sent_bytes'] // 1024}KB로 줄여 전송했습니다."
        )
        if info["model"] != recognizer.VISION_MODEL:
            st.info(f"기본 모델을 쓸 수 없어(혼잡하거나 서비스가 끝남) 대체 모델({info['model']})로 인식했습니다. 결과를 꼼꼼히 확인해 주세요.")
    if not rows_state:
        st.info("식재료를 찾지 못했습니다. 사진이 어둡거나 가려졌을 수 있어요. 아래 '재료 추가'로 직접 넣어 보세요.")
    else:
        st.caption("⚠️ 표시는 신뢰도가 낮은 항목입니다. 사진과 비교해 확인하고, 이름과 수량을 고치거나 🗑로 지울 수 있어요.")

    # 한 줄 = [신뢰도 경고] [재료 이름] [수량] [삭제]
    LINE = [0.5, 4, 2.5, 1]
    head = st.columns(LINE, vertical_alignment="center")
    head[1].caption("재료 이름")
    head[2].caption("수량")
    for row in rows_state:
        c_warn, c_name, c_qty, c_del = st.columns(LINE, vertical_alignment="center")
        c_warn.write("⚠️" if row["confidence"] == "low" else "")
        # 다른 메뉴로 갔다 오면 Streamlit 이 이 화면에 없던 위젯의 값을 지운다. 편집한 값을 행에도 기억해 두었다가,
        # 위젯 값이 없을 때만 그 값으로 채운다. (value 를 매번 바꿔 주면 위젯이 다시 만들어지므로 상태에 미리 넣는다)
        name_key, qty_key = rows_mod.name_key(row["id"]), rows_mod.qty_key(row["id"])
        st.session_state.setdefault(name_key, row["name"])
        st.session_state.setdefault(qty_key, row["quantity"])
        c_name.text_input("재료 이름", key=name_key, label_visibility="collapsed", placeholder="재료 이름")
        c_qty.text_input("수량", key=qty_key, label_visibility="collapsed", placeholder="예: 2개")
        row["name"], row["quantity"] = st.session_state[name_key], st.session_state[qty_key]
        c_del.button("🗑", key=f"del_{row['id']}", on_click=delete_row, args=(row["id"],), help="이 재료 삭제")
    st.button("＋ 재료 추가", on_click=add_row)

    if st.button("이 목록으로 확정"):
        confirmed = rows_mod.collect_confirmed(rows_state, st.session_state)
        if confirmed:
            st.session_state["confirmed"] = confirmed
            clear_recipes()  # 재료를 다시 확정했으면 이전 레시피는 지운다
        else:
            st.session_state.pop("confirmed", None)
            clear_recipes()
            st.warning("재료가 하나도 없습니다. 한 개 이상 입력해 주세요.")

if "confirmed" in st.session_state:
    names = ", ".join(md_safe(i["name"]) for i in st.session_state["confirmed"])  # 모델 인식 결과라 이스케이프
    st.success(f"확정됨: {names}")
    recipe_section(st.session_state["confirmed"])
