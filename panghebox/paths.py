"""定位胖盒云电脑的安装目录、配置文件和账号数据。

本模块只做「发现」——不修改任何东西。所有路径都允许被命令行参数覆盖,
因为安装位置和用户名在不同机器上都不一样。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# 应用进程名(用于检测是否正在运行)
PROCESS_NAME = "panghebox.exe"

# 安装目录下用于识别「这确实是胖盒安装目录」的标志文件
INSTALL_MARKERS = ("panghebox.exe", "data/app.so")

# 注册表位置(仅 Windows)。ClientKey / app_path 存这里。
REG_PATH = r"HKLM\SOFTWARE\PangHeBox"


class PathError(RuntimeError):
    """无法定位应用目录或数据目录。"""


def roaming_appdata() -> Path:
    """返回 %APPDATA%。"""
    val = os.environ.get("APPDATA")
    if not val:
        raise PathError("环境变量 APPDATA 不存在,无法定位账号数据目录")
    return Path(val)


def prefs_path() -> Path:
    """返回 Flutter SharedPreferences 文件的完整路径。

    这是账号登录态真正落地的地方,含 uid / token / userphone 等。
    """
    return roaming_appdata() / "Panghebox" / "panghebox" / "shared_preferences.json"


def prefs_dir() -> Path:
    """返回 prefs 所在目录,同时是 wechat_webview2 的父目录。"""
    return prefs_path().parent


def wechat_webview_dir() -> Path:
    """微信登录会话目录(WebView2)。

    用微信扫码登录的账号,凭据在这里而不在 prefs 里。
    """
    return prefs_dir() / "wechat_webview2"


def _looks_like_install(p: Path) -> bool:
    """判断目录是否是胖盒安装目录(靠标志文件)。"""
    if not p.is_dir():
        return False
    return all((p / marker).exists() for marker in INSTALL_MARKERS)


def _candidate_install_dirs() -> Iterable[Path]:
    """依次产出可能的安装目录,越靠前可信度越高。"""
    # 1) 注册表里记录的真实安装路径
    reg_path = _read_registry_app_path()
    if reg_path:
        yield reg_path

    # 2) 环境变量显式指定
    env = os.environ.get("PANGHEBOX_HOME")
    if env:
        yield Path(env)

    # 3) 本工具自己的仓库相邻位置 / 常见盘符下的默认安装名
    here = Path(__file__).resolve()
    for parent in here.parents:
        for name in ("PangHeBox", "胖盒云电脑"):
            yield parent / name

    # 4) 常见根目录 + 默认目录名
    for drive in ("C:", "D:", "E:", "F:"):
        root = Path(f"{drive}/")
        if not root.exists():
            continue
        yield root / "胖盒云电脑" / "PangHeBox"
        yield root / "PangHeBox"
        yield root / "Program Files" / "PangHeBox"
        yield root / "Program Files (x86)" / "PangHeBox"


def find_install_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """定位安装目录。

    Args:
        explicit: 用户显式指定的路径,优先级最高。

    Raises:
        PathError: 所有候选都不像安装目录时。
    """
    if explicit:
        p = Path(explicit)
        if not _looks_like_install(p):
            raise PathError(
                f"指定的目录不像胖盒安装目录(缺少 {' 或 '.join(INSTALL_MARKERS)}): {p}"
            )
        return p

    seen: set[Path] = set()
    for cand in _candidate_install_dirs():
        try:
            cand = cand.resolve()
        except OSError:
            continue
        if cand in seen:
            continue
        seen.add(cand)
        if _looks_like_install(cand):
            return cand

    raise PathError(
        "找不到胖盒云电脑安装目录。请用 --install-dir 显式指定,\n"
        "或设置环境变量 PANGHEBOX_HOME 指向含 panghebox.exe 的目录。"
    )


def exe_path(install_dir: Path) -> Path:
    """返回主程序路径。"""
    return install_dir / PROCESS_NAME


def _read_registry_app_path() -> Path | None:
    """从注册表读取 app_path。非 Windows 或读取失败时返回 None。"""
    if os.name != "nt":
        return None
    try:
        import winreg  # noqa: PLC0415  (仅 Windows 可用,延迟导入)
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\PangHeBox") as key:
            val, _ = winreg.QueryValueEx(key, "app_path")
    except OSError:
        return None
    if not val:
        return None
    p = Path(val)
    # 注册表存的是 exe 完整路径,取父目录
    return p.parent if p.suffix.lower() == ".exe" else p


def read_registry_channel() -> str | None:
    """读出安装渠道标识(ClientKey),用于诊断。"""
    if os.name != "nt":
        return None
    try:
        import winreg  # noqa: PLC0415
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\PangHeBox") as key:
            val, _ = winreg.QueryValueEx(key, "ClientKey")
    except OSError:
        return None
    return val or None


def read_registry_version() -> str | None:
    """读出客户端版本号(app_version),作为 Version 请求头使用。"""
    if os.name != "nt":
        return None
    try:
        import winreg  # noqa: PLC0415
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\PangHeBox") as key:
            val, _ = winreg.QueryValueEx(key, "app_version")
    except OSError:
        return None
    return val or None


_MACHINE_ID_RE = re.compile(r"machine_id\s*-{0,3}>\s*([0-9a-fA-F-]{36})")


def find_machine_id_from_logs(install_dir: Path | None = None) -> str | None:
    """从最近的日志里捞 machine_id。

    仅作为 prefs 不可读时的兜底——正常情况 machine_id 直接从 prefs 拿。
    """
    if install_dir is None:
        return None
    log_dir = install_dir / "log"
    if not log_dir.is_dir():
        return None
    logs = sorted(log_dir.glob("nika-*-log.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    for log in logs[:3]:
        try:
            text = log.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = _MACHINE_ID_RE.search(text)
        if m:
            return m.group(1)
    return None


@dataclass(frozen=True)
class AppContext:
    """一次运行中用到的全部路径,集中传递避免到处重新探测。"""

    install_dir: Path
    prefs: Path
    exe: Path

    @classmethod
    def discover(cls, install_dir: str | os.PathLike[str] | None = None) -> "AppContext":
        inst = find_install_dir(install_dir)
        return cls(install_dir=inst, prefs=prefs_path(), exe=exe_path(inst))

    def describe(self) -> str:
        return (
            f"安装目录: {self.install_dir}\n"
            f"主程序:   {self.exe}\n"
            f"账号文件: {self.prefs}"
        )
