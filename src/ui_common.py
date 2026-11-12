# 작성: 2026-10-24 22:55 KST
# 여러 화면(냉장고 레시피, 내 레시피)이 함께 쓰는 Streamlit 표시 도구.
import re

import streamlit as st

import recipe

DIFFICULTY_LABEL = recipe.DIFFICULTY_KO  # easy/medium/hard → 초급/중급/고급
# 조리 시간 선택지: (이름, 최소 분, 최대 분). "이상"은 최소 시간, "이내"는 최대 시간이다.
TIME_CHOICES = (("제한 없음", None, None), ("15분 이내", None, 15), ("30분 이내", None, 30), ("30분 이상", 30, None))


def md_safe(text):
    """모델이 준 글자를 마크다운·링크·수식으로 해석하지 않도록 이스케이프한다. 모델 출력은 믿지 않는다."""
    return re.sub(r"([\\`*_{}\[\]()#+!|<>~$])", r"\\\1", str(text))


def unexpected_message(error):
    """예상하지 못한 오류를 사용자에게 알릴 문구. 오류 종류만 알리고 내용(경로·키 등)은 서버 로그에만 남긴다."""
    return f"예상하지 못한 오류가 발생했어요. 잠시 후 다시 시도해도 같으면 관리자에게 알려 주세요. (오류 종류: {type(error).__name__})"


def recipe_caption(r):
    """카드 위쪽 한 줄 요약(요리 종류 · 시간 · 난이도 · 인분 · 부족한 재료). 모르는 요리 종류·난이도는 지어내지 않고 뺀다."""
    lack = "부족한 재료 없음" if not r.get("missing") else f"부족한 재료 {len(r['missing'])}개"
    kind = f"{r['cuisine']} · " if r.get("cuisine") else ""
    level = DIFFICULTY_LABEL.get(r.get("difficulty"))
    level = f"{level} · " if level else ""
    return f"{kind}⏱ {r['time_minutes']}분 · {level}{r['servings']}인분 · {lack}"


def render_recipe_summary(r):
    """요약 문장과 부족한 재료 배지."""
    if r.get("summary"):
        st.write(md_safe(r["summary"]))
    if r.get("missing"):
        st.markdown(" ".join(f":red-badge[🛒 {md_safe(m)}]" for m in r["missing"]))


def render_recipe_body(r):
    """재료와 만드는 법. 모델이 만든 글자라서 모두 이스케이프한다."""
    st.markdown("**재료**")
    for ing in r["ingredients"]:
        mark = "✅" if ing.get("have") else "🛒"
        st.markdown(f"- {mark} {md_safe(ing['name'])} {md_safe(ing.get('amount', ''))}")
    st.markdown("**만드는 법**")
    for n, step in enumerate(r["steps"], 1):
        st.markdown(f"{n}\\. {md_safe(step)}")
