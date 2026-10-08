"""读取 BA-AD v3.1.0 使用的日服 MemoryPack 资源清单。"""

import struct
from pathlib import Path


class CatalogReader:
    """按固定结构读取清单，格式变化时停止，避免错误选择资源。"""

    def __init__(self, content: bytes):
        self.content = content
        self.position = 0

    def number(self, format: str):
        """读取小端数值，截断清单由 struct 报错。"""
        value = struct.unpack_from("<" + format, self.content, self.position)[0]
        self.position += struct.calcsize("<" + format)
        return value

    def header(self, count: int) -> None:
        """核对对象字段数量，不接受未知版本的结构。"""
        actual = self.number("B")
        if actual != count:
            raise ValueError(f"日服清单对象格式变化: {actual} != {count}")

    def string(self) -> str | None:
        """读取 MemoryPack 的 UTF-8 或 UTF-16 字符串。"""
        length = self.number("i")
        if length == -1:
            return None
        if length < -1:
            self.number("i")
            size, encoding = ~length, "utf-8"
        else:
            size, encoding = length * 2, "utf-16-le"
        end = self.position + size
        if end > len(self.content):
            raise ValueError("日服清单字符串被截断。")
        value = self.content[self.position:end].decode(encoding)
        self.position = end
        return value

    def bundle(self) -> dict:
        """读取文件名、大小和官方摘要等七个字段。"""
        self.header(7)
        return {
            "Name": self.string(), "Size": self.number("q"),
            "IsPrologue": self.number("?"), "Crc": self.number("q"),
            "IsSplitDownload": self.number("?"), "FileHash": self.number("Q"),
            "Signature": self.string(),
        }

    def pack(self) -> list[dict]:
        """跳过 ZIP 包信息，读取其中的 Bundle 条目。"""
        self.header(6)
        self.string()
        self.number("q")
        self.number("q")
        self.number("?")
        self.number("?")
        return [self.bundle() for _ in range(self.number("i"))]


def read_jp_catalog(path: Path) -> list[dict]:
    """展开全量及更新包，并核对重复条目的大小与摘要。"""
    reader = CatalogReader(path.read_bytes())
    try:
        reader.header(4)
        reader.string()
        reader.number("i")
        entries = {}
        for _ in range(2):
            for _ in range(reader.number("i")):
                for entry in reader.pack():
                    name = entry["Name"]
                    if not name or Path(name).name != name or any(char in name for char in ("/", "\\", ":")):
                        raise ValueError(f"清单包名包含路径: {name}")
                    previous = entries.get(name)
                    if previous and (previous["Size"], previous["Crc"]) != (entry["Size"], entry["Crc"]):
                        raise ValueError(f"日服清单同名条目内容冲突: {name}")
                    entries[name] = entry
        if reader.position != len(reader.content):
            raise ValueError("日服清单有无法解析的尾部内容。")
    except struct.error as exc:
        raise ValueError("日服清单被截断或格式变化。") from exc
    return list(entries.values())
