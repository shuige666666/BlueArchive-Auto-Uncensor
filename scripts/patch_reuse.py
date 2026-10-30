"""复用已校验补丁，并为新国服版本生成配套的混合清单。"""

import copy
import hashlib
import json
import re
import shutil
import zlib
from datetime import datetime, timezone
from pathlib import Path

import UnityPy
from PIL import __version__ as pillow_version

from replaceTexture2D import normalize_bundle_name
from resource_sources import ROOT_DIR, save_json, sha256_file

# 修改素材写入规则时递增；旧版 cn_objects 报告采用规则版本 3。
PATCH_FORMAT_VERSION = 3


def builder_signature():
    """记录影响输出的处理规则与依赖版本，避免跨规则复用。"""
    return {"patch_format": PATCH_FORMAT_VERSION, "unitypy": UnityPy.__version__, "pillow": pillow_version}


def bundle_input_fingerprint(original, sources, keys, digests):
    """以实际输入内容而非全服版本号判断一个包是否需要重建。"""
    def digest(path):
        """同一轮共享原包只计算一次摘要。"""
        if path not in digests:
            digests[path] = sha256_file(path)
        return digests[path]
    inputs = {"builder": builder_signature(), "cn": [original.name, digest(original)],
              "jp": sorted((p.name, digest(p)) for p in sources), "objects": sorted(keys)}
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def safe_path(root, value):
    """限制报告引用的文件在工作区内，防止缓存路径越界。"""
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"补丁报告路径越界: {value}")
    return path


def valid_bundle_name(name):
    """官方文件名只允许单层文件，禁止复制时逃出资源目录。"""
    return bool(name) and Path(name).name == name and name not in (".", "..") and not any(c in name for c in "/\\:\x00")


def verified_bundle(previous, record, root=ROOT_DIR):
    """核验旧产物的实际字节，损坏或缺失时由调用方回源或重建。"""
    if not previous.get("structural_validation_passed") or not valid_bundle_name(record["name"]):
        return None
    directory = safe_path(root, previous["output_dir"])
    source = (directory / "AssetBundles/Android" / record["name"]).resolve()
    if not source.is_relative_to(directory) or not source.is_file() or source.stat().st_size != record["size"]:
        return None
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
            md5.update(chunk)
    if sha.hexdigest() != record["output_sha256"]:
        return None
    if record.get("md5") and md5.hexdigest() != record["md5"]:
        return None
    return source, md5.hexdigest()


def copy_verified_bundle(previous, record, verified, target):
    """复制到新快照，旧快照删除后也不影响复用文件。"""
    source, md5 = verified
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    shutil.copyfile(source, temporary)
    temporary.replace(target)
    return {**copy.deepcopy(record), "md5": md5, "reused_from": previous["run_id"]}


def reuse_built_bundle(previous, record, fingerprint, target, root=ROOT_DIR):
    """构建输入完全相同时复用文件，省去 Unity 解包、编码和重打包。"""
    if not record or record.get("input_fingerprint") != fingerprint:
        return None
    verified = verified_bundle(previous, record, root)
    return copy_verified_bundle(previous, record, verified, target) if verified else None


def original_signature(previous, record, root, cache):
    """读取原包校验记录，旧报告则用缓存原包摘要进行一次兼容迁移。"""
    if record.get("original_md5") and "original_size" in record:
        return record["original_md5"].lower(), record["original_size"]
    original = safe_path(root, cache / "cn" / previous["cn_source"]["resource_version"] / record["name"])
    if not original.is_file() or sha256_file(original) != record.get("original_sha256"):
        return None
    with original.open("rb") as stream:
        return hashlib.file_digest(stream, "md5").hexdigest(), original.stat().st_size


def bundle_family(name):
    """立绘纹理和图集骨骼必须成组复用，避免只回退一半导致显示异常。"""
    key = normalize_bundle_name(Path(name))
    if key.startswith("assets-_mx-spinecharacters-"):
        return re.sub(r"-_mxdependency-(textures|textassets)$", "", key)
    return key


def write_catalog(output, version, catalog):
    """清单与标记在全部包准备后生成，保证下载校验与实际文件一致。"""
    path = output / f"AssetBundles/Catalog/{version}/Android/bundleDownloadInfo.json"
    save_json(path, catalog)
    path.with_suffix(".hash").write_text(str(zlib.crc32(path.read_bytes())), encoding="utf-8")


def prepare_compatible_patch(previous, catalog, version, upstream, root=ROOT_DIR,
                             cache=Path(".cache/character-resources")):
    """快速保留与新国服原包兼容的旧补丁，其余条目使用新官方原包。"""
    if not re.fullmatch(r"\d{12}", version):
        raise ValueError("国服清单版本格式错误")
    entries = {entry["Name"]: entry for entry in catalog["BundleFiles"]}
    if len(entries) != len(catalog["BundleFiles"]):
        raise ValueError("新版官方清单含重复文件名")
    compatible, verified, invalid_families = {}, {}, set()
    previous_builder = previous.get("builder_signature", {
        "patch_format": 3, "unitypy": previous.get("runtime", {}).get("unitypy"),
        "pillow": previous.get("runtime", {}).get("pillow")})
    for record in previous.get("bundles", []):
        name = record["name"]
        if not valid_bundle_name(name):
            raise ValueError(f"补丁文件名包含路径: {name}")
        official = entries.get(name)
        original = original_signature(previous, record, root, cache)
        valid = (previous_builder == builder_signature() and record.get("replacement_mode") == "cn_objects"
                 and official is not None and original == (official["Crc"].lower(), official["Size"]))
        proof = verified_bundle(previous, record, root) if valid else None
        if proof:
            compatible[name], verified[name] = record, proof
        else:
            invalid_families.add(bundle_family(name))

    # 生成独立快照，不改正在下载的文件，也不覆盖后台扫描使用的官方缓存。
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = safe_path(root, Path("build/character-resources") / run_id / "modified")
    updated = copy.deepcopy(catalog)
    updated_entries = {entry["Name"]: entry for entry in updated["BundleFiles"]}
    reused = []
    for name, record in compatible.items():
        if bundle_family(name) in invalid_families:
            continue
        result = copy_verified_bundle(previous, record, verified[name], output / "AssetBundles/Android" / name)
        md5, size = original_signature(previous, record, root, cache)
        result.update(original_md5=md5, original_size=size)
        updated_entries[name].update(Crc=result["md5"], Size=result["size"])
        reused.append(result)
    write_catalog(output, version, updated)
    report = {"schema_version": 1, "run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(),
        "output_dir": output.relative_to(root).as_posix(), "structural_validation_passed": True,
        "partial": True, "builder_signature": previous_builder, "runtime": previous.get("runtime", {}),
        "cn_source": {**previous["cn_source"], "resource_version": version, "root_url": upstream,
                      "catalog_url": f"{upstream}/AssetBundles/Catalog/{version}/Android/bundleDownloadInfo.json"},
        "jp_source": previous.get("jp_source", {}), "characters": previous.get("characters", []),
        "bundles": reused, "build_stats": {"reused_bundles": len(reused), "rebuilt_bundles": 0,
            "official_bundles": sum(record["name"] in entries for record in previous.get("bundles", [])) - len(reused)},
        "game_client_verified": False, "applied": False}
    save_json(output.parent / "report.json", report)
    return report
