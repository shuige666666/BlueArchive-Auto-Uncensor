"""负责匹配角色资源、在国服原包中替换内容并独立检查重打包结果。"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path

import UnityPy
from PIL import ImageChops, ImageStat

from resource_sources import sha256_file

AssetKey = tuple[str, str]


def text_bytes(data) -> bytes:
    """无损读取 TextAsset；骨骼文件的非 UTF-8 字节用 surrogateescape 保留。"""
    value = data.m_Script
    return value.encode("utf-8", "surrogateescape") if isinstance(value, str) else bytes(value)


def asset_digest(obj) -> str:
    """按解码内容计算摘要，区分图片像素差异与资源包封装差异。"""
    data = obj.read()
    if obj.type.name == "Texture2D":
        image = data.image.convert("RGBA")
        content = f"{image.width}x{image.height}:".encode() + image.tobytes()
    else:
        content = text_bytes(data)
    return hashlib.sha256(content).hexdigest()


def find_assets(bundle_paths: list[Path], required: set[AssetKey]) -> dict[AssetKey, tuple[Path, object]]:
    """按对象类型和名称精确匹配；多个不同来源同名时停止，防止误选皮肤。"""
    found: dict[AssetKey, tuple[Path, object]] = {}
    for path in sorted(bundle_paths):
        env = UnityPy.load(str(path))
        for obj in env.objects:
            if obj.type.name not in ("Texture2D", "TextAsset"):
                continue
            name = obj.peek_name()
            key = (obj.type.name, name)
            if key not in required:
                continue
            if key in found:
                previous = found[key]
                if asset_digest(previous[1]) != asset_digest(obj):
                    raise RuntimeError(f"存在内容不同的同名资源 {key}: {previous[0].name}, {path.name}")
                continue
            found[key] = (path, obj)
    missing = required - found.keys()
    if missing:
        raise RuntimeError(f"缺少需要的资源，不能生成完整补丁: {sorted(missing)}")
    return found


def export_assets(assets: dict[AssetKey, tuple[Path, object]], directory: Path) -> list[dict]:
    """导出所选 PNG、图集和骨骼数据，并逐项保存可追溯的来源。"""
    records = []
    for (kind, name), (bundle, obj) in sorted(assets.items()):
        # 游戏对象名不得成为目录或绝对路径，防止解包写到工作区之外。
        if Path(name).name != name or any(char in name for char in ("/", "\\", ":")) or name in (".", ".."):
            raise RuntimeError(f"资源名称包含不安全路径: {name}")
        filename = name + ".png" if kind == "Texture2D" else name
        output = directory / filename
        output.parent.mkdir(parents=True, exist_ok=True)
        data = obj.read()
        if kind == "Texture2D":
            data.image.save(output)
        else:
            output.write_bytes(text_bytes(data))
        records.append({
            "type": kind,
            "name": name,
            "filename": filename,
            "source_bundle": bundle.name,
            "source_bundle_sha256": sha256_file(bundle),
            "source_path_id": obj.path_id,
            "file_sha256": sha256_file(output),
            "content_sha256": asset_digest(obj),
        })
    return records


def object_inventory(env) -> dict[tuple[str, int], str]:
    """记录原包对象标识和原始内容，用于确认未选中的对象没有被改写。"""
    return {(obj.assets_file.name, obj.path_id): hashlib.sha256(obj.get_raw_data()).hexdigest() for obj in env.objects}


def patch_bundle(
    source_bundle: Path,
    source_assets: dict[AssetKey, tuple[Path, object]],
    keys: set[AssetKey],
    output: Path,
    preview_dir: Path,
) -> dict:
    """保留国服包的对象和引用，只替换选定内容；保存后重新加载独立验证。"""
    env = UnityPy.load(str(source_bundle))
    before = object_inventory(env)
    targets = {}
    selected_ids = set()
    changes = []
    for obj in env.objects:
        if obj.type.name not in ("Texture2D", "TextAsset"):
            continue
        key = (obj.type.name, obj.peek_name())
        if key not in keys:
            continue
        if key in targets:
            raise RuntimeError(f"国服包中存在多个同名目标: {key}")
        targets[key] = obj
        selected_ids.add((obj.assets_file.name, obj.path_id))
    if set(targets) != keys:
        raise RuntimeError(f"国服原包缺少目标对象: {keys - targets.keys()}")

    # 1. 写入源资源内容，保持国服对象的 path_id 和贴图格式。
    for key, target in targets.items():
        source = source_assets[key][1]
        data = target.read()
        source_data = source.read()
        previous_digest = asset_digest(target)
        record = {
            "type": key[0],
            "name": key[1],
            "path_id": target.path_id,
            "before_sha256": previous_digest,
            "source_sha256": asset_digest(source),
        }
        if key[0] == "Texture2D":
            source_image = source_data.image.convert("RGBA")
            original_size = (data.m_Width, data.m_Height)
            if source_image.size != original_size:
                raise RuntimeError(f"贴图尺寸不一致，需要人工适配: {key[1]} JP={source_image.size}, CN={original_size}")
            record.update(size=list(original_size), texture_format=data.m_TextureFormat)
            preview_dir.mkdir(parents=True, exist_ok=True)
            data.image.save(preview_dir / f"{key[1]}-cn.png")
            source_image.save(preview_dir / f"{key[1]}-jp.png")
            data.image = source_image
        else:
            data.m_Script = text_bytes(source_data).decode("utf-8", "surrogateescape")
        data.save()
        changes.append(record)

    # 2. 输出国服文件名的 Bundle；不直接将跨服整包覆盖到国服。
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".part")
    temporary.write_bytes(env.file.save(packer="original"))

    # 3. 从输出字节重新解析，验证对象标识和未修改对象的原始字节。
    rebuilt = UnityPy.load(temporary.read_bytes())
    after = object_inventory(rebuilt)
    if before.keys() != after.keys():
        raise RuntimeError(f"重打包改变了对象标识: {source_bundle.name}")
    unchanged_ids = before.keys() - selected_ids
    if any(before[key] != after[key] for key in unchanged_ids):
        raise RuntimeError(f"重打包改变了未选择的对象: {source_bundle.name}")
    rebuilt_assets = {}
    for obj in rebuilt.objects:
        if obj.type.name in ("Texture2D", "TextAsset"):
            key = (obj.type.name, obj.peek_name())
            if key in keys:
                rebuilt_assets[key] = obj
    if set(rebuilt_assets) != keys:
        raise RuntimeError("重打包后目标资源丢失。")
    for record in changes:
        key = (record["type"], record["name"])
        target = rebuilt_assets[key]
        if target.path_id != record["path_id"]:
            raise RuntimeError(f"目标对象标识改变: {key}")
        record["output_sha256"] = asset_digest(target)
        record["differs_from_cn"] = record["source_sha256"] != record["before_sha256"]
        if key[0] == "TextAsset":
            if record["output_sha256"] != record["source_sha256"]:
                raise RuntimeError(f"文本或骨骼资源未无损写入: {key}")
        else:
            actual = target.read().image.convert("RGBA")
            expected = source_assets[key][1].read().image.convert("RGBA")
            if actual.size != expected.size or target.read().m_TextureFormat != record["texture_format"]:
                raise RuntimeError(f"重打包改变了贴图尺寸或格式: {key}")
            # ETC/ASTC 再编码有损，记录像素误差供复核，不伪装成像素完全一致。
            record["mean_absolute_pixel_error_rgba"] = ImageStat.Stat(ImageChops.difference(actual, expected)).mean
            actual.save(preview_dir / f"{key[1]}-output.png")
    temporary.replace(output)
    return {
        "name": source_bundle.name,
        "original_sha256": sha256_file(source_bundle),
        "output_sha256": sha256_file(output),
        "size": output.stat().st_size,
        "object_count": len(before),
        "untargeted_object_count": len(unchanged_ids),
        "object_ids_preserved": True,
        "untargeted_objects_unchanged": True,
        "assets": changes,
    }


def group_assets_by_bundle(assets: dict[AssetKey, tuple[Path, object]]) -> dict[Path, set[AssetKey]]:
    """把目标对象按实际国服包分组，兼容收藏画像分散在多个包中的情况。"""
    result: dict[Path, set[AssetKey]] = defaultdict(set)
    for key, (path, _) in assets.items():
        result[path].add(key)
    return dict(result)
