"""进程检测与启停。

为什么需要这一层:Flutter 的 SharedPreferences 在内存里持有完整状态,
**退出时会用内存里的值整体回写磁盘**。所以在应用运行期间修改 prefs 文件,
一关应用就被覆盖回去,切换无效。

因此切换的正确顺序是:检测进程 → 结束 → 等文件锁释放 → 写 prefs → 再启动。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .paths import PROCESS_NAME

# 被视作「应用正在运行」的进程名(小写比较)
WATCHED_PROCESSES = ("panghebox.exe", "cdesktop_rtc_host.exe", "cgame_rtc_host.exe")

# 结束进程后等待句柄释放的最长时间
DEFAULT_WAIT_SECONDS = 20.0


class ProcessError(RuntimeError):
    """进程操作失败。"""


def _decode(raw: bytes) -> str:
    """把子进程输出解码成文本。

    中文 Windows 上 tasklist 输出是 GBK(代码页 936),直接按 UTF-8 解会抛
    UnicodeDecodeError。所以按 OEM 代码页优先、再退到 UTF-8 与替换字符尝试。
    """
    if not raw:
        return ""
    encodings = []
    if is_windows():
        try:
            import ctypes  # noqa: PLC0415

            oem_cp = ctypes.windll.kernel32.GetOEMCP()
            if oem_cp:
                encodings.append(f"cp{oem_cp}")
        except Exception:  # noqa: BLE001  (取不到就用下面的兜底)
            pass
        encodings.append("gbk")
    encodings.extend(["utf-8", "latin-1"])
    for enc in encodings:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _run(cmd: list[str], *, timeout: float = 15.0) -> tuple[int, str]:
    """跑一条命令,返回 (returncode, 输出)。失败时 returncode 非 0。

    注意:故意不用 ``text=True`` —— 让 subprocess 返回 bytes,由我们按
    正确的代码页解码,避免中文系统上的 UnicodeDecodeError。
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            # 避免弹出控制台窗口
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return proc.returncode, _decode(proc.stdout) + _decode(proc.stderr)


def is_windows() -> bool:
    return os.name == "nt"


def running_pids(names: tuple[str, ...] = WATCHED_PROCESSES) -> list[int]:
    """返回正在运行的、名字在 names 里的进程 pid 列表。

    非 Windows 平台返回空列表(本工具主要面向 Windows 客户端)。
    """
    if not is_windows():
        return []
    # tasklist 的 /FI 过滤在中文系统上输出列名是本地化的,
    # 所以直接拉全量再用 python 匹配,避免依赖列名。
    code, out = _run(["tasklist", "/FO", "CSV", "/NH"])
    if code != 0:
        raise ProcessError(f"tasklist 执行失败: {out.strip()}")

    wanted = {n.lower() for n in names}
    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        # CSV 行: "映像名称","PID","会话名","会话#","内存使用"
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 2:
            continue
        name = parts[0].strip('"').lower()
        if name in wanted:
            try:
                pids.append(int(parts[1]))
            except ValueError:
                continue
    return pids


def app_is_running() -> bool:
    """主程序是否在运行。"""
    return bool(running_pids((PROCESS_NAME,)))


def any_running() -> bool:
    """是否任意相关进程还在。"""
    return bool(running_pids())


def kill_app(*, timeout: float = DEFAULT_WAIT_SECONDS, force_after: float = 5.0) -> bool:
    """结束客户端进程,直到全部退出或超时。

    先温和结束(taskkill 不带 /F),给应用机会正常保存状态;
    一段时间后仍未退出再强杀。

    Args:
        timeout: 总共等待的最长时间(秒)。
        force_after: 多少秒后升级为强杀。

    Returns:
        True 表示最终确认已全部退出。

    Raises:
        ProcessError: 非 Windows 平台。
    """
    if not is_windows():
        raise ProcessError("自动结束进程仅支持 Windows")

    if not any_running():
        return True

    deadline = time.monotonic() + timeout
    force_deadline = time.monotonic() + force_after
    forced = False

    while time.monotonic() < deadline:
        pids = running_pids()
        if not pids:
            return True

        use_force = forced or time.monotonic() >= force_deadline
        for pid in pids:
            cmd = ["taskkill", "/PID", str(pid), "/T"]
            if use_force:
                cmd.append("/F")
            _run(cmd)
        forced = forced or use_force

        time.sleep(0.5)

    return not any_running()


def wait_for_prefs_release(prefs: Path, *, timeout: float = 10.0) -> bool:
    """等待 prefs 文件可被独占写入(即句柄已释放)。

    做法是尝试以追加模式打开——被占用时 Windows 会抛 PermissionError。

    Returns:
        True 表示可以写入。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not prefs.exists():
            return True
        try:
            with open(prefs, "a", encoding="utf-8"):
                pass
            return True
        except PermissionError:
            time.sleep(0.3)
        except OSError:
            time.sleep(0.3)
    return False


def launch_app(exe: Path, *, args: list[str] | None = None) -> int:
    """启动客户端,返回 pid。

    用 DETACHED_PROCESS 让子进程不随本工具退出而结束。

    Raises:
        FileNotFoundError: exe 不存在。
    """
    if not exe.exists():
        raise FileNotFoundError(f"找不到主程序: {exe}")

    cmd = [str(exe), *(args or [])]
    # 工作目录设为安装目录,应用会按相对路径找 data/ 和 static/
    creationflags = 0
    if is_windows():
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(exe.parent),
            creationflags=creationflags,
            close_fds=True,
        )
        return proc.pid
    except OSError as e:
        # WinError 740:exe 清单要求管理员提升。CreateProcess 拒绝启动,
        # 改走 ShellExecute(资源管理器同款),由系统弹 UAC;拿不到 pid,返回 -1。
        if getattr(e, "winerror", None) != 740 or not is_windows():
            raise
        import ctypes  # noqa: PLC0415

        ret = ctypes.windll.shell32.ShellExecuteW(
            None, None, str(exe), None, str(exe.parent), 1  # SW_SHOWNORMAL
        )
        if ret <= 32:
            raise OSError(f"ShellExecute 启动失败,代码 {ret}") from e
        return -1
