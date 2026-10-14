"""存储 / 缓存管理控制器测试。

以 SimpleNamespace 作为 Request 替身直接调用视图函数，mock 掉 cache_manager 与
utils.storage_dir，聚焦目录占用汇总、缓存预览参数转发与清理结果透传，不触碰真实
文件系统。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.controllers.v1 import storage as store
from app.models.schema import CacheCleanRequest


def _request():
    return SimpleNamespace(headers={"x-task-id": "req-1"})


class _Stats:
    def __init__(self, file_count=5, total_size=1024, oldest=100.0, newest=200.0):
        self.file_count = file_count
        self.total_size = total_size
        self.oldest_mtime = oldest
        self.newest_mtime = newest


class _CleanResult:
    def __init__(self, deleted_count=3, deleted_size=512, failed_count=1):
        self.deleted_count = deleted_count
        self.deleted_size = deleted_size
        self.failed_count = failed_count


class TestStorageUsage(unittest.TestCase):
    def test_usage_sums_dirs_and_forwards_cache_stats(self):
        with (
            patch.object(store.utils, "storage_dir", return_value="/tmp/storage"),
            patch.object(
                store, "_dir_usage", side_effect=lambda n: {"name": n, "count": 1, "size": 10, "exists": True}
            ),
            patch.object(store.cache_manager, "get_video_cache_stats", return_value=_Stats()),
            patch.object(store.cache_manager, "video_cache_dir", return_value="/tmp/storage/cache_videos"),
        ):
            response = store.get_storage_usage(_request())
        data = response["data"]
        self.assertEqual(len(data["dirs"]), len(store._STORAGE_DIRS))
        self.assertEqual(data["total_bytes"], 10 * len(store._STORAGE_DIRS))
        self.assertEqual(data["video_cache"]["file_count"], 5)
        self.assertEqual(data["video_cache"]["total_size"], 1024)

    def test_preview_forwards_max_age_days(self):
        with patch.object(
            store.cache_manager, "get_video_cache_stats", return_value=_Stats(file_count=2, total_size=64)
        ) as stats:
            response = store.preview_video_cache(_request(), max_age_days=7)
        stats.assert_called_once_with(max_age_days=7)
        self.assertEqual(response["data"]["file_count"], 2)
        self.assertEqual(response["data"]["total_size"], 64)

    def test_clean_forwards_and_reports_remaining(self):
        body = CacheCleanRequest(max_age_days=30)
        with (
            patch.object(store.cache_manager, "clean_video_cache", return_value=_CleanResult()) as clean,
            patch.object(store.cache_manager, "get_video_cache_stats", return_value=_Stats(file_count=1, total_size=8)),
        ):
            response = store.clean_video_cache(_request(), body)
        clean.assert_called_once_with(max_age_days=30)
        data = response["data"]
        self.assertEqual(data["deleted_count"], 3)
        self.assertEqual(data["failed_count"], 1)
        self.assertEqual(data["remaining"]["file_count"], 1)


if __name__ == "__main__":
    unittest.main()
