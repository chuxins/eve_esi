"""MySQL 数据库访问层。

持久化：
- characters     角色表（支持多角色）
- oauth_tokens   OAuth token（每角色一条）
- wallet_journal 钱包变动流水（按 ref_id 去重）
- wallet_balance 钱包余额历史快照
- wallet_transactions 市场交易详情（按 transaction_id 去重）
- item_types       EVE 物品类型表（type_id -> 名称）

数据库连接配置来自 config.json 的 "db" 字段。

时区约定：所有时间列（journal_date / date / recorded_at / created_at）
统一存储为 UTC+8（北京时间）的裸时间，展示层无需再做换算。
"""

import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pymysql
from pymysql.cursors import DictCursor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

# 全库统一使用的时区（北京时间 UTC+8）
BEIJING_TZ = timezone(timedelta(hours=8))


class DatabaseError(RuntimeError):
    """数据库操作相关错误。"""


def _to_mysql_datetime(value):
    """将 ESI 的 ISO 8601（UTC）时间转换为 UTC+8 的 MySQL DATETIME 字符串。

    例如 '2026-08-12T15:28:21Z' -> '2026-08-12 23:28:21'
    支持 'T'/'Z'、'+00:00' 后缀及毫秒；无时区信息的字符串按 UTC 处理。
    """
    if not value:
        return None
    s = str(value).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        # 兜底：无法解析时按原样截断（历史兼容）
        return s.replace("T", " ")[:19]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


_SCHEMA_CACHE = None

# 表已存在时 CREATE TABLE IF NOT EXISTS 不会补列，这里做幂等的增量迁移
_COLUMN_MIGRATIONS = (
    ("item_types", "name_en",
     "ALTER TABLE item_types ADD COLUMN name_en VARCHAR(255) NULL AFTER name"),
    ("item_types", "name_norm",
     "ALTER TABLE item_types ADD COLUMN name_norm VARCHAR(512) NULL AFTER name_en"),
)


def normalize_item_name(name):
    """规范化物品名：只保留字母/数字/汉字并转小写，用于中英文模糊匹配。

    例：'三钛 合金' / '125mm Railgun I' -> '三钛合金' / '125mmrailguni'。
    非字母数字字符（空格、连字符、撇号、LIKE 通配符等）全部丢弃。
    """
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


def build_item_name_norm(*names):
    """把多个名称（中/英）规范化后拼成带分隔符的搜索键，如 '|三钛合金|tritanium|'。

    前后都加分隔符，使「精确匹配 = LIKE '%|名字|%'」与「模糊匹配 = LIKE '%名字%'」
    能用同一个字段完成。
    """
    parts = [normalize_item_name(n) for n in names if n]
    parts = [p for p in parts if p]
    if not parts:
        return None
    return "|" + "|".join(parts) + "|"


def _read_schema():
    """读取 schema.sql，返回 (可执行语句列表, 表名列表)，结果进程内缓存。"""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            sql = f.read()
        statements = []
        for part in sql.split(";"):
            stmt = "\n".join(
                line for line in part.splitlines() if not line.strip().startswith("--")
            ).strip()
            if stmt:
                statements.append(stmt)
        tables = re.findall(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`?(\w+)`?", sql, re.IGNORECASE
        )
        _SCHEMA_CACHE = (statements, tables)
    return _SCHEMA_CACHE


class Database:
    """数据库访问对象：线程安全，每个线程复用一个连接。

    - 连接复用：避免每次查询都重新握手；空闲断线由 ping 自动重连。
    - 批量写入：多行合并为单条 INSERT，配合显式事务减少往返与提交次数。
    """

    _BATCH_SIZE = 500  # 单次 executemany 的最大行数

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
            # 固定会话时区为 UTC+8，使 CURRENT_TIMESTAMP 与服务端 time_zone 无关
            "init_command": "SET time_zone = '+08:00'",
        }
        self._local = threading.local()
        self.ensure_schema()

    # ------------------------------------------------------------ 连接管理

    def _conn(self):
        """返回当前线程复用的连接（先心跳探测，断线则重连）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.ping(reconnect=True)
                return conn
            except pymysql.MySQLError:
                self.close()
        try:
            conn = pymysql.connect(**self.config)
        except pymysql.MySQLError as exc:
            raise DatabaseError(f"数据库连接失败：{exc}") from exc
        self._local.conn = conn
        return conn

    @contextmanager
    def _connect(self):
        """产出当前线程复用的连接（不关闭，供 with 语句使用）。"""
        yield self._conn()

    def close(self):
        """关闭当前线程持有的连接。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except pymysql.MySQLError:
                pass
            self._local.conn = None

    def _executemany(self, sql, rows):
        """分批写入多行（自动合并为单条多值 INSERT + 显式事务）。"""
        if not rows:
            return 0
        conn = self._conn()
        with conn.cursor() as cur:
            conn.begin()
            try:
                for start in range(0, len(rows), self._BATCH_SIZE):
                    cur.executemany(sql, rows[start:start + self._BATCH_SIZE])
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return len(rows)

    # ------------------------------------------------------------ schema 初始化

    def ensure_schema(self):
        """幂等创建所有表并补齐新增列。

        先做一次轻量检查（查 information_schema），表已齐备时跳过 DDL，
        避免每次实例化都执行全量 DDL。
        """
        statements, required = _read_schema()
        conn = self._conn()
        if required:
            placeholders = ",".join(["%s"] * len(required))
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT table_name AS t FROM information_schema.tables "
                    f"WHERE table_schema=%s AND table_name IN ({placeholders})",
                    (self.config["database"], *required),
                )
                present = {str(r["t"]).lower() for r in cur.fetchall()}
            missing = [t for t in required if t.lower() not in present]
            if missing:
                for stmt in statements:
                    with conn.cursor() as cur:
                        cur.execute(stmt)
        self._ensure_columns()

    def _ensure_columns(self):
        """幂等补齐 _COLUMN_MIGRATIONS 中声明的列。"""
        conn = self._conn()
        by_table = {}
        for table, column, ddl in _COLUMN_MIGRATIONS:
            by_table.setdefault(table, []).append((column, ddl))
        for table, columns in by_table.items():
            names = [c for c, _ in columns]
            placeholders = ",".join(["%s"] * len(names))
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT column_name AS c FROM information_schema.columns "
                    f"WHERE table_schema=%s AND table_name=%s "
                    f"AND column_name IN ({placeholders})",
                    (self.config["database"], table, *names),
                )
                present = {str(r["c"]).lower() for r in cur.fetchall()}
            for column, ddl in columns:
                if column.lower() in present:
                    continue
                with conn.cursor() as cur:
                    cur.execute(ddl)
                print(f"  [db] 已补充列 {table}.{column}")

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
        rows = [
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
            )
            for e in entries
        ]
        return self._executemany(
            """INSERT INTO wallet_journal
               (character_id, ref_id, journal_date, amount, balance, description,
                first_party_id, second_party_id, tax, reason, context_id, context_id_type)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE journal_date = VALUES(journal_date)""",
            rows,
        )

    def get_max_journal_ref_id(self, character_id):
        """返回该角色已入库的最大流水 ID；无记录时返回 None。

        用于增量同步：只向 ESI 索取比该 ID 更新的流水。
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(ref_id) AS max_ref FROM wallet_journal WHERE character_id=%s",
                    (character_id,),
                )
                row = cur.fetchone()
                return int(row["max_ref"]) if row and row["max_ref"] is not None else None

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

    # ------------------------------------------------------------ wallet transactions

    def upsert_wallet_transactions(self, character_id, entries):
        """批量写入市场交易详情，按 (character_id, transaction_id) 幂等去重。"""
        if not entries:
            return 0
        rows = []
        for e in entries:
            unit_price = float(e.get("unit_price") or 0)
            quantity = int(e.get("quantity") or 0)
            rows.append(
                (
                    character_id,
                    int(e.get("transaction_id")),
                    e.get("journal_ref_id"),
                    e.get("type_id"),
                    e.get("location_id"),
                    e.get("client_id"),
                    _to_mysql_datetime(e.get("date")),
                    int(bool(e.get("is_buy"))),
                    int(bool(e.get("is_personal"))),
                    quantity,
                    unit_price,
                    unit_price * quantity,
                )
            )
        return self._executemany(
            """INSERT INTO wallet_transactions
               (character_id, transaction_id, journal_ref_id, type_id, location_id,
                client_id, date, is_buy, is_personal, quantity, unit_price, total_price)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE
               journal_ref_id = VALUES(journal_ref_id),
               type_id = VALUES(type_id),
               location_id = VALUES(location_id),
               client_id = VALUES(client_id),
               date = VALUES(date),
               is_buy = VALUES(is_buy),
               is_personal = VALUES(is_personal),
               quantity = VALUES(quantity),
               unit_price = VALUES(unit_price),
               total_price = VALUES(total_price)""",
            rows,
        )

    def get_wallet_transactions(self, character_id, limit=50):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT t.*, i.name AS type_name
                       FROM wallet_transactions t
                       LEFT JOIN item_types i ON i.type_id = t.type_id
                       WHERE t.character_id=%s
                       ORDER BY t.date DESC, t.transaction_id DESC
                       LIMIT %s""",
                    (character_id, int(limit)),
                )
                return cur.fetchall()

    def get_wallet_transactions_by_journal_refs(self, character_id, journal_ref_ids):
        """按 journal_ref_id 查询市场交易详情，返回列表。"""
        if not journal_ref_ids:
            return []
        refs = [int(x) for x in journal_ref_ids if x]
        if not refs:
            return []
        placeholders = ",".join(["%s"] * len(refs))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT t.*, i.name AS type_name
                        FROM wallet_transactions t
                        LEFT JOIN item_types i ON i.type_id = t.type_id
                        WHERE t.character_id=%s AND t.journal_ref_id IN ({placeholders})
                        ORDER BY t.date ASC, t.transaction_id ASC""",
                    (character_id, *refs),
                )
                return cur.fetchall()

    def wallet_transactions_count(self, character_id=None):
        with self._connect() as conn:
            with conn.cursor() as cur:
                if character_id:
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM wallet_transactions WHERE character_id=%s",
                        (character_id,),
                    )
                else:
                    cur.execute("SELECT COUNT(*) AS n FROM wallet_transactions")
                return cur.fetchone()["n"]

    # ------------------------------------------------------------ item types

    def upsert_item_types(self, entries):
        """批量写入物品名称（entries 元素：type_id / name / name_en）。

        name 仅在库中原值为空时写入（避免把已有的 ESI 中文名覆盖为 SDE 写法）；
        name_en / name_norm 为模糊查询服务，新值不为空时刷新生效。
        """
        if not entries:
            return 0
        rows = []
        for e in entries:
            name = str(e.get("name") or "")[:255]
            name_en = str(e.get("name_en") or "")[:255]
            rows.append((
                int(e["type_id"]),
                name,
                name_en or None,
                build_item_name_norm(name, name_en),
            ))
        return self._executemany(
            """INSERT INTO item_types (type_id, name, name_en, name_norm)
               VALUES (%s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE
               name = IF(name IS NULL OR name = '', VALUES(name), name),
               name_en = COALESCE(VALUES(name_en), name_en),
               name_norm = CASE
                   WHEN VALUES(name_en) IS NULL AND name_norm IS NOT NULL
                   THEN name_norm
                   ELSE COALESCE(VALUES(name_norm), name_norm)
               END""",
            rows,
        )

    def find_item_type_by_name(self, name):
        """精确匹配物品（中文名 / 英文名 / 规范化后一致），未命中返回 None。"""
        name = (name or "").strip()
        if not name:
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT type_id, name FROM item_types
                       WHERE name = %s OR name_en = %s LIMIT 1""",
                    (name, name),
                )
                row = cur.fetchone()
                if row:
                    return row
                norm = normalize_item_name(name)
                if not norm:
                    return None
                cur.execute(
                    "SELECT type_id, name FROM item_types WHERE name_norm LIKE %s LIMIT 1",
                    (f"%|{norm}|%",),
                )
                return cur.fetchone()

    def search_item_types(self, keyword, limit=10):
        """模糊匹配物品名（中英文皆可），返回按匹配度排序的候选列表。

        排序：名称以关键词开头者优先，其次名称更短者优先（通常是更基础的物品）。
        """
        key = normalize_item_name(keyword)
        if not key:
            return []
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT type_id, name, name_en FROM item_types
                       WHERE name_norm LIKE %s
                       ORDER BY (name_norm LIKE %s) DESC,
                                CHAR_LENGTH(name) ASC, type_id ASC
                       LIMIT %s""",
                    (f"%{key}%", f"%|{key}%", int(limit)),
                )
                return cur.fetchall()

    def get_missing_item_type_ids(self, type_ids):
        """返回 item_types 表中不存在的 type_id 列表。"""
        ids = [int(x) for x in type_ids if x]
        if not ids:
            return []
        placeholders = ",".join(["%s"] * len(ids))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT type_id FROM item_types WHERE type_id IN ({placeholders})",
                    ids,
                )
                existing = {int(r["type_id"]) for r in cur.fetchall()}
        return [tid for tid in ids if tid not in existing]

    def item_types_count(self):
        """返回 (物品总数, 含英文名的数量)。"""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n, COUNT(name_en) AS n_en FROM item_types")
                row = cur.fetchone()
                return int(row["n"]), int(row["n_en"])

    def get_item_type_names_en(self, type_ids):
        """返回 {type_id: 英文名}（无英文名的条目不包含在内）。"""
        ids = [int(x) for x in type_ids if x]
        if not ids:
            return {}
        placeholders = ",".join(["%s"] * len(ids))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT type_id, name_en FROM item_types "
                    f"WHERE name_en IS NOT NULL AND type_id IN ({placeholders})",
                    ids,
                )
                return {int(r["type_id"]): r["name_en"] for r in cur.fetchall()}

    def get_item_type_names(self, type_ids):
        """返回 {type_id: name} 字典。"""
        ids = [int(x) for x in type_ids if x]
        if not ids:
            return {}
        placeholders = ",".join(["%s"] * len(ids))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT type_id, name FROM item_types WHERE type_id IN ({placeholders})",
                    ids,
                )
                return {int(r["type_id"]): r["name"] for r in cur.fetchall()}

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
