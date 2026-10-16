import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from app.models.schema import VideoAspect
from app.services import material_cache


def _pin_mtime(cache_path: Path) -> None:
    """Windows NTFS 文件 mtime 可比 time.time() 超前数微秒，导致
    cache_age<0 被当作未来日期删除。固定 mtime 到 1 秒前消除竞态。"""
    t = time.time() - 1
    os.utime(cache_path, (t, t))


@pytest.mark.parametrize('duration', ['1e309', 'Infinity', '-Infinity', 'NaN', '0.5'])
def test_corrupted_duration_is_a_cache_miss(duration):
    with TemporaryDirectory() as directory, patch.object(material_cache.utils, 'storage_dir', return_value=directory):
        cache_path = material_cache._cache_path('pixabay', 'nature', 5, VideoAspect.portrait)
        cache_path.write_text(
            '{"version":2,"items":[{"provider":"pixabay","url":"https://example.com/clip.mp4",'
            '"source_info":{"provider":"pixabay"},"duration":' + duration + '}]}'
        )
        _pin_mtime(cache_path)
        assert material_cache.load_material_search_cache('pixabay', 'nature', 5, VideoAspect.portrait) is None
        assert not Path(cache_path).exists()


def test_legacy_finite_float_duration_remains_usable():
    with TemporaryDirectory() as directory, patch.object(material_cache.utils, 'storage_dir', return_value=directory):
        cache_path = material_cache._cache_path('pixabay', 'nature', 5, VideoAspect.portrait)
        cache_path.write_text(
            '{"version":2,"items":[{"provider":"pixabay","url":"https://example.com/clip.mp4",'
            '"source_info":{"provider":"pixabay"},"duration":12.5}]}'
        )
        _pin_mtime(cache_path)
        items = material_cache.load_material_search_cache('pixabay', 'nature', 5, VideoAspect.portrait)
        assert items is not None
        assert items[0].duration == 12
