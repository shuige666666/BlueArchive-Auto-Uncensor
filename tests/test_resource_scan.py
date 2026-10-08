"""验证图集布局消除、差异筛选和扫描/原包缓存的增量行为。"""

import argparse
import hashlib
import json
from pathlib import Path
import unittest
import zlib
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from test_character_resources import test_directory
from resource_sources import ROOT_DIR, ensure_jp_bundles
from scan_character_resources import candidate_config, compare_group, image_difference, pair_fingerprint, scan
from spine_compare import compare_spine


class VisualDifferenceTests(unittest.TestCase):
    """过滤不可见颜色及轻微误差，同时保留明显内容变化。"""

    def test_hidden_rgb_is_not_a_visual_difference(self):
        """完全透明区域的 RGB 不影响实际显示。"""
        result = image_difference(Image.new("RGBA", (20, 20), (255, 0, 0, 0)), Image.new("RGBA", (20, 20), (0, 255, 0, 0)), 12, .01)
        self.assertEqual(result["classification"], "small_difference")

    def test_minor_quantization_is_not_selected(self):
        """少量编码误差不生成补丁候选。"""
        result = image_difference(Image.new("RGBA", (20, 20), (100, 100, 100, 255)), Image.new("RGBA", (20, 20), (102, 102, 102, 255)), 12, .01)
        self.assertEqual(result["classification"], "small_difference")

    def test_changed_clothing_block_is_selected(self):
        """内部大面积颜色变化不能被邻域容错掩盖。"""
        original = Image.new("RGBA", (100, 100), (100, 100, 100, 255))
        changed = original.copy()
        changed.paste((240, 240, 240, 255), (30, 30, 60, 60))
        self.assertEqual(image_difference(original, changed, 12, .01)["classification"], "visual_difference")

    def test_packing_rotation_and_position_do_not_change_region(self):
        """同图换位置并旋转打包时，还原后的切片仍一致。"""
        part = Image.new("RGBA", (8, 6), (0, 0, 0, 0))
        part.paste((255, 0, 0, 255), (0, 0, 3, 6))
        part.paste((0, 255, 0, 255), (3, 0, 8, 6))
        cn, jp = Image.new("RGBA", (30, 30)), Image.new("RGBA", (30, 30))
        cn.paste(part, (2, 3))
        jp.paste(part.rotate(90, expand=True), (15, 10))
        cn_atlas = "sample.png\nsize:30,30\nbody\nbounds:2,3,8,6\n"
        jp_atlas = "sample.png\nsize:30,30\nbody\nbounds:15,10,8,6\nrotate:90\n"
        result, previews = compare_spine(cn, jp, cn_atlas, jp_atlas, "sample", lambda left, right: image_difference(left, right, 12, .01))
        self.assertEqual(result["classification"], "small_difference")
        self.assertFalse(previews)

    def test_unknown_atlas_is_not_auto_selected(self):
        """无法还原的图集交给复核，不把整张图位置变化当作补丁。"""
        image = Image.new("RGBA", (10, 10))
        result, _ = compare_spine(image, image, "invalid", "invalid", "sample", lambda left, right: image_difference(left, right, 12, .01))
        self.assertEqual(result["classification"], "needs_review")

    def test_numbered_body_parts_with_different_canvases_are_compared(self):
        """瞬的身体附件带数字后缀且尺寸不同，不能被误合并后整组排除。"""
        cn = Image.new("RGBA", (100, 100), (100, 100, 100, 255))
        jp = cn.copy()
        jp.paste((240, 240, 240, 255), (0, 0, 20, 20))
        atlas = "sample.png\nsize:100,100\nbody_00\nbounds:0,0,20,20\nbody_01\nbounds:30,0,30,20\nface\nbounds:0,40,10,10\n"
        result, _ = compare_spine(cn, jp, atlas, atlas, "sample", lambda left, right: image_difference(left, right, 12, .01))
        self.assertEqual(result["classification"], "visual_difference")
        self.assertEqual(result["regions"]["body_00"]["classification"], "visual_difference")
        self.assertIn("body_01", result["regions"])
        self.assertEqual(result["regions"]["face"]["classification"], "small_difference")

    def test_text_only_changes_are_not_auto_patched(self):
        """骨骼或图集变化可以是更新，单靠字节变化不能认定立绘修改。"""
        base = "assets-_mx-spinecharacters-sample-_mxdependency-"
        result = {
            base + "textures": {"assets": [{"name": "sample", "type": "Texture2D", "classification": "identical"}]},
            base + "textassets": {"assets": [{"name": "sample" + suffix, "type": "TextAsset", "classification": "text_difference"} for suffix in (".atlas", ".skel")]},
        }
        self.assertFalse(candidate_config(result)["characters"])

    def test_incompatible_page_size_is_not_hidden_by_atlas_scaling(self):
        """图集能还原切片也不代表整页能写入国服，尺寸不同必须留待适配。"""
        key = ("Texture2D", "sample")
        jp = SimpleNamespace(read=lambda: SimpleNamespace(image=Image.new("RGBA", (10, 10))))
        cn = SimpleNamespace(read=lambda: SimpleNamespace(image=Image.new("RGBA", (20, 10))))
        with patch("scan_character_resources.index_group", side_effect=[({key: jp}, []), ({key: cn}, []), ({}, []), ({}, [])]), patch("scan_character_resources.asset_digest", side_effect=["cn", "jp"]), patch("scan_character_resources.compare_spine") as atlas:
            result = compare_group([], [], ROOT_DIR / ".cache/tests", 12, .01, ["jp-atlas"], ["cn-atlas"])
        self.assertEqual(result["assets"][0]["classification"], "incompatible_size")
        atlas.assert_not_called()


class IncrementalScanTests(unittest.TestCase):
    """实际执行扫描编排，核对第二次运行不会再下载或解包。"""

    def test_reuse_then_invalidate_only_changed_group(self):
        """相同清单复用结果；日服单包标记更新后只重比该组。"""
        with test_directory() as directory:
            cache = Path(directory)
            name = "assets-_mx-spinecharacters-sample-_mxdependency-textassets-2025-07-02_assets_all_1.bundle"
            cn_entry = {"Name": name, "Size": 1, "Crc": hashlib.md5(b"x").hexdigest()}
            jp_entry = {"Name": name, "Size": 1, "Crc": 10}
            cn_file = cache / "cn/test" / name
            cn_file.parent.mkdir(parents=True)
            cn_file.write_bytes(b"x")
            jp_metadata = {"bundle_dir": cache.relative_to(ROOT_DIR).as_posix()}
            args = argparse.Namespace(cache_dir=directory, proxy=None, offline=False, group=[], rescan=False, pixel_threshold=12, min_changed_ratio=.01, report=str(cache / "report.json"), config_output=str(cache / "config.json"), build=False, workers=1)
            result = {"assets": [], "jp_only": [], "cn_only": [], "jp_ambiguous": [], "cn_ambiguous": []}
            with patch("scan_character_resources.fetch_jp_catalog", return_value=([jp_entry], jp_metadata)), patch("scan_character_resources.fetch_cn_catalog", return_value=({"BundleFiles": [cn_entry]}, {"resource_version": "test", "root_url": "https://example.invalid"})), patch("scan_character_resources.ensure_jp_bundles", return_value={"downloaded_files": 0, "downloaded_payload_bytes": 0}) as downloader, patch("scan_character_resources.compare_group", return_value=result) as compare, patch("builtins.print"):
                first = scan(args)
                second = scan(args)
                self.assertEqual(first["counters"]["compared_groups"], 1)
                self.assertEqual(second["counters"]["reused_groups"], 1)
                self.assertEqual(compare.call_count, 1)
                self.assertEqual(downloader.call_count, 1)
                jp_entry["Crc"] = 11
                third = scan(args)
                self.assertEqual(third["counters"]["compared_groups"], 1)
                self.assertEqual(compare.call_count, 2)

    def test_settings_change_invalidates_comparison(self):
        """调整差异阈值后不能继续使用旧判断。"""
        entries = [{"Name": "sample.bundle", "Size": 1, "Crc": 10}]
        self.assertNotEqual(pair_fingerprint(entries, entries, 12, .01), pair_fingerprint(entries, entries, 20, .01))

    def test_unchanged_bundle_is_reused_across_catalog_snapshots(self):
        """全服清单变动时，未变化文件复制复用并更新本地索引位置。"""
        with test_directory() as directory:
            cache = Path(directory)
            first, second = cache / "jp/old/AssetBundles", cache / "jp/new/AssetBundles"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            name = "sample.bundle"
            (first / name).write_bytes(b"sample")
            entries = [{"Name": name, "Size": 6, "Crc": zlib.crc32(b"sample")}]
            with patch("resource_sources.prepare_baad", side_effect=AssertionError("不应下载")):
                ensure_jp_bundles(None, cache, entries, {"bundle_dir": first.relative_to(ROOT_DIR).as_posix()}, offline=True)
                result = ensure_jp_bundles(None, cache, entries, {"bundle_dir": second.relative_to(ROOT_DIR).as_posix()}, offline=True)
            self.assertEqual(result["downloaded_files"], 0)
            self.assertEqual((second / name).read_bytes(), b"sample")
            index = json.loads((cache / "jp-file-index.json").read_text(encoding="utf-8"))
            self.assertEqual(index[name]["file"], (second / name).relative_to(ROOT_DIR).as_posix())

    def test_corrupt_known_bundle_is_rejected_offline(self):
        """同尺寸损坏也不能冒充已验证缓存。"""
        with test_directory() as directory:
            cache = Path(directory)
            target = cache / "sample.bundle"
            target.write_bytes(b"before")
            entries = [{"Name": target.name, "Size": 6, "Crc": zlib.crc32(b"before")}]
            metadata = {"bundle_dir": cache.relative_to(ROOT_DIR).as_posix()}
            ensure_jp_bundles(None, cache, entries, metadata, offline=True)
            target.write_bytes(b"broken")
            with self.assertRaisesRegex(RuntimeError, "摘要不符"):
                ensure_jp_bundles(None, cache, entries, metadata, offline=True)

    def test_partial_tool_success_retries_only_missing_bundle(self):
        """工具退出成功却缺少文件时，独立检查并只重试失败项。"""
        with test_directory() as directory:
            cache = Path(directory)
            bundles = cache / "jp/current/AssetBundles"
            bundles.mkdir(parents=True)
            global_cache = cache / "global"
            catalog = global_cache / "catalog/japan/android/BundlePackingInfo.bytes"
            catalog.parent.mkdir(parents=True)
            catalog.write_bytes(b"catalog")
            entries = [{"Name": name, "Size": 6, "Crc": zlib.crc32(b"sample")} for name in ("first.bundle", "second.bundle")]
            metadata = {"bundle_dir": bundles.relative_to(ROOT_DIR).as_posix(), "catalog_sha256": hashlib.sha256(b"catalog").hexdigest()}
            patterns = []

            def simulate(executable, pattern, output, log, proxy, limit=4):
                """首轮模拟部分成功，重试只写入第二个文件。"""
                patterns.append(pattern)
                (bundles / ("first.bundle" if len(patterns) == 1 else "second.bundle")).write_bytes(b"sample")

            with patch("resource_sources.prepare_baad", return_value=(Path("unused"), {})), patch("resource_sources.run_baad", side_effect=simulate), patch("resource_sources.baad_data_dir", return_value=global_cache), patch("builtins.print"):
                result = ensure_jp_bundles(None, cache, entries, metadata)
            self.assertEqual(result["downloaded_files"], 2)
            self.assertEqual(len(patterns), 2)
            self.assertNotIn("first", patterns[1])
            self.assertIn("second", patterns[1])

    def test_equal_size_corrupt_download_cannot_pass(self):
        """同大小错误内容不能通过日服下载验证。"""
        with test_directory() as directory:
            cache = Path(directory)
            bundles = cache / "jp/current/AssetBundles"
            bundles.mkdir(parents=True)
            global_cache = cache / "global"
            catalog = global_cache / "catalog/japan/android/BundlePackingInfo.bytes"
            catalog.parent.mkdir(parents=True)
            catalog.write_bytes(b"catalog")
            metadata = {"bundle_dir": bundles.relative_to(ROOT_DIR).as_posix(), "catalog_sha256": hashlib.sha256(b"catalog").hexdigest()}
            entries = [{"Name": "sample.bundle", "Size": 6, "Crc": zlib.crc32(b"sample")}]
            with patch("resource_sources.prepare_baad", return_value=(Path("unused"), {})), patch("resource_sources.run_baad", side_effect=lambda *args, **kwargs: (bundles / "sample.bundle").write_bytes(b"broken")), patch("resource_sources.baad_data_dir", return_value=global_cache), patch("builtins.print"):
                with self.assertRaisesRegex(RuntimeError, "CRC 不符"):
                    ensure_jp_bundles(None, cache, entries, metadata)


if __name__ == "__main__":
    unittest.main()
