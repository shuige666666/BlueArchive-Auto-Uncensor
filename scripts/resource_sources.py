"""负责下载器准备、官方资源版本查询和经过校验的文件下载。"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import zipfile
import zlib
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT_DIR = Path(__file__).resolve().parent.parent


def sha256_file(path: Path) -> str:
    """计算文件摘要，记录来源并校验外部下载工具。"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def create_session() -> requests.Session:
    """创建带有限重试的下载会话，沿用用户已有代理配置。"""
    session = requests.Session()
    session.headers["User-Agent"] = "BlueArchive-CharacterResources/1.0"
    retry = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def save_json(path: Path, value: object) -> None:
    """以 UTF-8 原子写入报告，避免中断留下半份 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def download_file(
    session: requests.Session,
    url: str,
    target: Path,
    *,
    md5: str | None = None,
    size: int | None = None,
    sha256: str | None = None,
) -> None:
    """下载到临时文件，校验后再替换；缓存只在已知摘要一致时复用。"""
    def valid(path: Path) -> bool:
        """确认大小与调用方提供的摘要一致。"""
        if not path.is_file() or (size is not None and path.stat().st_size != size):
            return False
        if md5:
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "md5").hexdigest() != md5.lower():
                    return False
        return sha256 is None or sha256_file(path) == sha256.lower()

    if (md5 or sha256) and valid(target):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    try:
        with session.get(url, stream=True, timeout=(15, 120)) as response:
            response.raise_for_status()
            with temporary.open("wb") as stream:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        stream.write(chunk)
        if not valid(temporary):
            raise RuntimeError(f"下载文件校验失败: {url}")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_baad(session: requests.Session, cache_dir: Path) -> tuple[Path, dict]:
    """准备固定版本 BA-AD，执行前校验发布包及其中的可执行文件。"""
    config = json.loads((ROOT_DIR / "config/download-tools.json").read_text(encoding="utf-8"))["baad"]
    machine = platform.machine().lower()
    if machine not in ("amd64", "x86_64"):
        raise RuntimeError("当前仅配置 Windows/Linux x86_64；请补充已核验的发布包。")
    system = platform.system().lower()
    release = config["releases"].get(f"{system}-x86_64")
    if release is None:
        raise RuntimeError(f"尚未配置此平台的下载工具: {system}")
    directory = cache_dir / "tools" / f"baad-v{config['version']}"
    archive = directory / release["file"]
    url = f"{config['repository']}/releases/download/v{config['version']}/{release['file']}"
    download_file(session, url, archive, sha256=release["sha256"])
    executable = directory / ("baad.exe" if system == "windows" else "baad")
    with zipfile.ZipFile(archive) as bundle:
        member = next((item for item in bundle.infolist() if Path(item.filename).name == executable.name), None)
        if member is None:
            raise RuntimeError("BA-AD 发布包中没有预期的可执行文件。")
        # 只提取预期文件，防止 ZIP 中的相对路径逃出缓存目录。
        content = bundle.read(member)
    expected = hashlib.sha256(content).hexdigest()
    if not executable.exists() or sha256_file(executable) != expected:
        executable.write_bytes(content)
    if system != "windows":
        executable.chmod(0o755)
    return executable, {
        "version": config["version"],
        "source_commit": config["source_commit"],
        "url": url,
        "sha256": release["sha256"],
    }


def baad_data_dir() -> Path:
    """定位 BA-AD 官方默认元数据缓存，不更改系统环境变量。"""
    if platform.system() == "Windows":
        return Path(os.environ["LOCALAPPDATA"]) / "baad"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "baad"


def run_baad(executable: Path, pattern: str, output: Path, log: Path, proxy: str | None, limit: int = 4) -> None:
    """按名称筛选下载，完整诊断写入日志，不下载无关资源。"""
    command = [
        str(executable), "download", "japan", "--assets",
        "--filter", pattern, "--filter-method", "regex",
        "--limit", str(limit), "--retries", "3", "--output", str(output),
    ]
    if proxy:
        command.extend(["--proxy", proxy])
    with log.open("a", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=ROOT_DIR, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"日服下载失败，退出码 {result.returncode}，详见 {log}")


def fetch_jp_catalog(session, cache: Path, *, proxy=None, offline=False) -> tuple[list[dict], dict]:
    """只刷新日服清单；离线模式核对缓存快照后读取。"""
    from jp_catalog import read_jp_catalog

    local_catalog = cache / "jp-catalog.bytes"
    if offline:
        metadata = json.loads((cache / "jp-source.json").read_text(encoding="utf-8"))
        if not local_catalog.exists():
            original = baad_data_dir() / "catalog/japan/android/BundlePackingInfo.bytes"
            if sha256_file(original) != metadata["catalog_sha256"]:
                raise RuntimeError("没有与离线快照一致的日服清单。")
            shutil.copy2(original, local_catalog)
        if sha256_file(local_catalog) != metadata["catalog_sha256"]:
            raise RuntimeError("离线日服清单摘要不符。")
    else:
        executable, tool = prepare_baad(session, cache)
        log = cache / "jp-download.log"
        print("[JP] 刷新日服清单", flush=True)
        run_baad(executable, r"^$", cache / "jp-metadata", log, proxy)
        original = baad_data_dir() / "catalog/japan/android/BundlePackingInfo.bytes"
        metadata = json.loads((baad_data_dir() / "api_data.json").read_text(encoding="utf-8"))["japan"]
        metadata.update(tool=tool, catalog_sha256=sha256_file(original))
        shutil.copy2(original, local_catalog)
    entries = read_jp_catalog(local_catalog)
    directory = cache / "jp" / metadata["catalog_sha256"][:16] / "AssetBundles"
    directory.mkdir(parents=True, exist_ok=True)
    metadata.update(
        bundle_dir=directory.relative_to(ROOT_DIR).as_posix(),
        download_log=(cache / "jp-download.log").relative_to(ROOT_DIR).as_posix(),
    )
    save_json(cache / "jp-source.json", metadata)
    return entries, metadata


def ensure_jp_bundles(session, cache: Path, entries: list[dict], metadata: dict, *, proxy=None, offline=False) -> dict:
    """复用经过摘要核验的原包，仅让 BA-AD 下载缺失或更新的文件。"""
    import re

    directory = ROOT_DIR / metadata["bundle_dir"]
    index_path = cache / "jp-file-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    missing = []
    reused = 0

    def valid_bundle(path: Path, entry: dict) -> bool:
        """日服条目 CRC 为文件 CRC32，独立核对 BA-AD 的实际下载结果。"""
        if not path.is_file() or path.stat().st_size != entry["Size"]:
            return False
        crc = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                crc = zlib.crc32(chunk, crc)
        return crc == entry["Crc"]

    for entry in entries:
        name = entry["Name"]
        target = directory / name
        record = index.get(name)
        fingerprint = [entry["Size"], entry["Crc"]]
        if record and record["fingerprint"] == fingerprint:
            previous = (ROOT_DIR / record["file"]).resolve()
            if not previous.is_relative_to(ROOT_DIR):
                raise ValueError("日服缓存索引指向工作区外。")
            if valid_bundle(previous, entry) and sha256_file(previous) == record["sha256"]:
                if previous != target.resolve():
                    shutil.copy2(previous, target)
                index[name] = {**record, "file": target.relative_to(ROOT_DIR).as_posix()}
                reused += 1
                continue
        # 同一官方快照的已有下载也可纳入索引；随后始终核对本地 SHA。
        if valid_bundle(target, entry) and record is None:
            index[name] = {"fingerprint": fingerprint, "sha256": sha256_file(target), "file": target.relative_to(ROOT_DIR).as_posix()}
            reused += 1
            continue
        if offline:
            raise RuntimeError(f"离线日服原包缺失或摘要不符: {name}")
        target.unlink(missing_ok=True)
        missing.append(entry)

    if missing:
        executable, _ = prepare_baad(session, cache)
        print(f"[JP] 复用 {reused} 个包，下载 {len(missing)} 个包", flush=True)
        # Windows 命令行长度有限，按字符数拆分精确文件名过滤表达式。
        batches, batch, length = [], [], 0
        for entry in missing:
            escaped = re.escape(entry["Name"])
            if length + len(escaped) > 8000 and batch:
                batches.append(batch)
                batch, length = [], 0
            batch.append(entry)
            length += len(escaped) + 1
        if batch:
            batches.append(batch)
        for batch in batches:
            pattern = "^(?:" + "|".join(re.escape(entry["Name"]) for entry in batch) + ")$"
            run_baad(executable, pattern, directory.parent, cache / "jp-download.log", proxy)
            failed = [entry for entry in batch if not valid_bundle(directory / entry["Name"], entry)]
            if failed:
                # BA-AD 部分失败可能仍退出 0；降低并发，只重试失败条目。
                print(f"[JP] 独立校验发现 {len(failed)} 个失败包，低并发重试", flush=True)
                for entry in failed:
                    (directory / entry["Name"]).unlink(missing_ok=True)
                retry_pattern = "^(?:" + "|".join(re.escape(entry["Name"]) for entry in failed) + ")$"
                run_baad(executable, retry_pattern, directory.parent, cache / "jp-download.log", proxy, limit=2)
            original = baad_data_dir() / "catalog/japan/android/BundlePackingInfo.bytes"
            if sha256_file(original) != metadata["catalog_sha256"]:
                raise RuntimeError("下载期间日服清单变化，请重新运行。")
            for entry in batch:
                target = directory / entry["Name"]
                if not valid_bundle(target, entry):
                    save_json(index_path, index)
                    raise RuntimeError(f"日服下载文件缺失、尺寸或 CRC 不符: {entry['Name']}")
                index[entry["Name"]] = {
                    "fingerprint": [entry["Size"], entry["Crc"]],
                    "sha256": sha256_file(target), "file": target.relative_to(ROOT_DIR).as_posix(),
                }
            save_json(index_path, index)
    save_json(index_path, index)
    return {"downloaded_files": len(missing), "downloaded_payload_bytes": sum(entry["Size"] for entry in missing), "reused_files": reused}


def download_jp(session, cache_dir: Path, bundle_keys: list[str], *, proxy=None) -> tuple[Path, dict]:
    """获取指定资源组，同时复用未变化的日服包。"""
    from replaceTexture2D import normalize_bundle_name

    entries, metadata = fetch_jp_catalog(session, cache_dir, proxy=proxy)
    selected = [entry for entry in entries if normalize_bundle_name(Path(entry["Name"])) in bundle_keys]
    ensure_jp_bundles(session, cache_dir, selected, metadata, proxy=proxy)
    return ROOT_DIR / metadata["bundle_dir"], metadata


def fetch_cn_catalog(session: requests.Session, cache_dir: Path) -> tuple[dict, dict]:
    """从国服官方接口读取实际下载根地址与当前资源清单。"""
    import re

    setup_url = "https://bluearchive-cn.com/api/meta/setup"
    response = session.get(setup_url, timeout=30)
    response.raise_for_status()
    match = re.search(r"\d+\.\d+\.\d+", response.text)
    if match is None:
        raise RuntimeError("国服 setup 接口中没有 GameVersion。")
    state_url = "https://gs-api.bluearchive-cn.com/api/state"
    response = session.get(state_url, headers={"APP-VER": match.group(), "PLATFORM-ID": "1", "CHANNEL-ID": "2"}, timeout=30)
    response.raise_for_status()
    state = response.json()
    root = state["AddressablesCatalogUrlRoots"][0].rstrip("/")
    catalog_url = f"{root}/AssetBundles/Catalog/{state['ResourceVersion']}/Android/bundleDownloadInfo.json"
    response = session.get(catalog_url, timeout=60)
    response.raise_for_status()
    catalog = response.json()
    metadata = {
        "game_version": match.group(),
        "resource_version": str(state["ResourceVersion"]),
        "root_url": root,
        "state_url": state_url,
        "catalog_url": catalog_url,
    }
    save_json(cache_dir / "cn-source.json", metadata)
    save_json(cache_dir / "cn-catalog.json", catalog)
    return catalog, metadata
