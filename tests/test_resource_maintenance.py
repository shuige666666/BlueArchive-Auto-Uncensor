"""验证缓存清理、更新互斥和服务版本切换。"""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_character_resources import test_directory
from resource_sources import ROOT_DIR
from resource_maintenance import cleanup, pipeline_lock, update_resources
from serve_resources import ResourceServer


class MaintenanceTests(unittest.TestCase):
    """生成文件可以回收，正在服务的版本、原包及覆盖备份必须保留。"""

    def test_cleanup_retains_two_versions_and_active_older_version(self):
        """清理历史预览和中间产物，同时保护当前请求仍可能使用的旧版本。"""
        with test_directory() as directory:
            root = Path(directory)
            builds = []
            for index in range(4):
                build = root / "build/character-resources" / f"20261008T00000000000{index}Z"
                (build / "modified").mkdir(parents=True)
                (build / "modified/bundle").write_bytes(b"keep")
                (build / "previews").mkdir()
                (build / "previews/a.png").write_bytes(b"preview")
                (build / "report.json").write_text(json.dumps({"structural_validation_passed": True}))
                builds.append(build)
            cache = root / ".cache/character-resources"
            for name in ("scan-previews", "backups", "jp", "tools"):
                (cache / name).mkdir(parents=True)
                (cache / name / "a").write_bytes(b"data")
            stage = cache / "staged/20261008T000000000000Z"
            stage.mkdir(parents=True)
            (stage / "a.png").write_bytes(b"data")
            result = cleanup(root, protect=[builds[0] / "modified"])
            self.assertFalse(result["applied"])
            self.assertTrue(builds[1].exists())
            (root / ".cache/local-server").mkdir(parents=True)
            (root / ".cache/local-server/active.json").write_text(json.dumps({"protected_outputs": [
                (builds[0] / "modified").relative_to(root).as_posix()]}))
            result = cleanup(root, apply=True)
            self.assertGreater(result["bytes"], 0)
            self.assertTrue((builds[0] / "modified/bundle").exists())
            self.assertFalse(builds[1].exists())
            self.assertTrue(builds[2].exists())
            self.assertTrue(builds[3].exists())
            self.assertFalse((builds[3] / "previews").exists())
            self.assertFalse((cache / "scan-previews").exists())
            self.assertFalse(stage.exists())
            for name in ("backups", "jp", "tools"):
                self.assertTrue((cache / name / "a").exists())

    def test_cleanup_prunes_only_unreferenced_jp_raw_bundle(self):
        """仍被当前官方清单和本地索引引用的原包应保留，历史原包才回收。"""
        with test_directory() as directory:
            root = Path(directory)
            cache = root / ".cache/character-resources"
            raw = cache / "jp/snapshot/AssetBundles"
            raw.mkdir(parents=True)
            (raw / "keep.bundle").write_bytes(b"keep")
            (raw / "old.bundle").write_bytes(b"old")
            (cache / "jp-catalog.bytes").write_bytes(b"catalog")
            index = {name: {"fingerprint": [4, 1], "file": (raw / name).relative_to(root).as_posix()}
                     for name in ("keep.bundle", "old.bundle")}
            (cache / "jp-file-index.json").write_text(json.dumps(index))
            with patch("jp_catalog.read_jp_catalog", return_value=[{"Name": "keep.bundle", "Size": 4, "Crc": 1}]):
                cleanup(root, apply=True)
            self.assertTrue((raw / "keep.bundle").exists())
            self.assertFalse((raw / "old.bundle").exists())
            self.assertEqual(set(json.loads((cache / "jp-file-index.json").read_text())), {"keep.bundle"})

    def test_os_lock_prevents_concurrent_cleanup(self):
        """扫描持锁时，清理或另一次更新不能同时进入。"""
        with test_directory() as directory:
            root = Path(directory)
            with pipeline_lock(root):
                with self.assertRaises(RuntimeError):
                    with pipeline_lock(root):
                        self.fail("不应同时取得资源任务锁")
            with pipeline_lock(root):
                pass

    def test_unchanged_inputs_skip_build_and_failure_preserves_report(self):
        """定时刷新没有改变输入时不重编码；扫描失败不覆盖成功报告。"""
        with test_directory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            (root / "build/current").mkdir(parents=True)
            (root / "reports/resource-diff-characters.json").write_text(json.dumps({"characters": {"x": {"bundles": [{"key": "x"}]}}}))
            latest = root / "reports/character-resources.json"
            latest.write_text(json.dumps({"input_fingerprint": "same", "output_dir": "build/current"}))
            before = latest.read_bytes()
            with patch("resource_maintenance.ROOT_DIR", root), patch("scan_character_resources.scan"), patch("sync_character_resources.build_fingerprint", return_value="same"), patch("sync_character_resources.run") as build:
                update_resources(offline=True)
                build.assert_not_called()
            with patch("resource_maintenance.ROOT_DIR", root), patch("scan_character_resources.scan", side_effect=RuntimeError("download failed")):
                with self.assertRaises(RuntimeError):
                    update_resources()
            self.assertEqual(latest.read_bytes(), before)

    def test_snapshot_switch_keeps_existing_request_and_rejects_bad_output(self):
        """切换不能改变已有请求的快照，校验失败的新版本不能上线。"""
        with test_directory() as directory:
            old, new = Path(directory) / "old/modified", Path(directory) / "new/modified"
            old.mkdir(parents=True)
            new.mkdir(parents=True)
            report = {"output_dir": new.relative_to(ROOT_DIR).as_posix(), "run_id": "new",
                      "structural_validation_passed": True, "cn_source": {"root_url": "https://example.invalid/new"}}
            with ResourceServer(("127.0.0.1", 0), old, "https://example.invalid/old") as server:
                with server.lease() as snapshot:
                    server.activate(report)
                    self.assertEqual(snapshot[0][0], old.resolve())
                    self.assertIn(old.resolve(), server.protected_outputs())
                    self.assertEqual(server.directory, new.resolve())
                report["structural_validation_passed"] = False
                with self.assertRaises(ValueError):
                    server.activate(report)
                self.assertEqual(server.directory, new.resolve())

    def test_empty_candidate_update_publishes_official_catalog(self):
        """新版没有候选时发布官方清单，不能继续沿用上个版本的补丁。"""
        with test_directory() as directory:
            root = Path(directory)
            cache = root / ".cache/character-resources"
            cache.mkdir(parents=True)
            reports = root / "reports"
            reports.mkdir()
            (reports / "resource-diff-characters.json").write_text(json.dumps({"characters": {}}))
            (cache / "cn-source.json").write_text(json.dumps({"resource_version": "new", "root_url": "https://example.invalid"}))
            (cache / "jp-source.json").write_text(json.dumps({"version": "new"}))
            catalog = {"BundleFiles": [{"Name": "official.bundle", "Size": 1, "Crc": "abc"}]}
            (cache / "cn-catalog.json").write_text(json.dumps(catalog))
            with patch("resource_maintenance.ROOT_DIR", root), patch("scan_character_resources.scan"), patch("sync_character_resources.build_fingerprint", return_value="new"):
                report = update_resources(offline=True)
            self.assertTrue(report["structural_validation_passed"])
            self.assertEqual(report["bundles"], [])
            manifest = root / report["output_dir"] / "AssetBundles/Catalog/new/Android/bundleDownloadInfo.json"
            self.assertEqual(json.loads(manifest.read_text()), catalog)
            self.assertTrue((reports / "character-resources.json").exists())


if __name__ == "__main__":
    unittest.main()
