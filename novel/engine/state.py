"""任务状态管理"""

from __future__ import annotations

import uuid
from datetime import datetime

from loguru import logger

from novel.core.exceptions import InvalidTaskStateError
from novel.core.storage import StorageBackend


class TaskManager:
    """爬取任务管理：创建、恢复、查询。"""

    def __init__(self, storage: StorageBackend):
        self.storage = storage

    async def create_task(self, novel_id: str, site: str) -> str:
        """创建新的爬取任务，返回 task_id。"""
        task_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        await self.storage.save_state(task_id, {
            "task_id": task_id,
            "novel_id": novel_id,
            "site": site,
            "status": "pending",
            "total_chapters": 0,
            "crawled_chapters": 0,
            "failed_chapters": "[]",
            "last_chapter_id": None,
            "started_at": now,
            "updated_at": now,
            "error_message": None,
        })
        logger.info(f"创建任务: {task_id} (site={site}, novel_id={novel_id})")
        return task_id

    async def update_task(
        self,
        task_id: str,
        status: str | None = None,
        total: int | None = None,
        crawled: int | None = None,
        failed: list[str] | None = None,
        last_chapter_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """更新任务状态。"""
        state = await self.storage.load_state(task_id)
        if state is None:
            raise InvalidTaskStateError(f"Task not found: {task_id}")

        if status:
            state["status"] = status
        if total is not None:
            state["total_chapters"] = total
        if crawled is not None:
            state["crawled_chapters"] = crawled
        if failed is not None:
            import json
            state["failed_chapters"] = json.dumps(failed)
        if last_chapter_id is not None:
            state["last_chapter_id"] = last_chapter_id
        if error is not None:
            state["error_message"] = error

        state["updated_at"] = datetime.now().isoformat()
        await self.storage.save_state(task_id, state)

    async def resume_task(self, task_id: str) -> dict:
        """恢复任务。返回任务状态。"""
        state = await self.storage.load_state(task_id)
        if state is None:
            raise InvalidTaskStateError(f"Task not found: {task_id}")

        if state.get("status") not in ("paused", "failed"):
            raise InvalidTaskStateError(
                f"Cannot resume task in '{state.get('status')}' state"
            )

        state["status"] = "running"
        state["updated_at"] = datetime.now().isoformat()
        await self.storage.save_state(task_id, state)
        logger.info(f"恢复任务: {task_id}")
        return state

    async def pause_task(self, task_id: str) -> dict:
        """将任务标记为暂停。正在运行的引擎会在下一章节边界自动停止。"""
        state = await self.storage.load_state(task_id)
        if state is None:
            raise InvalidTaskStateError(f"Task not found: {task_id}")

        if state.get("status") not in ("running", "pending"):
            raise InvalidTaskStateError(
                f"Cannot pause task in '{state.get('status')}' state"
            )

        state["status"] = "paused"
        state["updated_at"] = datetime.now().isoformat()
        await self.storage.save_state(task_id, state)
        logger.info(f"暂停任务: {task_id}")
        return state

    async def delete_task(self, task_id: str) -> None:
        """删除任务记录。"""
        await self.storage.delete_task(task_id)
        logger.info(f"删除任务: {task_id}")
