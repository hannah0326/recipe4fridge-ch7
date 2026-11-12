# 작성: 2026-10-24 23:01 KST
# DB 접속 계층 테스트. 실제 DB 없이 가짜 연결·풀로, 앱이 잠들었다 깨어난 뒤 죽은 연결을 만났을 때의 복구와
# 접속 정보가 오류 문구에 새지 않는지를 확인한다.
import psycopg2
import psycopg2.pool
import psycopg2.errors
import pytest

import db

URL = "postgresql://postgres.abcdefghij:s3cr3t*pass@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.conn.executed.append((query, params))
        if self.conn.dead:
            raise psycopg2.OperationalError("server closed the connection unexpectedly")
        if self.conn.raise_on and self.conn.raise_on(query):
            raise self.conn.raise_on(query)
        self.conn.last = query

    def fetchall(self):
        return [{"n": 1}, {"n": 2}]

    def fetchone(self):
        return {"n": 1}


class FakeConn:
    def __init__(self, dead=False, raise_on=None):
        self.dead, self.raise_on, self.closed = dead, raise_on, False
        self.executed, self.commits, self.rollbacks, self.last = [], 0, 0, None

    def cursor(self, cursor_factory=None):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakePool:
    def __init__(self, conns):
        self.conns, self.given, self.returned, self.closed_all = list(conns), [], [], False

    def getconn(self):
        conn = self.conns.pop(0) if len(self.conns) > 1 else self.conns[0]
        self.given.append(conn)
        return conn

    def putconn(self, conn, close=False):
        self.returned.append((conn, close))

    def closeall(self):
        self.closed_all = True


def make_db(*pools, clock=None):
    pools = list(pools)
    made = []

    def factory(url):
        pool = pools.pop(0)
        made.append(pool)
        return pool

    return db.Database(URL, pool_factory=factory, clock=clock or (lambda: 0.0)), made


# --- 정상 동작 -----------------------------------------------------------------------------
def test_run_returns_rows_commits_and_gives_the_connection_back():
    conn = FakeConn()
    database, pools = make_db(FakePool([conn]))
    assert database.run("select 1", None, "all") == [{"n": 1}, {"n": 2}]
    assert database.run("select 1", None, "one") == {"n": 1}
    assert database.run("update x set y = 1", None, None) is None
    assert conn.commits == 3 and len(pools[0].returned) == 3 and database.pool_resets == 0


def test_values_are_passed_as_parameters_not_pasted_into_the_query():
    conn = FakeConn()
    database, _ = make_db(FakePool([conn]))
    database.run("select * from users where email = %s", ("x'; drop table users; --",), "one")
    query, params = conn.executed[-1]
    assert "drop table" not in query and params == ("x'; drop table users; --",)


# --- 끊어진 연결 복구 ---------------------------------------------------------------------
def test_a_dead_connection_makes_the_pool_rebuild_and_the_query_succeeds_on_retry():
    dead, fresh = FakeConn(dead=True), FakeConn()
    database, pools = make_db(FakePool([dead]), FakePool([fresh]))
    assert database.run("select 1") == [{"n": 1}, {"n": 2}]
    assert database.pool_resets == 1 and pools[0].closed_all is True     # 풀 전체를 새로 만들었다
    assert pools[0].returned and pools[0].returned[0][1] is True         # 죽은 연결은 풀에 되돌리지 않고 버렸다
    assert fresh.commits == 1


def test_a_connection_idle_too_long_is_checked_first_and_replaced_if_dead():
    now = [1000.0]
    stale, fresh = FakeConn(dead=True), FakeConn()
    database, pools = make_db(FakePool([stale]), FakePool([fresh]), clock=lambda: now[0])
    database._last_used = 1000.0 - db.IDLE_CHECK_SEC - 5                  # 앱이 자는 동안 오래 안 쓴 연결
    assert database.run("select 1") == [{"n": 1}, {"n": 2}]
    assert stale.executed[0][0] == "select 1"                            # 쓰기 전에 확인 쿼리를 먼저 보냈다
    assert database.pool_resets == 1 and fresh.commits == 1


def test_a_recently_used_connection_is_not_pinged_again():
    now = [1000.0]
    conn = FakeConn()
    database, _ = make_db(FakePool([conn]), clock=lambda: now[0])
    database._last_used = now[0]                                         # 방금 쓴 연결로 시작(처음 쓰는 연결은 확인을 먼저 한다)
    database.run("select 1")
    now[0] += 5
    database.run("select 2")
    assert [q for q, _ in conn.executed] == ["select 1", "select 2"]    # 확인 쿼리가 끼어들지 않았다


def test_an_idle_but_healthy_connection_is_kept():
    now = [1000.0]
    conn = FakeConn()
    database, _ = make_db(FakePool([conn]), clock=lambda: now[0])
    database._last_used = 1000.0 - 3600
    database.run("select 1")
    assert database.pool_resets == 0 and [q for q, _ in conn.executed][0] == "select 1"


def test_when_the_database_stays_down_a_clear_message_is_raised():
    database, _ = make_db(FakePool([FakeConn(dead=True)]), FakePool([FakeConn(dead=True)]), FakePool([FakeConn(dead=True)]))
    with pytest.raises(db.StorageError) as e:
        database.run("select 1")
    assert str(e.value) == db.STORAGE_DOWN_MESSAGE
    assert "s3cr3t" not in str(e.value) and "supabase.com" not in str(e.value)


def test_a_connection_that_fails_to_open_is_a_storage_error_without_secrets():
    class BrokenPool:
        def getconn(self):
            raise psycopg2.OperationalError(f'connection to server failed: password authentication failed for "{URL}"')

        def closeall(self):
            pass

    database, _ = make_db(BrokenPool(), BrokenPool(), BrokenPool())
    with pytest.raises(db.StorageError) as e:
        database.run("select 1")
    assert "s3cr3t" not in str(e.value) and "postgres." not in str(e.value)


# --- 연결 문제가 아닌 오류 ---------------------------------------------------------------
def test_a_constraint_error_is_not_treated_as_a_dead_connection_and_is_rolled_back():
    conn = FakeConn(raise_on=lambda q: psycopg2.errors.UniqueViolation("duplicate key") if "insert" in q else None)
    database, _ = make_db(FakePool([conn]))
    with pytest.raises(psycopg2.errors.UniqueViolation):
        database.run("insert into x values (1)")
    assert conn.rollbacks == 1 and database.pool_resets == 0 and conn.commits == 0


def test_a_failed_transaction_in_the_middle_is_rolled_back_and_not_retried():
    conn = FakeConn()
    database, _ = make_db(FakePool([conn]))
    with pytest.raises(RuntimeError):
        with database.transaction() as cur:
            cur.execute("insert into a values (1)")
            raise RuntimeError("코드 오류")
    assert conn.rollbacks == 1 and conn.commits == 0


# --- 접속 설정 --------------------------------------------------------------------------
def test_connection_settings_detect_dead_connections_quickly():
    assert db.CONNECT_KWARGS["tcp_user_timeout"] <= 10_000                # 끊긴 연결을 11분이 아니라 몇 초 안에 알아챈다
    assert db.CONNECT_KWARGS["connect_timeout"] <= 15 and db.CONNECT_KWARGS["keepalives"] == 1


def test_redact_hides_the_url_and_the_password():
    text = f"could not connect using {URL} (password s3cr3t*pass rejected)"
    out = db.redact(text, URL)
    assert "s3cr3t" not in out and "supabase.com" not in out and "<접속 문자열>" in out
    assert "s3cr3t*pass" not in db.redact("bad password s3cr3t*pass", URL)
    assert db.redact("ok", None) == "ok"


def test_database_url_precedence_secret_then_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://env")
    assert db.database_url("postgresql://secret") == "postgresql://secret"
    assert db.database_url(None) == "postgresql://env"


def test_close_closes_the_pool_once():
    pool = FakePool([FakeConn()])
    database, _ = make_db(pool)
    database.run("select 1")
    database.close()
    database.close()
    assert pool.closed_all is True


# --- 연결 풀 소진(동시 작업이 풀 크기를 넘을 때) ------------------------------------------
class LimitedPool:
    """ThreadedConnectionPool 처럼 max 개를 넘게 내주려 하면 기다리지 않고 PoolError 를 내는 가짜 풀."""

    def __init__(self, size, hold=0.0):
        import threading
        self.size, self.hold, self.used, self.peak, self._lock = size, hold, 0, 0, threading.Lock()
        self.closed_all = False

    def getconn(self):
        with self._lock:
            if self.used >= self.size:
                raise psycopg2.pool.PoolError("connection pool exhausted")
            self.used += 1
            self.peak = max(self.peak, self.used)
        pool = self

        class SlowConn(FakeConn):
            def commit(self):
                import time
                time.sleep(pool.hold)  # 질의가 연결을 잠깐 붙잡고 있는 것을 흉내 낸다
                super().commit()

        return SlowConn()

    def putconn(self, conn, close=False):
        with self._lock:
            self.used -= 1

    def closeall(self):
        self.closed_all = True


def test_more_concurrent_queries_than_the_pool_size_wait_their_turn_instead_of_failing():
    import threading
    pool = LimitedPool(size=3, hold=0.05)
    database = db.Database(URL, pool_factory=lambda url: pool, clock=lambda: 0.0, max_conn=3, wait_sec=5)
    errors = []

    def work():
        try:
            database.run("select 1")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and pool.peak <= 3 and pool.used == 0           # 모두 성공했고 연결을 모두 돌려줬다
    assert database.pool_resets == 0


def test_waiting_too_long_for_a_connection_is_a_storage_error_not_a_raw_pool_error():
    pool = LimitedPool(size=1)
    database = db.Database(URL, pool_factory=lambda url: pool, clock=lambda: 0.0, max_conn=1, wait_sec=0.05)
    with database.transaction():                                          # 하나뿐인 연결을 붙잡고 있는 동안
        with pytest.raises(db.StorageError) as e:
            database.run("select 1")
    assert isinstance(e.value, db.StorageBusyError) and str(e.value) == db.STORAGE_BUSY_MESSAGE
    assert database.pool_resets == 0                                      # 붐비는 것은 끊긴 것이 아니므로 풀을 버리지 않는다
    assert database.run("select 1") == [{"n": 1}, {"n": 2}]               # 연결이 돌아오면 다시 쓸 수 있다


def test_a_pool_error_from_the_pool_itself_becomes_a_storage_error():
    class ExhaustedPool(FakePool):
        def getconn(self):
            raise psycopg2.pool.PoolError("connection pool exhausted")

    database, _ = make_db(ExhaustedPool([]), ExhaustedPool([]))
    with pytest.raises(db.StorageBusyError):
        database.run("select 1")
    assert database._slots.acquire(blocking=False)         # 실패해도 연결 자리를 돌려줬다


def test_connection_slots_are_given_back_after_errors():
    conn = FakeConn(raise_on=lambda q: psycopg2.errors.UniqueViolation("duplicate key") if "insert" in q else None)
    database = db.Database(URL, pool_factory=lambda url: FakePool([conn]), clock=lambda: 0.0, max_conn=1, wait_sec=0.05)
    for _ in range(3):                                                    # 자리를 돌려주지 않으면 두 번째부터 기다리다 실패한다
        with pytest.raises(psycopg2.errors.UniqueViolation):
            database.run("insert into x values (1)")
    dead = db.Database(URL, pool_factory=lambda url: FakePool([FakeConn(dead=True)]), clock=lambda: 0.0, max_conn=1, wait_sec=0.05)
    for _ in range(2):
        with pytest.raises(db.StorageError) as e:
            dead.run("select 1")
        assert not isinstance(e.value, db.StorageBusyError)               # 끊긴 연결은 '붐빔'이 아니라 '연결 실패'로 알린다
