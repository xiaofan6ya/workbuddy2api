"""服务监听地址配置：env 优先 / DB 兜底 / API / main 解析优先级。"""
import argparse
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from admin import netcfg
from admin import server
from admin.config import settings
from admin.db import Base, get_db
from admin.models import SystemSetting
from admin.routers import netcfg as netcfg_router
from admin.security import require_admin


def _memory_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


class NetCfgUnitTests(unittest.TestCase):
    """直接测 netcfg 模块（用内存库会话）。"""

    def setUp(self):
        self.engine, Session = _memory_db()
        self.db = Session()
        netcfg.invalidate()
        # 保证测试不受宿主 .env 的 ADMIN_HOST 影响
        self._env = patch.dict("os.environ", {}, clear=False)
        self._env.start()
        import os
        os.environ.pop("ADMIN_HOST", None)

    def tearDown(self):
        self._env.stop()
        netcfg.invalidate()
        self.db.close()

    def test_default_is_lan_when_nothing_set(self):
        d = netcfg.current(self.db, fresh=True)
        self.assertEqual(d["host"], "0.0.0.0")
        self.assertEqual(d["source"], "默认值")
        self.assertTrue(d["editable"])

    def test_env_wins_over_db(self):
        self.db.add(SystemSetting(key=netcfg.KEY, value="0.0.0.0"))
        self.db.commit()
        with patch.dict("os.environ", {"ADMIN_HOST": "127.0.0.1"}):
            netcfg.invalidate()
            d = netcfg.current(self.db, fresh=True)
        self.assertEqual(d["host"], "127.0.0.1")
        self.assertFalse(d["editable"])
        self.assertIn("ADMIN_HOST", d["source"])
        self.assertEqual(d["saved"], "0.0.0.0")

    def test_save_then_current_reflects_db(self):
        res = netcfg.save("127.0.0.1", self.db)
        self.assertTrue(res["ok"])
        d = netcfg.current(self.db, fresh=True)
        self.assertEqual(d["host"], "127.0.0.1")
        self.assertEqual(d["source"], "后台设置")

    def test_save_rejected_when_env_set(self):
        with patch.dict("os.environ", {"ADMIN_HOST": "0.0.0.0"}):
            netcfg.invalidate()
            res = netcfg.save("127.0.0.1", self.db)
        self.assertFalse(res["ok"])
        # DB 不应被写入
        row = self.db.query(SystemSetting).filter(SystemSetting.key == netcfg.KEY).first()
        self.assertIsNone(row)

    def test_save_rejects_invalid_value(self):
        for bad in ("1.2.3.4", "", None):
            res = netcfg.save(bad, self.db)
            self.assertFalse(res["ok"], repr(bad))
        row = self.db.query(SystemSetting).filter(SystemSetting.key == netcfg.KEY).first()
        self.assertIsNone(row)

    def test_normalize_aliases(self):
        self.assertEqual(netcfg._normalize("localhost"), "127.0.0.1")
        self.assertEqual(netcfg._normalize("::1"), "127.0.0.1")
        self.assertEqual(netcfg._normalize("LAN"), "0.0.0.0")
        self.assertIsNone(netcfg._normalize("8.8.8.8"))

    def test_db_failure_falls_back_to_default(self):
        netcfg.invalidate()
        with patch("admin.db.SessionLocal", side_effect=RuntimeError("db down")):
            d = netcfg.current(fresh=True)  # db=None -> 走 SessionLocal -> 炸 -> 兜底
        # DB 打不开时退默认 0.0.0.0（不 fail-closed 到 127.0.0.1），且绝不抛异常
        self.assertEqual(d["host"], "0.0.0.0")
        self.assertEqual(d["source"], "默认值")


class NetCfgApiTests(unittest.TestCase):
    """HTTP 层：鉴权 / 状态码。"""

    def setUp(self):
        self.engine, Session = _memory_db()
        self.db = Session()
        netcfg.invalidate()
        self._env = patch.dict("os.environ", {}, clear=False)
        self._env.start()
        import os
        os.environ.pop("ADMIN_HOST", None)

    def tearDown(self):
        self._env.stop()
        netcfg.invalidate()
        self.db.close()

    def _client(self, auth=True):
        app = FastAPI()
        app.include_router(netcfg_router.router)
        if auth:
            app.dependency_overrides[require_admin] = lambda: True
        app.dependency_overrides[get_db] = lambda: self.db
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    def test_api_get_requires_admin(self):
        async def run():
            # 未覆盖 require_admin -> 无 token 应被拒
            async with self._client(auth=False) as c:
                r = await c.get("/api/net/config")
                self.assertIn(r.status_code, (401, 403))
            async with self._client() as c:
                r = await c.get("/api/net/config")
                self.assertEqual(r.status_code, 200)
                d = r.json()
                for k in ("host", "mode", "source", "editable", "allowed"):
                    self.assertIn(k, d)
                self.assertEqual(d["host"], "0.0.0.0")
                self.assertEqual(d["mode"], "lan")
        import asyncio
        asyncio.run(run())

    def test_api_put_valid_and_invalid(self):
        async def run():
            async with self._client() as c:
                r = await c.put("/api/net/config", json={"host": "127.0.0.1"})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["host"], "127.0.0.1")

                r = await c.put("/api/net/config", json={"host": "8.8.8.8"})
                self.assertEqual(r.status_code, 400)
        import asyncio
        asyncio.run(run())

    def test_api_put_env_locked_returns_409(self):
        async def run():
            with patch.dict("os.environ", {"ADMIN_HOST": "0.0.0.0"}):
                netcfg.invalidate()
                async with self._client() as c:
                    r = await c.put("/api/net/config", json={"host": "127.0.0.1"})
                    self.assertEqual(r.status_code, 409)
        import asyncio
        asyncio.run(run())


class ResolveHostPortTests(unittest.TestCase):
    """main._resolve_host_port 的优先级。

    注意：这里必须把 `admin.db.SessionLocal` 换成内存库。否则
    `netcfg.save(...)` / `effective_host()` 会写到/读到**真实**的
    `workbuddy_admin.db`，把开发库里的 `net_bind_host` 污染掉
    （实测中确实把线上库写成了 127.0.0.1）。
    """

    def setUp(self):
        self.engine, Session = _memory_db()
        self.db = Session()
        netcfg.invalidate()
        self._env = patch.dict("os.environ", {}, clear=False)
        self._env.start()
        import os
        os.environ.pop("ADMIN_HOST", None)
        # 让 netcfg 内部 `from admin.db import SessionLocal` 拿到内存会话
        self._db_patch = patch("admin.db.SessionLocal", Session)
        self._db_patch.start()

    def tearDown(self):
        self._db_patch.stop()
        self._env.stop()
        netcfg.invalidate()
        self.db.close()

    def _resolve(self, host=None, port=None):
        import main
        return main._resolve_host_port(argparse.Namespace(host=host, port=port))

    def test_cli_overrides_all(self):
        with patch.dict("os.environ", {"ADMIN_HOST": "0.0.0.0"}):
            h, _ = self._resolve(host="10.0.0.5")
        self.assertEqual(h, "10.0.0.5")

    def test_env_overrides_db(self):
        self.assertTrue(netcfg.save("127.0.0.1", self.db)["ok"])
        netcfg.invalidate()
        with patch.dict("os.environ", {"ADMIN_HOST": "0.0.0.0"}):
            h, _ = self._resolve()
        self.assertEqual(h, "0.0.0.0")

    def test_db_used_when_no_env_and_no_cli(self):
        self.assertTrue(netcfg.save("127.0.0.1", self.db)["ok"])
        netcfg.invalidate()
        h, port = self._resolve()
        self.assertEqual(h, "127.0.0.1")  # 来自内存 DB，而不是真实库
        self.assertEqual(port, 8790)

    def test_default_when_nothing_set(self):
        h, port = self._resolve()
        self.assertEqual(h, "0.0.0.0")  # 无 env、无 DB -> 默认值
        self.assertEqual(port, 8790)


class ConsoleGateRegressionTests(unittest.IsolatedAsyncioTestCase):
    """改监听地址不应影响 /admin 仅本机（回归护栏）。"""

    async def test_admin_still_local_only(self):
        hits = {"n": 0}

        async def app(scope, receive, send):
            hits["n"] += 1
            from starlette.responses import PlainTextResponse
            await PlainTextResponse("ok")(scope, receive, send)

        mw = server.ConsoleLocalOnlyMiddleware(app)
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "path": "/admin", "headers": [],
                 "client": ("192.168.137.50", 54321), "method": "GET"}
        with patch.object(settings, "CONSOLE_LOCAL_ONLY", True), \
             patch.object(settings, "CONSOLE_ALLOWED_IPS", ""):
            await mw(scope, receive, send)
        self.assertEqual(hits["n"], 0)
        start = next(m for m in sent if m.get("type") == "http.response.start")
        self.assertEqual(start["status"], 403)


if __name__ == "__main__":
    unittest.main()
