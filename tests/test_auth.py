# 작성: 2026-10-24 22:59 KST
import datetime as dt

import pytest

import auth
import repository


@pytest.fixture
def repo():
    return repository.MemoryRepository()


PW = "pa55word*!"


# --- 입력 검증 -----------------------------------------------------------------------------
@pytest.mark.parametrize("email", ["", "plain", "a@b", "a b@c.com", "@c.com", "a@@c.com", "x" * 250 + "@c.com"])
def test_bad_emails_are_rejected(email):
    with pytest.raises(auth.AuthError):
        auth.validate_email(email)


def test_email_is_trimmed_and_lowercased():
    assert auth.validate_email("  Hannah@Example.COM ") == "hannah@example.com"


@pytest.mark.parametrize("pw,part", [("short1", "8자 이상"), ("x" * 129, "128자 이하"), ("password", "흔하거나"),
                                      ("12345678", "흔하거나"), ("Hannah@Example.com", "흔하거나")])
def test_weak_passwords_are_rejected(pw, part):
    with pytest.raises(auth.AuthError) as e:
        auth.validate_password(pw, "hannah@example.com")
    assert part in str(e.value)


def test_strong_enough_password_passes_including_special_characters():
    assert auth.validate_password("한글비번*abc!") == "한글비번*abc!"


# --- 비밀번호 해시 ------------------------------------------------------------------------
def test_hash_is_argon2id_salted_and_verifies():
    h1, h2 = auth.hash_password(PW), auth.hash_password(PW)
    assert h1.startswith("$argon2id$") and h1 != h2                 # 솔트 때문에 매번 다르다
    assert PW not in h1
    assert auth.verify_password(h1, PW) and not auth.verify_password(h1, PW + "x")


@pytest.mark.parametrize("bad_hash", ["", "not-a-hash", None, "$argon2id$garbage"])
def test_invalid_hash_never_verifies_and_never_raises(bad_hash):
    assert auth.verify_password(bad_hash or "", PW) is False


# --- 가입 ----------------------------------------------------------------------------------
def test_register_stores_only_a_hash_and_defaults_the_display_name(repo):
    user = auth.register(repo, " Hannah@Example.com ", PW)
    assert user["email"] == "hannah@example.com" and user["provider"] == "password" and user["display_name"] == "hannah"
    assert "password_hash" not in user                               # 사용자 정보에 해시를 싣지 않는다
    stored = repo.users[user["id"]]["password_hash"]
    assert stored.startswith("$argon2id$") and PW not in str(repo.users)


def test_register_uses_the_given_display_name(repo):
    assert auth.register(repo, "a@example.com", PW, "  한나  ")["display_name"] == "한나"


def test_register_rejects_a_duplicate_email_case_insensitively(repo):
    auth.register(repo, "a@example.com", PW)
    with pytest.raises(auth.AuthError) as e:
        auth.register(repo, "A@EXAMPLE.COM", PW)
    assert "이미 가입" in str(e.value)


def test_register_does_not_merge_into_a_google_account(repo):
    repo.create_user("a@example.com", "google", "구글 사용자", google_sub="sub-1")
    with pytest.raises(auth.AuthError) as e:
        auth.register(repo, "a@example.com", PW)
    assert str(e.value) == auth.GOOGLE_ACCOUNT
    assert repo.users[1]["provider"] == "google" and repo.users[1]["password_hash"] is None


def test_register_handles_a_signup_race_as_a_normal_duplicate(repo, monkeypatch):
    monkeypatch.setattr(repo, "get_credentials", lambda email: None)   # 확인 직후 다른 사람이 먼저 가입한 상황
    auth.register(repo, "a@example.com", PW)
    with pytest.raises(auth.AuthError) as e:
        auth.register(repo, "a@example.com", PW)
    assert "이미 가입" in str(e.value)


# --- 로그인 --------------------------------------------------------------------------------
def test_login_succeeds_with_any_email_case_and_records_the_time(repo):
    auth.register(repo, "a@example.com", PW)
    user = auth.login(repo, "  A@Example.com", PW)
    assert user["email"] == "a@example.com" and user["last_login_at"] is not None


def test_wrong_password_and_unknown_email_get_the_same_message(repo):
    auth.register(repo, "a@example.com", PW)
    with pytest.raises(auth.AuthError) as wrong:
        auth.login(repo, "a@example.com", "wrong-password")
    with pytest.raises(auth.AuthError) as unknown:
        auth.login(repo, "nobody@example.com", PW)
    assert str(wrong.value) == str(unknown.value) == auth.GENERIC_LOGIN_ERROR   # 계정 존재 여부를 알려 주지 않는다


def test_unknown_email_still_spends_the_time_of_a_hash_check(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "verify_password", lambda h, p: calls.append(h) or False)
    with pytest.raises(auth.AuthError):
        auth.login(repo, "nobody@example.com", PW)
    assert calls == [auth._DUMMY_HASH]                               # 없는 이메일도 검증을 한 번 한다(시간 차이로 드러나지 않게)


def test_a_google_account_cannot_log_in_with_a_password(repo):
    repo.create_user("a@example.com", "google", "구글", google_sub="s")
    with pytest.raises(auth.AuthError) as e:
        auth.login(repo, "a@example.com", PW)
    assert str(e.value) == auth.GOOGLE_ACCOUNT


def test_empty_inputs_fail_cleanly(repo):
    for email, pw in (("", ""), (None, None), ("a@example.com", None)):
        with pytest.raises(auth.AuthError):
            auth.login(repo, email, pw)


def test_login_locks_after_repeated_failures_then_recovers_after_the_window(repo):
    now = [1000.0]
    limiter = auth.LoginLimiter(max_failures=3, window_sec=600, clock=lambda: now[0])
    auth.register(repo, "a@example.com", PW)
    for _ in range(3):
        with pytest.raises(auth.AuthError) as e:
            auth.login(repo, "a@example.com", "wrong-password", limiter)
        assert str(e.value) == auth.GENERIC_LOGIN_ERROR
    with pytest.raises(auth.AuthError) as locked:
        auth.login(repo, "a@example.com", PW, limiter)               # 맞는 비밀번호여도 잠겨 있는 동안은 막는다
    assert "연달아 실패" in str(locked.value) and "분 뒤" in str(locked.value)
    now[0] += 601
    assert auth.login(repo, "a@example.com", PW, limiter)["email"] == "a@example.com"


def test_a_success_resets_the_failure_count(repo):
    limiter = auth.LoginLimiter(max_failures=3, window_sec=600)
    auth.register(repo, "a@example.com", PW)
    for _ in range(2):
        with pytest.raises(auth.AuthError):
            auth.login(repo, "a@example.com", "wrong-password", limiter)
    auth.login(repo, "a@example.com", PW, limiter)
    for _ in range(2):                                               # 성공으로 초기화됐으니 다시 2번까지는 괜찮다
        with pytest.raises(auth.AuthError) as e:
            auth.login(repo, "a@example.com", "wrong-password", limiter)
        assert str(e.value) == auth.GENERIC_LOGIN_ERROR


def test_failures_of_one_email_do_not_lock_another(repo):
    limiter = auth.LoginLimiter(max_failures=2, window_sec=600)
    auth.register(repo, "a@example.com", PW)
    auth.register(repo, "b@example.com", PW)
    for _ in range(2):
        with pytest.raises(auth.AuthError):
            auth.login(repo, "a@example.com", "wrong-password", limiter)
    assert auth.login(repo, "b@example.com", PW, limiter)["email"] == "b@example.com"


def test_old_failure_records_are_dropped_so_memory_does_not_grow(repo):
    now = [1000.0]
    limiter = auth.LoginLimiter(max_failures=5, window_sec=600, clock=lambda: now[0])
    for i in range(500):                                             # 아무 이메일로나 한 번씩 실패(argon2 를 피하려고 제한기만 쓴다)
        limiter.begin(f"nobody{i}@example.com")
        limiter.end(f"nobody{i}@example.com", "fail")
    assert len(limiter._failures) == 500
    now[0] += 601                                                    # 창 시간이 지난 뒤 다음 시도에서 한꺼번에 정리한다
    with pytest.raises(auth.AuthError):
        auth.login(repo, "someone@example.com", PW, limiter)
    assert list(limiter._failures) == ["someone@example.com"] and limiter._pending == {}


def test_a_lock_still_counts_failures_inside_the_window_after_a_sweep(repo):
    now = [1000.0]
    limiter = auth.LoginLimiter(max_failures=2, window_sec=600, clock=lambda: now[0])
    auth.register(repo, "a@example.com", PW)
    now[0] += 590
    for _ in range(2):
        with pytest.raises(auth.AuthError):
            auth.login(repo, "a@example.com", "wrong-password", limiter)
    now[0] += 20                                                     # 정리가 돌 시점이지만 이 기록은 아직 창 안이다
    with pytest.raises(auth.AuthError) as locked:
        auth.login(repo, "a@example.com", PW, limiter)
    assert "연달아 실패" in str(locked.value)


def test_concurrent_attempts_cannot_get_more_password_checks_than_the_limit(repo, monkeypatch):
    import threading
    auth.register(repo, "a@example.com", PW)
    limiter = auth.LoginLimiter(max_failures=3, window_sec=600)
    checks, gate = [], threading.Event()

    def slow_verify(h, p):                                           # 비밀번호 확인이 도는 동안 다른 시도가 몰려온다
        checks.append(p)
        gate.wait(2)
        return False

    monkeypatch.setattr(auth, "verify_password", slow_verify)
    results = []

    def attempt():
        try:
            auth.login(repo, "a@example.com", "wrong-password", limiter)
        except auth.AuthError as e:
            results.append(str(e))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for _ in range(200):                                             # 한도만큼 확인에 들어가고 나머지는 막혔을 때까지 기다린다
        if len(checks) >= 3 and len(results) >= 5:
            break
        threading.Event().wait(0.01)
    gate.set()
    for t in threads:
        t.join()
    assert len(checks) == 3                                          # 동시에 8번 시도해도 한도(3)만큼만 비밀번호를 확인했다
    assert sum("연달아 실패" in r for r in results) == 5 and limiter._pending == {}


def test_an_attempt_that_is_not_a_password_failure_does_not_count(repo):
    limiter = auth.LoginLimiter(max_failures=1, window_sec=600)
    repo.create_user("g@example.com", "google", "구글", google_sub="s")
    for _ in range(3):                                               # 구글 계정 안내는 실패로 세지 않는다(이전과 같음)
        with pytest.raises(auth.AuthError) as e:
            auth.login(repo, "g@example.com", PW, limiter)
        assert str(e.value) == auth.GOOGLE_ACCOUNT
    assert limiter._pending == {} and limiter._failures == {}


# --- 구글 로그인 ---------------------------------------------------------------------------
def identity(**kw):
    base = {"email": "G@Example.com", "name": "구글 한나", "sub": "sub-1", "email_verified": True}
    base.update(kw)
    return base


def test_first_google_login_creates_a_google_account(repo):
    user = auth.login_with_google(repo, identity())
    assert user["email"] == "g@example.com" and user["provider"] == "google" and user["display_name"] == "구글 한나"
    assert repo.users[user["id"]]["password_hash"] is None and repo.users[user["id"]]["google_sub"] == "sub-1"


def test_second_google_login_reuses_the_account(repo):
    first = auth.login_with_google(repo, identity())
    second = auth.login_with_google(repo, identity(name="이름 바뀜"))
    assert second["id"] == first["id"] and len(repo.users) == 1


@pytest.mark.parametrize("bad", [identity(email=None), identity(email=""), identity(email_verified=False),
                                 identity(email_verified=None), identity(email_verified="true"), None, {}])
def test_unverified_or_incomplete_google_identity_is_refused(repo, bad):
    with pytest.raises(auth.AuthError):
        auth.login_with_google(repo, bad)
    assert repo.users == {}                                          # 계정도 만들지 않는다


def test_google_login_does_not_merge_into_a_password_account(repo):
    auth.register(repo, "g@example.com", PW)
    with pytest.raises(auth.AuthError) as e:
        auth.login_with_google(repo, identity())
    assert str(e.value) == auth.PASSWORD_ACCOUNT
    assert repo.users[1]["provider"] == "password" and repo.users[1]["google_sub"] is None


def test_google_login_refuses_a_different_google_subject_for_the_same_email(repo):
    auth.login_with_google(repo, identity(sub="sub-1"))
    with pytest.raises(auth.AuthError) as e:
        auth.login_with_google(repo, identity(sub="sub-OTHER"))
    assert "달라서" in str(e.value)


# --- 로그인 유지 세션 ---------------------------------------------------------------------
def test_token_has_enough_randomness_and_only_its_hash_is_stored(repo):
    user = auth.register(repo, "a@example.com", PW)
    token = auth.start_session(repo, user["id"])
    assert auth.valid_token_format(token) and len(token) == 43
    assert token not in repo.sessions                                # 저장되는 것은 토큰이 아니라 해시
    assert auth.hash_token(token) in repo.sessions and len(auth.hash_token(token)) == 64
    assert auth.start_session(repo, user["id"]) != token             # 매번 다른 토큰


def test_token_logs_the_user_back_in_until_it_expires(repo):
    now = [dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc)]
    repo = repository.MemoryRepository(now=lambda: now[0])
    user = auth.register(repo, "a@example.com", PW)
    token = auth.start_session(repo, user["id"], now=now[0])
    assert auth.user_from_token(repo, token)["id"] == user["id"]
    now[0] += dt.timedelta(days=auth.SESSION_DAYS - 1)
    assert auth.user_from_token(repo, token) is not None
    now[0] += dt.timedelta(days=2)
    assert auth.user_from_token(repo, token) is None                 # 30일이 지나면 만료


@pytest.mark.parametrize("bad", [None, "", "short", "x" * 44, "!" * 43, 12345, "a" * 42 + " "])
def test_malformed_tokens_are_rejected_without_touching_the_store(repo, bad):
    assert auth.user_from_token(repo, bad) is None


def test_unknown_token_does_not_log_anyone_in(repo):
    auth.register(repo, "a@example.com", PW)
    assert auth.user_from_token(repo, auth.new_token()) is None


def test_end_session_makes_the_token_useless(repo):
    user = auth.register(repo, "a@example.com", PW)
    token = auth.start_session(repo, user["id"])
    auth.end_session(repo, token)
    assert auth.user_from_token(repo, token) is None
    auth.end_session(repo, "엉뚱한 값")                                 # 잘못된 값이어도 오류 없이 끝난다


def test_deleting_the_user_invalidates_their_tokens(repo):
    user = auth.register(repo, "a@example.com", PW)
    token = auth.start_session(repo, user["id"])
    repo.delete_user(user["id"])
    assert auth.user_from_token(repo, token) is None


def test_starting_a_session_purges_expired_ones(repo):
    now = [dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc)]
    repo = repository.MemoryRepository(now=lambda: now[0])
    user = auth.register(repo, "a@example.com", PW)
    old = auth.start_session(repo, user["id"], now=now[0])
    now[0] += dt.timedelta(days=40)
    auth.start_session(repo, user["id"], now=now[0])
    assert auth.hash_token(old) not in repo.sessions and len(repo.sessions) == 1



def test_google_account_whose_email_changed_gets_a_matching_explanation(repo):
    auth.login_with_google(repo, identity(email="old@example.com", sub="sub-1"))
    with pytest.raises(auth.AuthError) as e:                         # 같은 구글 계정(sub)이 새 이메일로 돌아온 경우
        auth.login_with_google(repo, identity(email="new@example.com", sub="sub-1"))
    assert str(e.value) == auth.GOOGLE_CREATE_FAILED and "이메일을 바꾸" in str(e.value)
    assert len(repo.users) == 1
