# 작성: 2026-10-24 22:53 KST
# 3단계: 사용자·로그인 세션·저장한 레시피의 저장소. (Streamlit 에 의존하지 않는다)
#
# - PostgresRepository: 실제 Supabase(Postgres). 모든 값은 파라미터로 바인딩하고, 레시피 질의는 항상 user_id 로 범위를 제한한다.
# - MemoryRepository: 테스트용 가짜 저장소. 같은 규칙(유일성, 상한, 중복, 소유자 확인)을 지킨다.
#
# 계정은 이메일 하나에 하나만 둔다. 가입 방식(provider)은 password 또는 google 이고 서로 합치지 않는다.
import datetime as dt
import hashlib
import threading

import psycopg2
import psycopg2.errors
import psycopg2.extras
from psycopg2 import sql

import llm
import recipe as recipe_mod

MAX_RECIPES_PER_USER = 200
MAX_MEMO_LEN = 500
MAX_NAME_LEN = 30
PROVIDERS = ("password", "google")


class RepositoryError(llm.ServiceError):
    """사용자에게 그대로 보여 줘도 되는 저장소 오류."""


class DuplicateEmailError(RepositoryError):
    pass


class DuplicateRecipeError(RepositoryError):
    pass


class RecipeLimitError(RepositoryError):
    pass


def recipe_fingerprint(recipe):
    """제목과 재료 구성이 같으면 같은 레시피로 보기 위한 지문. 이름 비교 규칙은 2단계와 같다."""
    names = sorted(recipe_mod.normalize_name(i.get("name", "")) for i in recipe.get("ingredients", []))
    text = recipe_mod.normalize_name(recipe.get("title", "")) + "|" + ",".join(names)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_memo(memo):
    return str(memo or "").strip()[:MAX_MEMO_LEN]


def clean_profile(display_name, default_servings, diet_restrictions, allergies):
    """프로필 입력을 허용 범위로 맞춘다. 알 수 없는 값은 기본값을 쓴다."""
    try:
        servings = max(1, min(10, int(default_servings)))
    except (TypeError, ValueError):
        servings = 2
    return {
        "display_name": str(display_name or "").strip()[:MAX_NAME_LEN],
        "default_servings": servings,
        "diet_restrictions": recipe_mod._clean_list(diet_restrictions),
        "allergies": recipe_mod._clean_list(allergies),
    }


def filter_recipes(rows, query="", min_minutes=None, max_minutes=None, difficulty=None):
    """저장한 레시피 목록을 제목 검색·시간·난이도로 거른다. 두 저장소가 같은 규칙을 쓴다."""
    q = (query or "").strip().lower()
    out = []
    for row in rows:
        r = row["recipe"]
        minutes = r.get("time_minutes")
        if q and q not in row["title"].lower():
            continue
        if max_minutes and (minutes is None or minutes > max_minutes):
            continue
        if min_minutes and (minutes is None or minutes < min_minutes):
            continue
        if difficulty and r.get("difficulty") != difficulty:
            continue
        out.append(row)
    return out


# --- 스키마 -----------------------------------------------------------------------------
def schema_statements(schema):
    """app 스키마와 테이블을 만드는 SQL. 여러 번 실행해도 안전하다(이미 있으면 건너뜀)."""
    s = sql.Identifier(schema)
    fmt = lambda text: sql.SQL(text).format(s=s)  # noqa: E731
    return [
        fmt("create schema if not exists {s}"),
        fmt("""create table if not exists {s}.users (
            id bigint generated always as identity primary key,
            email text not null unique,
            provider text not null check (provider in ('password', 'google')),
            password_hash text,
            google_sub text unique,
            display_name text not null default '',
            default_servings integer not null default 2 check (default_servings between 1 and 10),
            diet_restrictions jsonb not null default '[]'::jsonb,
            allergies jsonb not null default '[]'::jsonb,
            created_at timestamptz not null default now(),
            last_login_at timestamptz,
            constraint users_password_ck check (provider <> 'password' or password_hash is not null)
        )"""),
        fmt("""create table if not exists {s}.auth_sessions (
            token_hash text primary key,
            user_id bigint not null references {s}.users (id) on delete cascade,
            created_at timestamptz not null default now(),
            expires_at timestamptz not null
        )"""),
        fmt("create index if not exists auth_sessions_user_idx on {s}.auth_sessions (user_id)"),
        fmt("""create table if not exists {s}.saved_recipes (
            id bigint generated always as identity primary key,
            user_id bigint not null references {s}.users (id) on delete cascade,
            title text not null,
            fingerprint text not null,
            recipe jsonb not null,
            pantry jsonb not null default '[]'::jsonb,
            memo text not null default '',
            created_at timestamptz not null default now(),
            unique (user_id, fingerprint)
        )"""),
        fmt("create index if not exists saved_recipes_user_idx on {s}.saved_recipes (user_id, created_at desc)"),
        # 행 수준 보안: 정책을 만들지 않으므로 Supabase 의 공개 데이터 API(anon·authenticated)로는 아무것도 읽을 수 없다.
        # 앱은 테이블 소유자(postgres)로 직접 접속하므로 영향이 없다.
        fmt("alter table {s}.users enable row level security"),
        fmt("alter table {s}.auth_sessions enable row level security"),
        fmt("alter table {s}.saved_recipes enable row level security"),
        # 공개 API 역할의 권한도 걷어 낸다(역할이 있는 Supabase 에서만).
        sql.SQL(
            "do $$ declare r text; begin "
            "foreach r in array array['anon', 'authenticated'] loop "
            "if exists (select 1 from pg_roles where rolname = r) then "
            "execute format('revoke all on all tables in schema %I from %I', {lit}, r); "
            "execute format('revoke all on schema %I from %I', {lit}, r); "
            "end if; end loop; end $$"
        ).format(lit=sql.Literal(schema)),
    ]


class PostgresRepository:
    def __init__(self, db, schema="app"):
        self.db = db
        self.schema = schema
        self._ready = False
        self._lock = threading.Lock()

    def q(self, text):
        return sql.SQL(text).format(s=sql.Identifier(self.schema))

    def init(self):
        """스키마와 테이블이 없으면 만든다. 프로세스마다 한 번만 실행한다."""
        with self._lock:
            if self._ready:
                return
            with self.db.transaction() as cur:
                for stmt in schema_statements(self.schema):
                    cur.execute(stmt)
            self._ready = True

    # --- 사용자 ---
    USER_COLS = "id, email, provider, display_name, default_servings, diet_restrictions, allergies, created_at, last_login_at"

    def create_user(self, email, provider, display_name="", password_hash=None, google_sub=None):
        try:
            row = self.db.run(
                self.q("insert into {s}.users (email, provider, display_name, password_hash, google_sub) "
                       "values (%s, %s, %s, %s, %s) returning " + self.USER_COLS),
                (email, provider, str(display_name or "")[:MAX_NAME_LEN], password_hash, google_sub), "one")
        except psycopg2.errors.UniqueViolation:
            raise DuplicateEmailError("이미 가입된 이메일입니다.") from None
        return row

    def get_user_by_email(self, email):
        return self.db.run(self.q("select " + self.USER_COLS + " from {s}.users where email = %s"), (email,), "one")

    def get_credentials(self, email):
        return self.db.run(
            self.q("select id, provider, password_hash, google_sub from {s}.users where email = %s"), (email,), "one")

    def get_user(self, user_id):
        return self.db.run(self.q("select " + self.USER_COLS + " from {s}.users where id = %s"), (user_id,), "one")

    def update_profile(self, user_id, display_name, default_servings, diet_restrictions, allergies):
        p = clean_profile(display_name, default_servings, diet_restrictions, allergies)
        return self.db.run(
            self.q("update {s}.users set display_name = %s, default_servings = %s, diet_restrictions = %s, "
                   "allergies = %s where id = %s returning " + self.USER_COLS),
            (p["display_name"], p["default_servings"], psycopg2.extras.Json(p["diet_restrictions"]),
             psycopg2.extras.Json(p["allergies"]), user_id), "one")

    def touch_login(self, user_id):
        self.db.run(self.q("update {s}.users set last_login_at = now() where id = %s"), (user_id,), None)

    def delete_user(self, user_id):
        row = self.db.run(self.q("delete from {s}.users where id = %s returning id"), (user_id,), "one")
        return row is not None

    # --- 로그인 세션 ---
    def create_session(self, user_id, token_hash, expires_at):
        self.db.run(self.q("insert into {s}.auth_sessions (token_hash, user_id, expires_at) values (%s, %s, %s)"),
                    (token_hash, user_id, expires_at), None)

    def user_for_session(self, token_hash):
        return self.db.run(
            self.q("select " + ", ".join("u." + c.strip() for c in self.USER_COLS.split(","))
                   + " from {s}.auth_sessions s join {s}.users u on u.id = s.user_id "
                   "where s.token_hash = %s and s.expires_at > now()"), (token_hash,), "one")

    def delete_session(self, token_hash):
        self.db.run(self.q("delete from {s}.auth_sessions where token_hash = %s"), (token_hash,), None)

    def delete_sessions_of(self, user_id):
        self.db.run(self.q("delete from {s}.auth_sessions where user_id = %s"), (user_id,), None)

    def purge_expired_sessions(self):
        self.db.run(self.q("delete from {s}.auth_sessions where expires_at <= now()"), None, None)

    # --- 저장한 레시피 (모든 질의가 user_id 로 범위를 제한한다) ---
    def count_recipes(self, user_id):
        return self.db.run(self.q("select count(*) as n from {s}.saved_recipes where user_id = %s"), (user_id,), "one")["n"]

    def recipe_fingerprints(self, user_id):
        rows = self.db.run(self.q("select fingerprint from {s}.saved_recipes where user_id = %s"), (user_id,))
        return {r["fingerprint"] for r in rows}

    def save_recipe(self, user_id, recipe, pantry):
        if self.count_recipes(user_id) >= MAX_RECIPES_PER_USER:
            raise RecipeLimitError(f"저장한 레시피가 {MAX_RECIPES_PER_USER}개예요. 오래된 레시피를 지운 뒤 저장해 주세요.")
        try:
            row = self.db.run(
                self.q("insert into {s}.saved_recipes (user_id, title, fingerprint, recipe, pantry) "
                       "values (%s, %s, %s, %s, %s) returning id"),
                (user_id, recipe["title"], recipe_fingerprint(recipe), psycopg2.extras.Json(recipe),
                 psycopg2.extras.Json(list(pantry))), "one")
        except psycopg2.errors.UniqueViolation:
            raise DuplicateRecipeError("이미 저장한 레시피예요.") from None
        return row["id"]

    def list_recipes(self, user_id, query="", min_minutes=None, max_minutes=None, difficulty=None):
        rows = self.db.run(
            self.q("select id, title, recipe, pantry, memo, created_at from {s}.saved_recipes "
                   "where user_id = %s order by created_at desc, id desc"), (user_id,))
        return filter_recipes(rows, query, min_minutes, max_minutes, difficulty)

    def update_memo(self, user_id, recipe_id, memo):
        row = self.db.run(self.q("update {s}.saved_recipes set memo = %s where id = %s and user_id = %s returning id"),
                          (clean_memo(memo), recipe_id, user_id), "one")
        return row is not None

    def delete_recipe(self, user_id, recipe_id):
        row = self.db.run(self.q("delete from {s}.saved_recipes where id = %s and user_id = %s returning id"),
                          (recipe_id, user_id), "one")
        return row is not None


class MemoryRepository:
    """테스트용 저장소. PostgresRepository 와 같은 규칙을 메모리에서 지킨다."""

    def __init__(self, now=None):
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.users, self.sessions, self.recipes = {}, {}, {}
        self._user_seq = self._recipe_seq = 0
        self.fail = None  # 테스트에서 저장소 장애를 흉내 낼 때 예외를 넣는다

    def _check(self):
        if self.fail:
            raise self.fail

    def init(self):
        self._check()

    @staticmethod
    def _public(u):
        return {k: v for k, v in u.items() if k not in ("password_hash", "google_sub")}

    def create_user(self, email, provider, display_name="", password_hash=None, google_sub=None):
        self._check()
        if any(u["email"] == email for u in self.users.values()):
            raise DuplicateEmailError("이미 가입된 이메일입니다.")
        # 실제 DB 의 google_sub unique 제약과 같게 한다(테스트가 구글 이메일 변경 상황을 재현할 수 있게).
        if google_sub and any(u["google_sub"] == google_sub for u in self.users.values()):
            raise DuplicateEmailError("이미 가입된 이메일입니다.")
        if provider not in PROVIDERS or (provider == "password" and not password_hash):
            raise ValueError("잘못된 계정 정보")
        self._user_seq += 1
        user = {"id": self._user_seq, "email": email, "provider": provider, "display_name": str(display_name or "")[:MAX_NAME_LEN],
                "default_servings": 2, "diet_restrictions": [], "allergies": [], "created_at": self._now(),
                "last_login_at": None, "password_hash": password_hash, "google_sub": google_sub}
        self.users[user["id"]] = user
        return self._public(user)

    def _by_email(self, email):
        return next((u for u in self.users.values() if u["email"] == email), None)

    def get_user_by_email(self, email):
        self._check()
        u = self._by_email(email)
        return self._public(u) if u else None

    def get_credentials(self, email):
        self._check()
        u = self._by_email(email)
        return {k: u[k] for k in ("id", "provider", "password_hash", "google_sub")} if u else None

    def get_user(self, user_id):
        self._check()
        u = self.users.get(user_id)
        return self._public(u) if u else None

    def update_profile(self, user_id, display_name, default_servings, diet_restrictions, allergies):
        self._check()
        u = self.users.get(user_id)
        if not u:
            return None
        u.update(clean_profile(display_name, default_servings, diet_restrictions, allergies))
        return self._public(u)

    def touch_login(self, user_id):
        self._check()
        if user_id in self.users:
            self.users[user_id]["last_login_at"] = self._now()

    def delete_user(self, user_id):
        self._check()
        if user_id not in self.users:
            return False
        del self.users[user_id]
        self.sessions = {h: s for h, s in self.sessions.items() if s["user_id"] != user_id}
        self.recipes = {i: r for i, r in self.recipes.items() if r["user_id"] != user_id}
        return True

    def create_session(self, user_id, token_hash, expires_at):
        self._check()
        self.sessions[token_hash] = {"user_id": user_id, "expires_at": expires_at}

    def user_for_session(self, token_hash):
        self._check()
        s = self.sessions.get(token_hash)
        if not s or s["expires_at"] <= self._now() or s["user_id"] not in self.users:
            return None
        return self._public(self.users[s["user_id"]])

    def delete_session(self, token_hash):
        self._check()
        self.sessions.pop(token_hash, None)

    def delete_sessions_of(self, user_id):
        self._check()
        self.sessions = {h: s for h, s in self.sessions.items() if s["user_id"] != user_id}

    def purge_expired_sessions(self):
        self._check()
        self.sessions = {h: s for h, s in self.sessions.items() if s["expires_at"] > self._now()}

    def _mine(self, user_id):
        return [r for r in self.recipes.values() if r["user_id"] == user_id]

    def count_recipes(self, user_id):
        self._check()
        return len(self._mine(user_id))

    def recipe_fingerprints(self, user_id):
        self._check()
        return {r["fingerprint"] for r in self._mine(user_id)}

    def save_recipe(self, user_id, recipe, pantry):
        self._check()
        if self.count_recipes(user_id) >= MAX_RECIPES_PER_USER:
            raise RecipeLimitError(f"저장한 레시피가 {MAX_RECIPES_PER_USER}개예요. 오래된 레시피를 지운 뒤 저장해 주세요.")
        fp = recipe_fingerprint(recipe)
        if fp in self.recipe_fingerprints(user_id):
            raise DuplicateRecipeError("이미 저장한 레시피예요.")
        self._recipe_seq += 1
        self.recipes[self._recipe_seq] = {"id": self._recipe_seq, "user_id": user_id, "title": recipe["title"], "fingerprint": fp,
                                          "recipe": recipe, "pantry": list(pantry), "memo": "", "created_at": self._now()}
        return self._recipe_seq

    def list_recipes(self, user_id, query="", min_minutes=None, max_minutes=None, difficulty=None):
        self._check()
        rows = sorted(self._mine(user_id), key=lambda r: (r["created_at"], r["id"]), reverse=True)
        rows = [{k: r[k] for k in ("id", "title", "recipe", "pantry", "memo", "created_at")} for r in rows]
        return filter_recipes(rows, query, min_minutes, max_minutes, difficulty)

    def update_memo(self, user_id, recipe_id, memo):
        self._check()
        r = self.recipes.get(recipe_id)
        if not r or r["user_id"] != user_id:
            return False
        r["memo"] = clean_memo(memo)
        return True

    def delete_recipe(self, user_id, recipe_id):
        self._check()
        r = self.recipes.get(recipe_id)
        if not r or r["user_id"] != user_id:
            return False
        del self.recipes[recipe_id]
        return True
