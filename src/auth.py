# 작성: 2026-10-24 22:54 KST
# 3단계: 가입·로그인(아이디=이메일 + 비밀번호), 구글 로그인 연결, 로그인 세션 토큰, 로그인 실패 제한. (Streamlit 에 의존하지 않는다)
#
# - 비밀번호는 argon2 해시로만 저장한다. 평문은 저장도 로그도 하지 않는다.
# - 계정은 이메일 하나에 하나다. 가입 방식(password·google)이 다르면 합치지 않고 안내한다.
#   (이메일 인증이 없는 비밀번호 가입과 구글 계정을 자동으로 합치면, 남의 이메일로 미리 가입해 둔 계정에 들어가게 될 수 있다)
import datetime as dt
import hashlib
import re
import secrets
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

import llm
from repository import DuplicateEmailError

COOKIE_NAME = "fridge_session"
SESSION_DAYS = 30
MIN_PASSWORD = 8
MAX_PASSWORD = 128
MAX_FAILURES = 5
FAILURE_WINDOW_SEC = 15 * 60

GENERIC_LOGIN_ERROR = "이메일 또는 비밀번호가 올바르지 않습니다."
GOOGLE_ACCOUNT = "이 이메일은 구글 로그인으로 가입되어 있어요. '구글로 로그인'을 눌러 주세요."
PASSWORD_ACCOUNT = "이 이메일은 이미 아이디·비밀번호로 가입되어 있어요. 아이디·비밀번호로 로그인해 주세요."

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_COMMON_PASSWORDS = {"12345678", "123456789", "1234567890", "password", "password1", "qwertyui", "qwerty123", "11111111",
                     "00000000", "abcd1234", "iloveyou", "asdfghjk", "비밀번호123"}
_HASHER = PasswordHasher()  # argon2id 기본 설정
# 없는 이메일로 로그인해도 같은 시간이 걸리도록, 없을 때도 이 해시로 검증을 한 번 한다(계정 존재 여부가 시간으로 드러나지 않게).
_DUMMY_HASH = _HASHER.hash("timing-equalizer-not-a-real-password")


class AuthError(llm.ServiceError):
    """사용자에게 그대로 보여 줘도 되는 인증 오류. 비밀번호나 해시는 담지 않는다."""


def normalize_email(email):
    return str(email or "").strip().lower()


def validate_email(email):
    email = normalize_email(email)
    if not email or len(email) > 254 or not _EMAIL_RE.match(email):
        raise AuthError("이메일 형식이 올바르지 않아요. 예: name@example.com")
    return email


def validate_password(password, email=""):
    password = password or ""
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"비밀번호는 {MIN_PASSWORD}자 이상이어야 해요.")
    if len(password) > MAX_PASSWORD:
        raise AuthError(f"비밀번호는 {MAX_PASSWORD}자 이하여야 해요.")
    if password.lower() in _COMMON_PASSWORDS or password.lower() == normalize_email(email):
        raise AuthError("너무 흔하거나 추측하기 쉬운 비밀번호예요. 다른 비밀번호를 써 주세요.")
    return password


def hash_password(password):
    return _HASHER.hash(password)


def verify_password(password_hash, password):
    try:
        return _HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


class LoginLimiter:
    """같은 이메일로 로그인에 연달아 실패하면 잠깐 막는다. 서버 메모리에만 있어 재시작하면 초기화된다.

    - 실패 기록은 창 시간(window_sec)이 지나면 지운다. 아무 이메일로나 시도해도 기록이 끝없이 쌓이지 않게 한다.
    - begin()·end() 는 확인과 기록을 한 자리 잡기로 묶는다. 비밀번호 확인(argon2)이 도는 동안 진행 중인 시도도
      실패할 수 있는 것으로 세므로, 여러 세션이 동시에 시도해도 비밀번호 확인까지 가는 횟수가 max_failures 를 넘지 않는다.
      (횟수·시간 규칙은 check()·fail() 과 같다. 하나씩 차례로 시도하면 결과가 똑같다)
    """

    def __init__(self, max_failures=MAX_FAILURES, window_sec=FAILURE_WINDOW_SEC, clock=time.time):
        self.max_failures, self.window_sec, self._clock = max_failures, window_sec, clock
        self._failures = {}
        self._pending = {}  # 이메일별로 비밀번호 확인이 진행 중인 시도 수
        self._lock = threading.Lock()
        self._last_sweep = clock()

    def _recent(self, key, now):
        """창 시간 안의 실패 기록. 하나도 없으면 키를 지운다."""
        fresh = [t for t in self._failures.get(key, ()) if now - t < self.window_sec]
        if fresh:
            self._failures[key] = fresh
        else:
            self._failures.pop(key, None)
        return fresh

    def _sweep(self, now):
        """창 시간이 지난 기록을 한꺼번에 지운다. 창 시간에 한 번만 돌아서 평소 비용은 거의 없다."""
        if now - self._last_sweep < self.window_sec:
            return
        self._last_sweep = now
        # dict 는 키를 지워도 내부 표가 줄지 않으므로, 남길 것만 담아 새로 만든다.
        self._failures = {k: times for k, times in self._failures.items() if times and now - times[-1] < self.window_sec}

    def _blocked(self, fresh, now):
        wait = max(1, int((fresh[0] + self.window_sec - now) // 60) + 1) if fresh else 1
        return AuthError(f"로그인에 연달아 실패해서 잠시 막았어요. 약 {wait}분 뒤에 다시 시도해 주세요.")

    def check(self, key):
        with self._lock:
            now = self._clock()
            self._sweep(now)
            fresh = self._recent(key, now)
            if len(fresh) >= self.max_failures:
                raise self._blocked(fresh, now)

    def fail(self, key):
        with self._lock:
            now = self._clock()
            self._failures[key] = [*self._recent(key, now), now]

    def reset(self, key):
        with self._lock:
            self._failures.pop(key, None)

    def begin(self, key):
        """시도를 시작한다. 지난 실패와 진행 중인 시도를 합쳐 한도에 닿았으면 막는다. 통과하면 반드시 end() 를 부른다."""
        with self._lock:
            now = self._clock()
            self._sweep(now)
            fresh = self._recent(key, now)
            if len(fresh) + self._pending.get(key, 0) >= self.max_failures:
                raise self._blocked(fresh, now)
            self._pending[key] = self._pending.get(key, 0) + 1

    def end(self, key, outcome):
        """시도를 끝낸다. outcome 은 "fail"(실패 기록), "success"(기록 지움), 그 밖(기록하지 않음)이다."""
        with self._lock:
            left = self._pending.get(key, 0) - 1
            if left > 0:
                self._pending[key] = left
            else:
                self._pending.pop(key, None)
            now = self._clock()
            if outcome == "fail":
                self._failures[key] = [*self._recent(key, now), now]
            elif outcome == "success":
                self._failures.pop(key, None)


def register(repo, email, password, display_name=""):
    """아이디(이메일)·비밀번호로 가입한다. 만들어진 사용자(비밀번호 해시 제외)를 돌려준다."""
    email = validate_email(email)
    validate_password(password, email)
    existing = repo.get_credentials(email)
    if existing:
        raise AuthError(GOOGLE_ACCOUNT if existing["provider"] == "google" else "이미 가입된 이메일입니다. 로그인해 주세요.")
    name = str(display_name or "").strip() or email.split("@")[0]
    try:
        return repo.create_user(email, "password", name, password_hash=hash_password(password))
    except DuplicateEmailError:  # 동시에 같은 이메일로 가입한 경우
        raise AuthError("이미 가입된 이메일입니다. 로그인해 주세요.") from None


def login(repo, email, password, limiter=None):
    """아이디(이메일)·비밀번호로 로그인한다. 틀린 이유(이메일 없음/비밀번호 틀림)는 구분해서 알려 주지 않는다."""
    email = normalize_email(email)
    if limiter:
        limiter.begin(email)  # 확인과 실패 기록 사이에 다른 세션의 시도가 끼어들지 못하게 자리를 먼저 잡는다
    outcome = None  # 실패로 세지 않는 경우(구글 계정, 저장소 오류 등)는 그대로 None
    try:
        cred = repo.get_credentials(email) if email else None
        if cred is None:
            verify_password(_DUMMY_HASH, password or "")
            outcome = "fail"
            raise AuthError(GENERIC_LOGIN_ERROR)
        if cred["provider"] == "google" or not cred["password_hash"]:
            raise AuthError(GOOGLE_ACCOUNT)
        if not verify_password(cred["password_hash"], password or ""):
            outcome = "fail"
            raise AuthError(GENERIC_LOGIN_ERROR)
        outcome = "success"
    finally:
        if limiter:
            limiter.end(email, outcome)
    repo.touch_login(cred["id"])
    return repo.get_user(cred["id"])


def login_with_google(repo, identity):
    """구글이 보낸 사용자 정보(email, name, sub, email_verified)로 로그인하고, 처음이면 계정을 만든다."""
    email = normalize_email((identity or {}).get("email"))
    if not email:
        raise AuthError("구글 계정의 이메일을 받지 못했어요. 다시 시도해 주세요.")
    if (identity or {}).get("email_verified") is not True:
        raise AuthError("구글에서 확인되지 않은 이메일이라 로그인할 수 없어요.")
    sub = str(identity.get("sub") or "") or None
    cred = repo.get_credentials(email)
    if cred is None:
        name = str(identity.get("name") or "").strip() or email.split("@")[0]
        try:
            user = repo.create_user(email, "google", name, google_sub=sub)
        except DuplicateEmailError:
            raise AuthError("계정을 만드는 중 문제가 생겼어요. 다시 시도해 주세요.") from None
        repo.touch_login(user["id"])
        return user
    if cred["provider"] == "password":
        raise AuthError(PASSWORD_ACCOUNT)
    if cred.get("google_sub") and sub and cred["google_sub"] != sub:
        raise AuthError("이 이메일에 연결된 구글 계정 정보가 달라서 로그인할 수 없어요.")
    repo.touch_login(cred["id"])
    return repo.get_user(cred["id"])


# --- 로그인 유지(쿠키) 세션 ---
def new_token():
    return secrets.token_urlsafe(32)


def hash_token(token):
    """DB 에는 토큰이 아니라 해시만 저장한다. DB 가 유출돼도 쿠키를 만들 수 없게 하기 위해서다."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_token_format(token):
    return isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is not None


def start_session(repo, user_id, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    token = new_token()
    repo.purge_expired_sessions()
    repo.create_session(user_id, hash_token(token), now + dt.timedelta(days=SESSION_DAYS))
    return token


def user_from_token(repo, token):
    if not valid_token_format(token):
        return None
    return repo.user_for_session(hash_token(token))


def end_session(repo, token):
    if valid_token_format(token):
        repo.delete_session(hash_token(token))
