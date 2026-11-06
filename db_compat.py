"""
Database compatibility layer — works with both SQLite (local dev) and
PostgreSQL/Neon (production).

Includes connection pooling for high-performance PostgreSQL connections.
"""

import os
import re
import sqlite3

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool

    _pg_pool = None

    def _get_pg_pool():
        global _pg_pool
        if _pg_pool is None:
            _pg_pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=DATABASE_URL
            )
        return _pg_pool


def _sqlite_to_pg_sql(sql):
    """Translate common SQLite SQL idioms to Postgres."""
    sql = re.sub(
        r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT',
        'SERIAL PRIMARY KEY',
        sql,
        flags=re.IGNORECASE,
    )
    return sql


def _translate_placeholders(sql):
    """Replace SQLite ? placeholders with Postgres %s."""
    result = []
    in_string = False
    quote_char = None
    for ch in sql:
        if in_string:
            result.append(ch)
            if ch == quote_char:
                in_string = False
        else:
            if ch in ("'", '"'):
                in_string = True
                quote_char = ch
                result.append(ch)
            elif ch == '?':
                result.append('%s')
            else:
                result.append(ch)
    return ''.join(result)


class CompatRow:
    """Row wrapper that supports both dict-style and index-style access."""
    def __init__(self, data):
        if isinstance(data, dict):
            self._keys = list(data.keys())
            self._values = list(data.values())
            self._dict = data
        else:
            self._keys = []
            self._values = list(data) if data else []
            self._dict = {}

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._dict[key]

    def __contains__(self, key):
        return key in self._dict

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return self._keys

    def values(self):
        return self._values

    def items(self):
        return zip(self._keys, self._values)

    def get(self, key, default=None):
        return self._dict.get(key, default)


class CompatPgResult:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def lastrowid(self):
        if self._cursor.description:
            try:
                row = self._cursor.fetchone()
                if row and 'id' in row:
                    return row['id']
            except Exception:
                pass
        return None

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        return CompatRow(row)

    def fetchall(self):
        rows = self._cursor.fetchall()
        return [CompatRow(r) for r in rows]


class CompatConnection:
    """Wraps a psycopg2 connection with pooled check-in on close."""

    def __init__(self, pg_conn, pool=None):
        self._conn = pg_conn
        self._pool = pool
        self._closed = False

    def execute(self, sql, params=None):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        translated = _translate_placeholders(sql)
        if translated.strip().upper().startswith('INSERT') and 'RETURNING' not in translated.upper():
            translated = translated.rstrip().rstrip(';') + ' RETURNING id'
        if params:
            cur.execute(translated, params)
        else:
            cur.execute(translated)
        return CompatPgResult(cur)

    def commit(self):
        if not self._closed:
            self._conn.commit()

    def rollback(self):
        if not self._closed:
            self._conn.rollback()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._pool:
            try:
                if self._conn.closed:
                    self._pool.putconn(self._conn, close=True)
                else:
                    self._conn.rollback()  # reset transaction state
                    self._pool.putconn(self._conn)
            except Exception:
                try:
                    self._pool.putconn(self._conn, close=True)
                except Exception:
                    pass
        else:
            try:
                self._conn.close()
            except Exception:
                pass

    def cursor(self, **kwargs):
        return self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def get_db_connection(db_file=None):
    """
    Get a database connection from pool with automated idle SSL reconnect.
    """
    if USE_POSTGRES:
        pool = _get_pg_pool()
        raw_conn = pool.getconn()
        for _ in range(2):
            if raw_conn.closed:
                try:
                    pool.putconn(raw_conn, close=True)
                except Exception:
                    pass
                raw_conn = pool.getconn()
                continue
            try:
                with raw_conn.cursor() as test_cur:
                    test_cur.execute("SELECT 1")
                break
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                try:
                    pool.putconn(raw_conn, close=True)
                except Exception:
                    pass
                raw_conn = pool.getconn()
        return CompatConnection(raw_conn, pool=pool)
    else:
        if db_file is None:
            db_file = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "cvent_events.db"
            )
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn


def adapt_create_table_sql(sql):
    if USE_POSTGRES:
        return _sqlite_to_pg_sql(sql)
    return sql


def safe_alter_add_column(conn, table, column, col_type, default=None):
    if USE_POSTGRES:
        default_clause = f" DEFAULT {default}" if default is not None else ""
        if isinstance(default, str) and default not in ('NULL',):
            default_clause = f" DEFAULT '{default}'"
        try:
            raw = conn._conn if hasattr(conn, '_conn') else conn
            cur = raw.cursor()
            cur.execute("SAVEPOINT alter_col")
            cur.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {col_type}{default_clause}"
            )
            cur.execute("RELEASE SAVEPOINT alter_col")
            raw.commit()
        except Exception:
            raw = conn._conn if hasattr(conn, '_conn') else conn
            try:
                cur2 = raw.cursor()
                cur2.execute("ROLLBACK TO SAVEPOINT alter_col")
                cur2.execute("RELEASE SAVEPOINT alter_col")
            except Exception:
                pass
    else:
        try:
            default_clause = ""
            if default is not None:
                default_clause = f" DEFAULT '{default}'" if isinstance(default, str) else f" DEFAULT {default}"
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {col_type}{default_clause}"
            )
        except Exception:
            pass


def is_postgres():
    return USE_POSTGRES
