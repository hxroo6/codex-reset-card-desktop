"""Read-only version checks; unsupported binaries never receive schedule edits."""
from functools import lru_cache
from pathlib import Path
import subprocess

from app_config import SUPPORTED_UPSTREAM_VERSION


@lru_cache(maxsize=8)
def _inspect(path, modified, size):
    try:
        result = subprocess.run([path, "version"], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=5,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        version = result.stdout.strip() if result.returncode == 0 else ""
        supported = version == SUPPORTED_UPSTREAM_VERSION
        return {"version": version or "未知", "supported": supported,
                "message": "" if supported else "后台版本不兼容；仅允许修改已验证的 " + SUPPORTED_UPSTREAM_VERSION + " 版计划"}
    except (OSError, subprocess.SubprocessError):
        return {"version": "未知", "supported": False, "message": "后台版本检查失败，暂不可推迟"}


def inspect_upstream(binary):
    try:
        path = Path(binary).resolve()
        info = path.stat()
        return dict(_inspect(str(path), info.st_mtime_ns, info.st_size))
    except OSError:
        return {"version": "未安装", "supported": False, "message": "未找到重置卡后台，请先安装上游工具"}
