"""特征测试③：档案素材授权白名单（历史档案模式的核心合规约束）。

锁定"仅接受 PD / CC0 / CC BY，拒绝会传染/限制商用与演绎的 CC BY-SA / NC / ND /
fair use / all rights reserved"这一硬规则。针对纯函数，不触网、不依赖 ffmpeg。

* wikimedia.classify_license —— 允许/拒绝/无元数据/无法识别四态。
* wikimedia._is_noise_asset —— 非图片(PDF/地图)与"以人物命名的街道/学校"噪声剔除。
"""

from __future__ import annotations

import unittest

from app.services.archives import wikimedia


class TestClassifyLicense(unittest.TestCase):
    def test_public_domain_allowed(self):
        allowed, reason = wikimedia.classify_license("Public Domain", "")
        self.assertTrue(allowed)
        self.assertEqual(reason, "Public Domain")

    def test_cc0_allowed(self):
        self.assertTrue(wikimedia.classify_license("CC0", "")[0])

    def test_cc_by_4_allowed(self):
        self.assertTrue(wikimedia.classify_license("CC BY 4.0", "")[0])

    def test_cc_by_sa_rejected(self):
        # ShareAlike 会传染整片再授权，必须拒绝，即名字里含 "cc by"。
        allowed, reason = wikimedia.classify_license("CC BY-SA 4.0", "")
        self.assertFalse(allowed)
        self.assertIn("restricted license", reason)

    def test_cc_by_nc_rejected(self):
        self.assertFalse(wikimedia.classify_license("CC BY-NC 2.0", "")[0])

    def test_cc_by_nd_rejected(self):
        self.assertFalse(wikimedia.classify_license("CC BY-ND 3.0", "")[0])

    def test_fair_use_and_all_rights_reserved_rejected(self):
        self.assertFalse(wikimedia.classify_license("Fair use", "")[0])
        self.assertFalse(wikimedia.classify_license("All Rights Reserved", "")[0])

    def test_missing_metadata_rejected(self):
        allowed, reason = wikimedia.classify_license("", "")
        self.assertFalse(allowed)
        self.assertEqual(reason, "no license metadata")

    def test_unrecognised_rejected(self):
        allowed, reason = wikimedia.classify_license("Weird Custom Terms", "")
        self.assertFalse(allowed)
        self.assertIn("unrecognised license", reason)


class TestIsNoiseAsset(unittest.TestCase):
    def test_real_photo_not_noise(self):
        self.assertFalse(wikimedia._is_noise_asset("File:Einstein portrait 1921.jpg"))

    def test_non_image_extension_is_noise(self):
        # PDF / 地图 / SVG 不能当影像素材。
        self.assertTrue(wikimedia._is_noise_asset("File:Einstein biography.pdf"))
        self.assertTrue(wikimedia._is_noise_asset("File:Berlin map.svg"))

    def test_named_after_subject_is_noise(self):
        # 以人物命名的街道/学校/纪念碑不是本人照片。
        self.assertTrue(wikimedia._is_noise_asset("File:Albert Einstein Straße.jpg"))
        self.assertTrue(wikimedia._is_noise_asset("File:Einstein High School.png"))

    def test_empty_not_noise(self):
        self.assertFalse(wikimedia._is_noise_asset(""))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
