"""还原 Spine 图集切片后比较，排除位置、旋转和裁剪空白的差异。"""

from PIL import Image


def atlas_regions(text: str, texture_name: str, image) -> dict:
    """依照 Spine atlas 格式恢复切片，按完整附件名称保留独立内容。"""
    sections, current = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            current = {"name": line, "properties": {}}
            sections.append(current)
        elif current is not None:
            key, value = line.split(":", 1)
            current["properties"][key.strip()] = value.strip()
    page = None
    regions = {}
    for section in sections:
        name, properties = section["name"], section["properties"]
        if name.lower().endswith((".png", ".jpg")) and "bounds" not in properties:
            page = name.rsplit(".", 1)[0], properties
            continue
        if not page or page[0] != texture_name:
            continue
        if "bounds" in properties:
            x, y, width, height = [int(value) for value in properties["bounds"].split(",")]
        elif "xy" in properties and "size" in properties:
            x, y = [int(value) for value in properties["xy"].split(",")]
            width, height = [int(value) for value in properties["size"].split(",")]
        else:
            continue
        page_width, page_height = [int(value) for value in page[1].get("size", f"{image.width},{image.height}").split(",")]
        sx, sy = image.width / (page_width or image.width), image.height / (page_height or image.height)
        rotation = properties.get("rotate", "0")
        degrees = 90 if rotation == "true" else 0 if rotation == "false" else int(rotation)
        packed_width, packed_height = (height, width) if degrees % 180 else (width, height)
        # atlas 的旋转为逆时针；恢复时顺时针转回，并处理低分辨率贴图页。
        crop = image.crop((round(x * sx), round(y * sy), round((x + packed_width) * sx), round((y + packed_height) * sy))).convert("RGBA")
        crop = crop.rotate(-degrees, expand=True)
        if "offsets" in properties:
            left, bottom, original_width, original_height = [int(value) for value in properties["offsets"].split(",")]
        else:
            left, bottom = [int(value) for value in properties.get("offset", "0,0").split(",")]
            original_width, original_height = [int(value) for value in properties.get("orig", f"{width},{height}").split(",")]
        size = round(original_width * sx), round(original_height * sy)
        if min(size) <= 0 or size[0] * size[1] > 32_000_000:
            raise ValueError(f"图集切片原始画布大小异常: {name}")
        # 数字后缀也是附件名的一部分，不能据此合并身体部件或不同姿态。
        key = (name, size)
        canvas = regions.setdefault(key, Image.new("RGBA", size))
        canvas.alpha_composite(crop, (round(left * sx), round((original_height - height - bottom) * sy)))
    # 同名不同画布不强行选择，只保留可明确对应的切片。
    result, ambiguous = {}, set()
    for (name, _), canvas in regions.items():
        if name in result:
            ambiguous.add(name)
        result[name] = canvas
    return {name: image for name, image in result.items() if name not in ambiguous}


def compare_spine(cn_image, jp_image, cn_atlas: str, jp_atlas: str, name: str, compare) -> tuple[dict, dict]:
    """比较还原后的共有切片，等比例导出缩放统一到较小画布。"""
    cn = atlas_regions(cn_atlas, name, cn_image)
    jp = atlas_regions(jp_atlas, name, jp_image)
    records, previews = {}, {}
    for region in sorted(cn.keys() & jp.keys()):
        left, right = cn[region], jp[region]
        aspect_left, aspect_right = left.width / left.height, right.width / right.height
        if abs(aspect_left / aspect_right - 1) > 0.02:
            records[region] = {"classification": "incompatible_region_geometry"}
            continue
        size = (min(left.width, right.width), min(left.height, right.height))
        if left.size != size:
            left = left.resize(size, Image.Resampling.LANCZOS)
        if right.size != size:
            right = right.resize(size, Image.Resampling.LANCZOS)
        records[region] = compare(left, right)
        if records[region]["classification"] == "visual_difference":
            previews[region] = left, right
    classification = "visual_difference" if previews else "small_difference"
    if not records or any(record["classification"] == "incompatible_region_geometry" for record in records.values()):
        classification = "needs_review"
    return {
        "classification": classification, "comparison_method": "atlas_regions",
        "regions": records, "jp_only_regions": sorted(jp.keys() - cn.keys()),
        "cn_only_regions": sorted(cn.keys() - jp.keys()),
    }, previews
