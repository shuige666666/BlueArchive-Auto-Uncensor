"""验证按包复用、立绘成组回退，以及新版清单与文件字节一致。"""

import copy
import gc
import hashlib
import json
import shutil
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import UnityPy
from PIL import Image

from test_character_resources import test_directory
from patch_reuse import builder_signature, prepare_compatible_patch, write_catalog
from resource_sources import ROOT_DIR, save_json
from sync_character_resources import run


TEXTURE = "assets-_mx-spinecharacters-ako_spr-_mxdependency-textures-2026-06-04_assets_all_1.bundle"
TEXT = "assets-_mx-spinecharacters-ako_spr-_mxdependency-textassets-2026-06-04_assets_all_2.bundle"
COLLECTION = "uis-01_common-14_charactercollect-_mxload-textures-2026-06-04_assets_all_3.bundle"


def make_snapshot(root, names=(TEXTURE, TEXT, COLLECTION), version="202610131404", stamp="20261029T000000000000Z"):
    """创建可追溯的最小旧补丁快照，不依赖网络或全量游戏资源。"""
    output = root / "build/character-resources" / stamp / "modified"
    records, entries = [], []
    for name in names:
        original, modified = ("original-" + name).encode(), ("patched-" + name).encode()
        path = output / "AssetBundles/Android" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(modified)
        raw = root / ".cache/character-resources/cn" / version / name
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(original)
        entries.append({"Name": name, "Size": len(original), "Crc": hashlib.md5(original).hexdigest()})
        records.append({"name": name, "size": len(modified), "output_sha256": hashlib.sha256(modified).hexdigest(),
            "original_sha256": hashlib.sha256(original).hexdigest(), "original_md5": hashlib.md5(original).hexdigest(),
            "original_size": len(original), "md5": hashlib.md5(modified).hexdigest(), "replacement_mode": "cn_objects", "assets": []})
    report = {"run_id": stamp, "output_dir": output.relative_to(root).as_posix(), "structural_validation_passed": True,
        "builder_signature": builder_signature(), "cn_source": {"resource_version": version, "root_url": "https://official.invalid/prodm39"},
        "jp_source": {"version": "test"}, "characters": ["ako"], "bundles": records}
    catalog = {"BundleFiles": entries}
    modified_catalog = copy.deepcopy(catalog)
    for entry, record in zip(modified_catalog["BundleFiles"], records):
        entry.update(Crc=record["md5"], Size=record["size"])
    write_catalog(output, version, modified_catalog)
    save_json(output.parent / "report.json", report)
    return report, catalog


class CompatibleSnapshotTests(unittest.TestCase):
    """验证官方版本变更时可用旧包被保留，变化部分不会错误混搭。"""

    def test_version_only_change_preserves_every_patch_byte_and_manifest(self):
        """仅版本号变化应保留全部包，清单校验值仍是补丁值。"""
        with test_directory() as temporary:
            root = Path(temporary)
            old, official = make_snapshot(root)
            report = prepare_compatible_patch(old, official, "202610221737", "https://official.invalid/prodm39", root)
            self.assertEqual(report["build_stats"], {"reused_bundles": 3, "rebuilt_bundles": 0, "official_bundles": 0})
            output = root / report["output_dir"]
            manifest = output / "AssetBundles/Catalog/202610221737/Android/bundleDownloadInfo.json"
            for entry in json.loads(manifest.read_text())["BundleFiles"]:
                data = (output / "AssetBundles/Android" / entry["Name"]).read_bytes()
                self.assertEqual(hashlib.md5(data).hexdigest(), entry["Crc"])
                self.assertEqual(len(data), entry["Size"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), next(b for b in old["bundles"] if b["name"] == entry["Name"])["output_sha256"])
            self.assertEqual(manifest.with_suffix(".hash").read_text(), str(zlib.crc32(manifest.read_bytes())))
            # 复制后的快照独立持有文件，不依赖旧目录继续存在。
            shutil.rmtree(root / old["output_dir"])
            self.assertTrue((output / "AssetBundles/Android" / TEXTURE).is_file())

    def test_changed_texture_falls_back_with_its_skeleton_only(self):
        """纹理原包变化时整套立绘回退，其他画像包保持补丁。"""
        with test_directory() as temporary:
            root = Path(temporary)
            old, official = make_snapshot(root)
            official["BundleFiles"][0].update(Crc=hashlib.md5(b"new-texture").hexdigest(), Size=11)
            report = prepare_compatible_patch(old, official, "202610221737", "https://official.invalid/prodm39", root)
            self.assertEqual([b["name"] for b in report["bundles"]], [COLLECTION])
            output = root / report["output_dir"]
            entries = json.loads(next(output.glob("AssetBundles/Catalog/*/Android/*.json")).read_text())["BundleFiles"]
            self.assertEqual(entries[:2], official["BundleFiles"][:2])
            self.assertFalse((output / "AssetBundles/Android" / TEXT).exists())

    def test_corrupt_missing_and_legacy_records_are_handled(self):
        """旧报告可从原包缓存迁移，损坏补丁不能随兼容清单发布。"""
        with test_directory() as temporary:
            root = Path(temporary)
            old, official = make_snapshot(root)
            legacy = copy.deepcopy(old)
            legacy.pop("builder_signature")
            legacy["runtime"] = {k: v for k, v in builder_signature().items() if k != "patch_format"}
            for record in legacy["bundles"]:
                record.pop("original_md5")
                record.pop("original_size")
            report = prepare_compatible_patch(legacy, official, "202610221737", "https://official.invalid/prodm39", root)
            self.assertEqual(len(report["bundles"]), 3)
            texture = root / old["output_dir"] / "AssetBundles/Android" / TEXTURE
            texture.write_bytes(b"x" * texture.stat().st_size)
            report = prepare_compatible_patch(old, official, "202610221738", "https://official.invalid/prodm39", root)
            self.assertEqual([b["name"] for b in report["bundles"]], [COLLECTION])
            (root / old["output_dir"] / "AssetBundles/Android" / COLLECTION).unlink()
            report = prepare_compatible_patch(old, official, "202610221739", "https://official.invalid/prodm39", root)
            self.assertEqual(report["bundles"], [])

    def test_removed_family_rules_and_invalid_paths_do_not_reuse(self):
        """删除、处理规则变化和非法路径都不能沿用旧资源。"""
        with test_directory() as temporary:
            root = Path(temporary)
            old, official = make_snapshot(root)
            official["BundleFiles"] = official["BundleFiles"][1:]
            report = prepare_compatible_patch(old, official, "202610221737", "https://official.invalid/prodm39", root)
            self.assertEqual([b["name"] for b in report["bundles"]], [COLLECTION])
            with patch("patch_reuse.PATCH_FORMAT_VERSION", 4):
                report = prepare_compatible_patch(old, official, "202610221738", "https://official.invalid/prodm39", root)
                self.assertEqual(report["bundles"], [])
            old["bundles"][0]["name"] = "../outside.bundle"
            with self.assertRaises(ValueError):
                prepare_compatible_patch(old, official, "202610221739", "https://official.invalid/prodm39", root)
            with self.assertRaises(ValueError):
                prepare_compatible_patch(old, official, "../escape", "https://official.invalid/prodm39", root)


class IncrementalBuildTests(unittest.TestCase):
    """通过实际 Unity 包验证复用跳过重打包，而非只比较日志计数。"""

    def test_actual_bundle_reuse_change_and_corruption(self):
        """版本变化复用同字节补丁，素材变化或产物损坏才重新构建。"""
        original = next((ROOT_DIR / "assetexclusions").glob("assets-_mx-spinecharacters-ako_spr-_mxdependency-textures-*.bundle"))
        with test_directory() as temporary:
            root = Path(temporary)
            cache, jp = root / ".cache/character-resources", root / "jp"
            jp.mkdir()
            source = jp / original.name
            env = UnityPy.load(str(original))
            data = next(o for o in env.objects if o.type.name == "Texture2D").read()
            data.image = Image.new("RGBA", (64, 32), (100, 20, 30, 255))
            data.save()
            source.write_bytes(env.file.save(packer="original"))
            second_name = original.name.replace("ako_spr", "other_spr")
            shutil.copyfile(source, jp / second_name)
            raw = cache / "cn/202610131404" / original.name
            raw.parent.mkdir(parents=True)
            shutil.copyfile(original, raw)
            shutil.copyfile(original, raw.with_name(second_name))
            catalog = {"BundleFiles": [{"Name": original.name, "Size": original.stat().st_size,
                                        "Crc": hashlib.md5(original.read_bytes()).hexdigest()}]}
            catalog["BundleFiles"].append({**catalog["BundleFiles"][0], "Name": second_name})
            save_json(cache / "cn-catalog.json", catalog)
            save_json(cache / "cn-source.json", {"resource_version": "202610131404", "root_url": "https://official.invalid"})
            config = root / "config.json"
            save_json(config, {"schema_version": 1, "characters": {"ako": {"bundles": [{
                "key": original.stem.rsplit("-2026-", 1)[0], "textures": ["ako_spr"], "text_assets": []}, {
                "key": Path(second_name).stem.rsplit("-2026-", 1)[0], "textures": ["ako_spr"], "text_assets": []}]}}})
            args = SimpleNamespace(config="config.json", character=[], cache_dir=".cache/character-resources", proxy=None,
                                   output_dir=None, report="reports/latest.json", offline=True, apply=False, workers=1)
            with patch("sync_character_resources.ROOT_DIR", root), patch("sync_character_resources.fetch_jp_catalog", return_value=([], {"version": "test", "bundle_dir": "jp"})), \
                 patch("sync_character_resources.ensure_jp_bundles"), patch("sync_character_resources.build_fingerprint", return_value="test-input"):
                first = run(args)
                gc.collect()
                save_json(cache / "cn-source.json", {"resource_version": "202610221737", "root_url": "https://official.invalid"})
                with patch("sync_character_resources.patch_bundle", side_effect=AssertionError("复用时不得重打包")):
                    second = run(args)
                gc.collect()
                self.assertEqual(second["build_stats"]["reused_bundles"], 2)
                self.assertEqual(first["bundles"][0]["output_sha256"], second["bundles"][0]["output_sha256"])
                # 输入实际改变后应重建，不能因为文件名相同而命中缓存。
                data.image = Image.new("RGBA", (64, 32), (20, 180, 90, 255))
                data.save()
                source.write_bytes(env.file.save(packer="original"))
                third = run(args)
                gc.collect()
                self.assertEqual(third["build_stats"]["rebuilt_bundles"], 1)
                self.assertEqual(third["build_stats"]["reused_bundles"], 1)
                third_record = next(r for r in third["bundles"] if r["name"] == original.name)
                second_record = next(r for r in second["bundles"] if r["name"] == original.name)
                self.assertNotEqual(third_record["output_sha256"], second_record["output_sha256"])
                target = root / third["output_dir"] / "AssetBundles/Android" / original.name
                target.write_bytes(b"x" * target.stat().st_size)
                fourth = run(args)
                gc.collect()
                self.assertEqual(fourth["build_stats"]["rebuilt_bundles"], 1)
                self.assertEqual(fourth["build_stats"]["reused_bundles"], 1)
                fourth_record = next(r for r in fourth["bundles"] if r["name"] == original.name)
                self.assertEqual(third_record["output_sha256"], fourth_record["output_sha256"])


if __name__ == "__main__":
    unittest.main()
