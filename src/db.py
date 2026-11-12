# 작성: 2026-10-24 22:53 KST
# 3단계: Supabase(Postgres) 접속 계층. 연결 풀과 끊어진 연결 복구를 맡는다. (Streamlit 에 의존하지 않는다)
#
# Streamlit Community Cloud 는 접속이 없으면 앱을 재우는데, 자는 동안 풀 안의 DB 연결이 끊긴다.
# 풀은 그것을 모르고 죽은 연결을 그대로 내주므로, 오래 안 쓴 연결은 꺼낼 때 확인 쿼리를 보내고
# 죽었으면 풀을 새로 만든다. 연결 시간 제한도 짧게 둬서 끊긴 연결을 11분이 아니라 몇 초 안에 알아챈다.
import contextlib
import logging
import os
import re
import threading
import time

import psycopg2
import psycopg2.extras
import psycopg2.pool

import llm

logger = logging.getLogger("fridge_db")

STORAGE_DOWN_MESSAGE = (
    "저장소(DB)에 연결하지 못했어요. 잠시 후 다시 시도해 주세요. "
    "로그인과 저장이 아닌 재료 인식과 레시피 추천은 그대로 쓸 수 있어요."
)
STORAGE_BUSY_MESSAGE = (
    "저장소(DB)에 요청이 몰려 있어요. 잠시 후 다시 시도해 주세요. "
    "로그인과 저장이 아닌 재료 인식과 레시피 추천은 그대로 쓸 수 있어요."
)
# 풀이 동시에 내줄 수 있는 연결 수. 이보다 많은 작업이 몰리면 바로 실패하지 않고 연결이 돌아올 때까지 기다린다.
# (Supabase Session pooler 의 무료 연결 수를 넘지 않도록 크게 늘리지 않는다)
POOL_MIN = 1
POOL_MAX = 5
# 연결이 모두 쓰이는 중일 때 기다리는 최대 시간(초). 넘으면 StorageBusyError 로 알린다.
POOL_WAIT_SEC = 10
# 이 시간(초)보다 오래 안 쓴 연결은 꺼낼 때 먼저 확인 쿼리를 보낸다.
IDLE_CHECK_SEC = 30
CONNECT_KWARGS = {
    "connect_timeout": 10,
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "tcp_user_timeout": 8000,  # 밀리초. 끊긴 연결을 8초 안에 알아챈다
}
CONNECTION_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)


class StorageError(llm.ServiceError):
    """저장소를 쓸 수 없을 때. 메시지는 사용자에게 그대로 보여 줘도 되고, 접속 정보는 담지 않는다."""


class StorageBusyError(StorageError):
    """연결이 모두 쓰이는 중이라 기다려도 차례가 오지 않았다. 연결이 끊긴 것이 아니므로 풀을 새로 만들지 않는다."""


def database_url(secret=None):
    """접속 문자열을 찾는다. 배포 환경의 Secrets 값(secret) → 환경변수 → 프로젝트 루트 .env 순서다."""
    if secret:
        return secret
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, ".env"), encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    return line.split("=", 1)[1].strip().strip("\"'") or None
    except OSError:
        pass
    return None


def redact(text, url):
    """오류 문구에서 접속 문자열과 비밀번호를 가린다. 로그에도 접속 정보를 남기지 않기 위해서다."""
    out = str(text)
    if url:
        out = out.replace(url, "<접속 문자열>")
        m = re.match(r"^[^:]+://[^:]+:(.+)@[^@/]+/", url)
        if m:
            out = out.replace(m.group(1), "<비밀번호>")
    return out


def _default_pool(url):
    return psycopg2.pool.ThreadedConnectionPool(POOL_MIN, POOL_MAX, url, **CONNECT_KWARGS)


class Database:
    """연결 풀과 질의 실행. 모든 값은 파라미터로 바인딩하고, 연결이 끊겼으면 풀을 새로 만들어 한 번 다시 시도한다."""

    def __init__(self, url, pool_factory=_default_pool, clock=time.monotonic, max_conn=POOL_MAX, wait_sec=POOL_WAIT_SEC):
        self.url = url
        self._pool_factory = pool_factory
        self._clock = clock
        self._lock = threading.Lock()
        # 풀(ThreadedConnectionPool)은 연결이 모두 쓰이는 중이면 기다리지 않고 PoolError 를 낸다.
        # 그래서 꺼내기 전에 이 세마포어로 자리를 먼저 잡아, 넘치는 작업은 연결이 돌아올 때까지 기다리게 한다.
        self._slots = threading.BoundedSemaphore(max_conn)
        self._wait_sec = wait_sec
        self._pool = None
        self._last_used = 0.0
        self.pool_resets = 0  # 풀을 새로 만든 횟수(점검·테스트용)

    # --- 풀 관리 ---
    def _get_pool(self):
        with self._lock:
            if self._pool is None:
                self._pool = self._pool_factory(self.url)
            return self._pool

    def reset_pool(self):
        """풀 안의 연결은 자는 동안 모두 죽었을 수 있으므로 하나만 바꾸지 않고 풀 전체를 새로 만든다."""
        with self._lock:
            old, self._pool = self._pool, None
            self.pool_resets += 1
        if old is not None:
            with contextlib.suppress(Exception):
                old.closeall()

    def close(self):
        with self._lock:
            old, self._pool = self._pool, None
        if old is not None:
            with contextlib.suppress(Exception):
                old.closeall()

    def _checkout(self):
        """살아 있는 연결을 꺼낸다. 오래 안 쓴 연결은 확인 쿼리를 보내고, 죽었으면 풀을 새로 만든다.

        먼저 연결 자리(세마포어)를 잡는다. 자리를 잡았으면 반드시 _release 나 여기서의 실패 처리로 돌려준다.
        """
        if not self._slots.acquire(timeout=self._wait_sec):
            logger.warning("DB 연결 대기 시간 초과(%s초)", self._wait_sec)
            raise StorageBusyError(STORAGE_BUSY_MESSAGE)
        try:
            return self._checkout_conn()
        except BaseException:
            self._slots.release()
            raise

    def _checkout_conn(self):
        pool_error = False
        for attempt in range(2):
            pool = self._get_pool()
            try:
                conn = pool.getconn()
            except psycopg2.pool.PoolError as e:
                # 다른 작업이 풀을 새로 만들며 닫은 직후이거나(closed), 자리 계산이 어긋나 풀이 비었을 때(exhausted).
                # 연결 오류가 아니므로 풀을 버리지 않고, 새 풀을 한 번 더 받아 본 뒤 그래도 안 되면 알린다.
                # PoolError 문구에는 접속 정보가 없다("connection pool exhausted" 등).
                logger.warning("DB 풀에서 연결을 받지 못함: %s", e)
                pool_error = True
                continue
            pool_error = False
            idle = self._clock() - self._last_used
            if idle < IDLE_CHECK_SEC and not conn.closed:
                return pool, conn
            try:
                with conn.cursor() as cur:
                    cur.execute("select 1")
                conn.rollback()
                return pool, conn
            except CONNECTION_ERRORS:
                with contextlib.suppress(Exception):
                    pool.putconn(conn, close=True)
                self.reset_pool()
        if pool_error:
            raise StorageBusyError(STORAGE_BUSY_MESSAGE)
        raise StorageError(STORAGE_DOWN_MESSAGE)

    def _release(self, pool, conn, broken=False):
        self._last_used = self._clock()
        try:
            with contextlib.suppress(Exception):
                pool.putconn(conn, close=broken)
        finally:
            self._slots.release()

    # --- 실행 ---
    @contextlib.contextmanager
    def transaction(self):
        """한 트랜잭션으로 묶어 실행할 커서를 준다. 예외가 나면 되돌린다."""
        try:
            pool, conn = self._checkout()
        except CONNECTION_ERRORS as e:
            logger.error("DB 연결 실패: %s", redact(e, self.url))
            raise StorageError(STORAGE_DOWN_MESSAGE) from None
        broken = False
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield cur
            conn.commit()
        except CONNECTION_ERRORS as e:
            broken = True
            self.reset_pool()
            logger.error("DB 연결 오류: %s", redact(e, self.url))
            raise StorageError(STORAGE_DOWN_MESSAGE) from None
        except BaseException:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise
        finally:
            self._release(pool, conn, broken)

    def run(self, query, params=None, fetch="all"):
        """질의 하나를 실행한다. fetch 는 "all"(목록), "one"(한 행 또는 None), None(결과 없음)이다.

        연결이 끊겨 실패하면 풀을 새로 만들고 한 번 다시 시도한다. 그래도 실패하면 StorageError 다.
        """
        for attempt in range(2):
            try:
                with self.transaction() as cur:
                    cur.execute(query, params)
                    if fetch == "all":
                        return [dict(r) for r in cur.fetchall()]
                    if fetch == "one":
                        row = cur.fetchone()
                        return dict(row) if row else None
                    return None
            except StorageBusyError:
                raise  # 연결이 끊긴 것이 아니라 붐비는 것이다. 이미 기다렸으므로 다시 기다리지 않는다
            except StorageError:
                if attempt == 1:
                    raise
        raise StorageError(STORAGE_DOWN_MESSAGE)  # 도달하지 않는다(안전장치)
