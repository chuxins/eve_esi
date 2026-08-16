"""MySQL 数据库访问层。

持久化：
- characters     角色表（支持多角色）
- oauth_tokens   OAuth token（每角色一条）
- wallet_journal 钱包变动流水（按 ref_id 去重）
- wallet_balance 钱包余额历史快照

数据库连接配置来自 config.json 的 "db" 字段。
"""

import os

import pymysql
from pymysql.cursors import DictCursor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")


class DatabaseError(RuntimeError):
    """数据库操作相关错误。"""


def _to_mysql_datetime(value):
    """将 ESI 的 ISO 8601 时间字符串转换为 MySQL DATETIME 格式。

    例如 '2026-08-12T15:28:21Z' -> '2026-08-12 15:28:21'
    支持 'T'/'Z'、'+00:00' 后缀及毫秒。
    """
    if not value:
        return None
    s = str(value).strip()
    s = s.replace("Z", "").replace("z", "").replace("+00:00", "")
    s = s.replace("T", " ")
    if "." in s:
        s = s.split(".")[0]
    return s[:19]


def _schema_statements():
    """读取 schema.sql 并拆分为可执行的 SQL 语句列表（过滤注释行）。"""
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        sql = f.read()
    statements = []
    for part in sql.split(";"):
        stmt = "\n".join(
            line for line in part.splitlines() if not line.strip().startswith("--")
        ).strip()
        if stmt:
            statements.append(stmt)
    return statements


class Database:
    def __init__(self, db_config):
        self.config = {
            "host": db_config.get("host", "127.0.0.1"),
            "port": int(db_config.get("port", 3306)),
            "user": db_config.get("user", "eve_esi"),
            "password": db_config.get("password", ""),
            "database": db_config.get("database", "eve_esi"),
            "charset": "utf8mb4",
            "cursorclass": DictCursor,
            "autocommit": True,
        }
        self.ensure_schema()

    def _connect(self):
        try:
            return pymysql.connect(**self.config)
        except pymysql.MySQLError as exc:
            raise DatabaseError(f"数据库连接失败：{exc}") from exc

    # ------------------------------------------------------------ schema 初始化

    def ensure_schema(self):
        """幂等创建所有表。"""
        for stmt in _schema_statements():
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(stmt)

    # ------------------------------------------------------------ characters

    def upsert_character(self, character_id, character_name, scopes=None):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO characters (character_id, character_name, scopes)
                       VALUES (%s, %s, %s)
                       ON DUPLICATE KEY UPDATE
                       character_name = VALUES(character_name),
                       scopes = VALUES(scopes)""",
                    (character_id, character_name, scopes),
                )

    def get_character(self, character_id):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM characters WHERE character_id=%s", (character_id,)
                )
                return cur.fetchone()

    def list_characters(self):
        """列出所有角色及其 token 状态。"""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT c.character_id, c.character_name, c.scopes, c.created_at,
                              t.access_token IS NOT NULL AS has_token,
                              t.expires_at, t.updated_at AS token_updated_at
                       FROM characters c
                       LEFT JOIN oauth_tokens t ON t.character_id = c.character_id
                       ORDER BY c.character_id"""
                )
                return cur.fetchall()

    def delete_character(self, character_id):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM characters WHERE character_id=%s", (character_id,)
                )

    # ------------------------------------------------------------ tokens

    def upsert_token(self, character_id, token):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO oauth_tokens
                       (character_id, access_token, refresh_token, token_type, expires_at, scope)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE
                       access_token = VALUES(access_token),
                       refresh_token = VALUES(refresh_token),
                       token_type = VALUES(token_type),
                       expires_at = VALUES(expires_at),
                       scope = VALUES(scope)""",
                    (
                        character_id,
                        token.get("access_token"),
                        token.get("refresh_token"),
                        token.get("token_type"),
                        int(token.get("expires_at", 0)),
                        token.get("scope"),
                    ),
                )

    def get_token(self, character_id):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM oauth_tokens WHERE character_id=%s", (character_id,)
                )
                row = cur.fetchone()
                if row:
                    row["expires_at"] = int(row["expires_at"] or 0)
                return row

    def delete_token(self, character_id):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM oauth_tokens WHERE character_id=%s", (character_id,)
                )

    # ------------------------------------------------------------ wallet journal

    def upsert_journal(self, character_id, entries):
        """批量写入钱包流水，按 (character_id, ref_id) 幂等去重。

        注意：新版 ESI journal 条目的唯一标识字段为 ``id``（旧版为
        ``ref_id``），此处兼容两种。
        """
        if not entries:
            return 0
        with self._connect() as conn:
            with conn.cursor() as cur:
                for e in entries:
                    cur.execute(
                        """INSERT INTO wallet_journal
                           (character_id, ref_id, journal_date, amount, balance, description,
                            first_party_id, second_party_id, tax, reason, context_id, context_id_type)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON DUPLICATE KEY UPDATE journal_date = VALUES(journal_date)""",
                        (
                            character_id,
                            e.get("id") or e.get("ref_id"),
                            _to_mysql_datetime(e.get("date")),
                            e.get("amount"),
                            e.get("balance"),
                            (e.get("description") or "")[:512],
                            e.get("first_party_id"),
                            e.get("second_party_id"),
                            e.get("tax"),
                            (e.get("reason") or "")[:512],
                            e.get("context_id"),
                            e.get("context_id_type"),
                        ),
                    )
                return len(entries)

    def get_journal(self, character_id, limit=50):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM wallet_journal
                       WHERE character_id=%s
                       ORDER BY journal_date DESC, id DESC
                       LIMIT %s""",
                    (character_id, int(limit)),
                )
                return cur.fetchall()

    def get_journal_after(self, character_id, ref_id, limit=50):
        """获取 ref_id 大于指定值（上次推送之后）的流水，按时间正序。"""
        with self._connect() as conn:
            with conn.cursor() as cur:
                if ref_id:
                    cur.execute(
                        """SELECT * FROM wallet_journal
                           WHERE character_id=%s AND ref_id > %s
                           ORDER BY journal_date ASC, id ASC
                           LIMIT %s""",
                        (character_id, int(ref_id), int(limit)),
                    )
                else:
                    cur.execute(
                        """SELECT * FROM wallet_journal
                           WHERE character_id=%s
                           ORDER BY journal_date ASC, id ASC
                           LIMIT %s""",
                        (character_id, int(limit)),
                    )
                return cur.fetchall()

    def journal_count(self, character_id=None):
        with self._connect() as conn:
            with conn.cursor() as cur:
                if character_id:
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM wallet_journal WHERE character_id=%s",
                        (character_id,),
                    )
                else:
                    cur.execute("SELECT COUNT(*) AS n FROM wallet_journal")
                return cur.fetchone()["n"]

    # ------------------------------------------------------------ balance 快照

    def record_balance(self, character_id, balance):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO wallet_balance (character_id, balance) VALUES (%s, %s)",
                    (character_id, balance),
                )

    def get_balance_history(self, character_id, limit=50):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM wallet_balance
                       WHERE character_id=%s
                       ORDER BY recorded_at DESC
                       LIMIT %s""",
                    (character_id, int(limit)),
                )
                return cur.fetchall()
