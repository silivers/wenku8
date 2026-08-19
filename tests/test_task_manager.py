"""TaskManager 暂停 / 恢复 / 删除 测试（使用临时 SQLite）"""

import asyncio
import tempfile
from pathlib import Path

from novel.engine.state import TaskManager
from novel.storage.sqlite_backend import SQLiteBackend


def test_pause_resume_delete():
    tmp = Path(tempfile.mkdtemp()) / "test.db"

    async def run():
        storage = SQLiteBackend(str(tmp))
        await storage.init()
        mgr = TaskManager(storage)

        tid = await mgr.create_task("123", "wenku8")
        await mgr.update_task(tid, status="running", total=10, crawled=3)

        # 暂停
        await mgr.pause_task(tid)
        state = await storage.load_state(tid)
        assert state["status"] == "paused"

        # 恢复
        await mgr.resume_task(tid)
        state = await storage.load_state(tid)
        assert state["status"] == "running"

        # 删除
        await mgr.delete_task(tid)
        assert await storage.load_state(tid) is None

        await storage.close()

    asyncio.run(run())
