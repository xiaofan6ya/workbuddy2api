"""数据库方言适配的单元测试（不依赖真实 PostgreSQL / MySQL 实例）。

为什么要有这组测试
------------------
`admin/db.py` 的方言分支决定「老库升级时迁移列与查询索引建不建得上」。
这几个函数**失败是静默的**（`except: pass` / 只 warning），所以一旦适配写错，
表现不是启动报错，而是「升级后查询依旧很慢」这种极难归因的症状 ——
本仓库历史上就踩过一次（见 `_ensure_index` 的注释）。

这里把 `_DIALECT_NAME` 打桩成各方言，锁住类型映射、标识符引用与生成的
DDL 文本，避免后续改动悄悄退回「只兼容 MySQL」。
"""
import unittest
from unittest import mock

from admin import db as wbdb


class ColumnTypeTests(unittest.TestCase):
    """`column_type()` 的类型名改写。"""

    def test_postgresql_rewrites_mysql_type_names(self):
        with mock.patch.object(wbdb, "_DIALECT_NAME", "postgresql"):
            # PostgreSQL 没有 DATETIME / INT；FLOAT 是单精度而迁移列存倍率
            self.assertEqual(wbdb.column_type("DATETIME"), "TIMESTAMP")
            self.assertEqual(wbdb.column_type("INT"), "INTEGER")
            self.assertEqual(wbdb.column_type("FLOAT"), "DOUBLE PRECISION")

    def test_postgresql_keeps_length_and_unknown_types(self):
        with mock.patch.object(wbdb, "_DIALECT_NAME", "postgresql"):
            self.assertEqual(wbdb.column_type("VARCHAR(64)"), "VARCHAR(64)")
            self.assertEqual(wbdb.column_type("VARCHAR(2048)"), "VARCHAR(2048)")
            self.assertEqual(wbdb.column_type("TEXT"), "TEXT")

    def test_mysql_and_sqlite_keep_names_unchanged(self):
        """MySQL/SQLite 分支必须**原样透传** —— MySQL 是当前线上路径。"""
        for dialect in ("mysql", "sqlite"):
            with mock.patch.object(wbdb, "_DIALECT_NAME", dialect):
                for raw in ("DATETIME", "INT", "FLOAT", "TEXT", "VARCHAR(120)"):
                    self.assertEqual(wbdb.column_type(raw), raw,
                                     f"{dialect} 不应改写 {raw}")

    def test_type_name_matching_is_case_insensitive(self):
        with mock.patch.object(wbdb, "_DIALECT_NAME", "postgresql"):
            self.assertEqual(wbdb.column_type("datetime"), "TIMESTAMP")
            self.assertEqual(wbdb.column_type(" int "), "INTEGER")


class QuoteIdentTests(unittest.TestCase):
    """`quote_ident()` 的方言引用符。"""

    def test_quoting_per_dialect(self):
        cases = (("postgresql", '"'), ("sqlite", '"'), ("mysql", "`"))
        for dialect, quote in cases:
            with mock.patch.object(wbdb, "_DIALECT_NAME", dialect):
                self.assertEqual(wbdb.quote_ident("usage_logs"),
                                 f"{quote}usage_logs{quote}")

    def test_rejects_identifier_outside_whitelist(self):
        """名字是拼进 SQL 的，白名单必须挡住带引号/分号/空格的输入。"""
        bad_names = (
            'x" ; DROP TABLE accounts; --',
            "x`; DROP TABLE accounts; --",
            "has space",
            "1leading_digit",
            "",
            "带中文",
        )
        with mock.patch.object(wbdb, "_DIALECT_NAME", "postgresql"):
            for bad in bad_names:
                with self.assertRaises(ValueError, msg=f"未拦住 {bad!r}"):
                    wbdb.quote_ident(bad)


class GeneratedDdlTests(unittest.TestCase):
    """`add_column_ddl()` / `create_index_ddl()` 生成的 DDL 文本。"""

    def test_add_column_ddl_postgresql(self):
        with mock.patch.object(wbdb, "_DIALECT_NAME", "postgresql"):
            ddl = wbdb.add_column_ddl("accounts", "credits_expiring_soon", "INT", "DEFAULT 0")
        self.assertEqual(
            ddl, 'ALTER TABLE "accounts" ADD COLUMN "credits_expiring_soon" INTEGER DEFAULT 0')

    def test_add_column_ddl_mysql_unchanged(self):
        """MySQL 生成结果必须与改造前逐字一致，否则线上迁移行为会变。"""
        with mock.patch.object(wbdb, "_DIALECT_NAME", "mysql"):
            ddl = wbdb.add_column_ddl("accounts", "credits_expiring_soon", "INT", "DEFAULT 0")
        self.assertEqual(
            ddl, "ALTER TABLE `accounts` ADD COLUMN `credits_expiring_soon` INT DEFAULT 0")

    def test_add_column_ddl_sqlite_uses_double_quotes(self):
        with mock.patch.object(wbdb, "_DIALECT_NAME", "sqlite"):
            ddl = wbdb.add_column_ddl("accounts", "credits_expiring_soon", "INT", "DEFAULT 0")
        self.assertEqual(
            ddl, 'ALTER TABLE "accounts" ADD COLUMN "credits_expiring_soon" INT DEFAULT 0')

    def test_add_column_ddl_without_default(self):
        with mock.patch.object(wbdb, "_DIALECT_NAME", "postgresql"):
            ddl = wbdb.add_column_ddl("schedules", "stop_after", "DATETIME", "NULL")
        self.assertEqual(ddl, 'ALTER TABLE "schedules" ADD COLUMN "stop_after" TIMESTAMP NULL')
        # 空 default 时不应留尾随空格
        with mock.patch.object(wbdb, "_DIALECT_NAME", "postgresql"):
            ddl2 = wbdb.add_column_ddl("api_keys", "key_full", "VARCHAR(2048)")
        self.assertEqual(ddl2, 'ALTER TABLE "api_keys" ADD COLUMN "key_full" VARCHAR(2048)')

    def test_create_index_ddl_per_dialect(self):
        cols = ("account_id", "created_at")
        with mock.patch.object(wbdb, "_DIALECT_NAME", "postgresql"):
            self.assertEqual(
                wbdb.create_index_ddl("usage_logs", "ix_usage_logs_account_created", cols),
                'CREATE INDEX IF NOT EXISTS "ix_usage_logs_account_created" '
                'ON "usage_logs" ("account_id", "created_at")')
        with mock.patch.object(wbdb, "_DIALECT_NAME", "sqlite"):
            self.assertEqual(
                wbdb.create_index_ddl("usage_logs", "ix_usage_logs_account_created", cols),
                'CREATE INDEX IF NOT EXISTS "ix_usage_logs_account_created" '
                'ON "usage_logs" ("account_id", "created_at")')
        with mock.patch.object(wbdb, "_DIALECT_NAME", "mysql"):
            # MySQL 不支持 IF NOT EXISTS，不能带上
            self.assertEqual(
                wbdb.create_index_ddl("usage_logs", "ix_usage_logs_account_created", cols),
                "CREATE INDEX `ix_usage_logs_account_created` "
                "ON `usage_logs` (`account_id`, `created_at`)")


class MigrationTargetsTests(unittest.TestCase):
    """守住迁移清单本身：新增迁移列/索引时这里会失败，提醒同步考虑各方言。"""

    def test_every_migration_column_type_is_supported(self):
        """`_ensure_column` 的调用点只允许用约定过的类型名。

        新增类型名时必须同时确认：MySQL/SQLite 能直接执行、PostgreSQL 要么
        原生支持、要么已在 `_TYPE_ALIASES` 里登记。
        """
        import re
        import pathlib

        source = pathlib.Path(wbdb.__file__).read_text(encoding="utf-8")
        calls = re.findall(r'_ensure_column\(\s*"[^"]+"\s*,\s*"[^"]+"\s*,\s*"([^"]+)"', source)
        self.assertTrue(calls, "未解析到任何 _ensure_column 调用，正则可能已过期")

        known_safe = {"INT", "FLOAT", "TEXT"}
        pg_aliased = set((wbdb._TYPE_ALIASES.get("postgresql") or {}).keys())
        length_typed = {"VARCHAR"}
        for col_type in calls:
            head = col_type.partition("(")[0].strip().upper()
            self.assertTrue(
                head in known_safe or head in pg_aliased or head in length_typed,
                f"迁移列类型 {col_type!r} 未登记：PostgreSQL 上可能无法执行，"
                f"请在 _TYPE_ALIASES 补映射或改用既有类型",
            )


if __name__ == "__main__":
    unittest.main()
