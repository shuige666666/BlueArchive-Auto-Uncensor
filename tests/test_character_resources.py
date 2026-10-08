"""验证角色范围、路径隔离、下载校验及骨骼二进制处理。"""

import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from character_patch import find_assets, text_bytes
from resource_sources import download_file
from sync_character_resources import read_plan, workspace_path
from uploadR2 import run as upload_resources


@contextmanager
def test_directory():
    """把测试临时文件限制在项目缓存，并在清理前确认目录范围。"""
    root = Path(__file__).resolve().parents[1] / ".cache/tests"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        if not Path(directory).resolve().is_relative_to(root.resolve()):
            raise RuntimeError("测试临时目录超出项目缓存。")
        yield directory


class CharacterPlanTests(unittest.TestCase):
    """保护角色与皮肤的明确范围，防止错误配置扩大替换范围。"""

    def write_config(self, directory, characters):
        """创建只用于该测试的角色配置。"""
        path = Path(directory) / "characters.json"
        path.write_text(json.dumps({"schema_version": 1, "characters": characters}), encoding="utf-8")
        return path

    def test_shared_collection_group_merges_selected_characters(self):
        """多个角色共用收藏包时，只合并所选角色，不混入其他皮肤。"""
        with test_directory() as directory:
            characters = {name: {"bundles": [{"key": "collection", "textures": [name]}]} for name in ("Asuna", "Ako", "AsunaBunny")}
            selected, groups = read_plan(self.write_config(directory, characters), ["Asuna", "Ako"])
            self.assertEqual(set(groups["collection"]), {("Texture2D", "Asuna"), ("Texture2D", "Ako")})
            self.assertEqual(selected, ["Asuna", "Ako"])

    def test_unknown_character_fails_instead_of_falling_back_to_all(self):
        """输入不存在的角色时不能默默替换全部角色。"""
        with test_directory() as directory:
            with self.assertRaisesRegex(ValueError, "角色未配置"):
                read_plan(self.write_config(directory, {"asuna": {"bundles": []}}), ["typo"])

    def test_bundle_group_cannot_escape_staging_directory(self):
        """资源组不是文件路径，禁止通过配置写出暂存目录。"""
        with test_directory() as directory:
            config = self.write_config(directory, {"asuna": {"bundles": [{"key": "../outside", "textures": ["Asuna"]}]}})
            with self.assertRaisesRegex(ValueError, "不能包含路径"):
                read_plan(config, ["asuna"])

    def test_output_must_stay_in_workspace(self):
        """保护意外填写到项目之外的输出位置。"""
        with self.assertRaisesRegex(ValueError, "必须位于项目内"):
            workspace_path("../outside")


class AssetIntegrityTests(unittest.TestCase):
    """验证缺项、同名冲突和二进制数据的真实失败路径。"""

    def test_spine_binary_bytes_survive_surrogateescape(self):
        """骨骼文件中不是 UTF-8 的字节不能被替换字符损坏。"""
        content = bytes(range(256)) + b"\x00\xffSpine"
        self.assertEqual(text_bytes(SimpleNamespace(m_Script=content.decode("utf-8", "surrogateescape"))), content)

    def test_missing_source_asset_fails(self):
        """缺少原图时不能生成看起来成功的空补丁。"""
        with self.assertRaisesRegex(RuntimeError, "缺少需要的资源"):
            find_assets([], {("Texture2D", "Asuna")})

    def test_conflicting_same_name_sources_fail(self):
        """清单中出现不同内容的同名资源时不能按文件排序误选。"""
        def reader(content):
            return SimpleNamespace(type=SimpleNamespace(name="TextAsset"), peek_name=lambda: "asuna.skel", read=lambda: SimpleNamespace(m_Script=content))

        with patch("character_patch.UnityPy.load", side_effect=[SimpleNamespace(objects=[reader(b"first")]), SimpleNamespace(objects=[reader(b"different")])]):
            with self.assertRaisesRegex(RuntimeError, "内容不同的同名资源"):
                find_assets([Path("a.bundle"), Path("b.bundle")], {("TextAsset", "asuna.skel")})

    def test_download_checksum_failure_preserves_existing_file(self):
        """损坏下载不能覆盖现有缓存，也不能遗留可误用的半文件。"""
        with test_directory() as directory:
            target = Path(directory) / "bundle"
            target.write_bytes(b"previous")
            response = SimpleNamespace(raise_for_status=lambda: None, iter_content=lambda size: iter([b"corrupt"]))
            session = unittest.mock.MagicMock()
            session.get.return_value.__enter__.return_value = response
            with self.assertRaisesRegex(RuntimeError, "校验失败"):
                download_file(session, "https://example.invalid/bundle", target, md5="0" * 32)
            self.assertEqual(target.read_bytes(), b"previous")
            self.assertFalse(target.with_name("bundle.part").exists())


class UploadPublicationTests(unittest.TestCase):
    """在不访问 R2 的情况下验证发布顺序和失败时的边界。"""

    def write_candidate(self, directory):
        """创建资源、清单和更新标记，模拟一个最小候选目录。"""
        root = Path(directory)
        for name in ("AssetBundles/Android/character.bundle", "AssetBundles/Catalog/1/Android/bundleDownloadInfo.json", "AssetBundles/Catalog/1/Android/bundleDownloadInfo.hash"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        return root

    def test_dry_run_does_not_read_credentials_or_connect(self):
        """查看上传计划时不能读取凭据或创建远端连接。"""
        with test_directory() as directory:
            root = self.write_candidate(directory)
            with patch("uploadR2.required_env", side_effect=AssertionError("不能读取凭据")), patch("uploadR2.boto3.client", side_effect=AssertionError("不能连接")), patch("builtins.print"):
                upload_resources(root, "staging/asuna", True)

    def test_manifest_is_uploaded_only_after_resource_success(self):
        """资源成功后才发布清单，最后发布更新标记。"""
        with test_directory() as directory:
            root = self.write_candidate(directory)
            uploaded = []
            with patch("uploadR2.required_env", return_value="test"), patch("uploadR2.boto3.client"), patch("uploadR2.upload_file", side_effect=lambda client, bucket, path, key, config: uploaded.append(path.name)), patch("builtins.print"):
                upload_resources(root, "staging/asuna", False)
            self.assertEqual(uploaded, ["character.bundle", "bundleDownloadInfo.json", "bundleDownloadInfo.hash"])

    def test_failed_resource_upload_does_not_publish_manifest(self):
        """资源上传失败时，不进入清单和标记的发布阶段。"""
        with test_directory() as directory:
            root = self.write_candidate(directory)
            with patch("uploadR2.required_env", return_value="test"), patch("uploadR2.boto3.client"), patch("uploadR2.upload_file", side_effect=RuntimeError("模拟上传失败")) as uploader:
                with self.assertRaisesRegex(RuntimeError, "模拟上传失败"):
                    upload_resources(root, "staging/asuna", False)
            self.assertEqual(uploader.call_count, 1)
            self.assertEqual(uploader.call_args.args[2].name, "character.bundle")


if __name__ == "__main__":
    unittest.main()
