"""账号档案的读写,以及 prefs 的白名单按键合并与原子写入。

设计要点(每一条都对应一个真实会踩的坑):

1. **绝不整文件覆盖 prefs。** prefs 共 50+ 个键,其中大部分和账号无关
   (guide_device_* 引导进度 30 个、qualitys 画质、setup_channel 渠道、
   ocpc 广告归因)。整文件覆盖会把当前设备的这些状态一起带过去。

2. **machine_id / accessKey 是设备级凭证,跨账号共享,绝不覆盖。**
   实测同一台机器上多个账号用的是同一个 accessKey。

3. **选区键随账号一起存。** lastFlowZone / lastFlowZoneName 是「上次选的区」,
   不跟着账号走的话,切完号还停在上一个账号的区。

4. **原子写入。** prefs 一旦写坏,Flutter 启动时解析失败可能直接丢登录态。
   先写临时文件再 os.replace,并保留一份 .bak。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 键分类
# ---------------------------------------------------------------------------

# 跟随账号走的键:切换时用目标账号的值覆盖当前 prefs
ACCOUNT_KEYS: tuple[str, ...] = (
    "flutter.uid",
    "flutter.token",
    "flutter.userphone",
    "flutter.isAutoLogin",
    "flutter.last_login_access_key",
    "flutter.personalDiskLoginSessionId",
    "flutter.new_user_ctivity",
    # 选区随账号走,否则切完还停在上一个号的区
    "flutter.lastFlowZone",
    "flutter.lastFlowZoneName",
)

# 设备级键:跨账号共享,**永不覆盖**。缺失时(冷启动)才允许从档案补齐。
DEVICE_KEYS: tuple[str, ...] = (
    "flutter.machine_id",
    "flutter.setup_channel",
    "flutter.ocpc",
)

# 纯本机状态的键:既不属于账号也不属于设备,切换时原样保留
# (speed_test_cooldown_by_uid 是个按 uid 累积的字典,覆盖会丢测速冷却记录)
LOCAL_ONLY_KEYS: tuple[str, ...] = (
    "flutter.speed_test_cooldown_by_uid",
    "flutter.quality",
    "flutter.qualitys",
    "flutter.leListenPort",
    "flutter.lEServerhandle",
    "flutter.charge_url",
    "flutter.recent_launch_key",
    "flutter.net_type",
    "flutter.display_scene_type",
    "flutter.scene_type",
    "flutter.recommend_default_zone_id",
    "flutter.autoSettlementType",
)

# guide_device_* 前缀:按 uid 分键的新手引导进度。
# 切换时**只追加目标账号自己的**,不删当前设备已有的(否则来回切会反复弹引导)。
GUIDE_PREFIX = "flutter.guide_device_"


def account_snapshot(prefs: dict[str, Any]) -> dict[str, Any]:
    """从完整 prefs 里抽出属于「账号」的那部分。

    Args:
        prefs: 完整的 prefs 字典。

    Returns:
        只含 ACCOUNT_KEYS 里存在项的字典。
    """
    return {k: prefs[k] for k in ACCOUNT_KEYS if k in prefs}


def device_snapshot(prefs: dict[str, Any]) -> dict[str, Any]:
    """从完整 prefs 里抽出「设备级」凭证,用于档案里留一份兜底。"""
    return {k: prefs[k] for k in DEVICE_KEYS if k in prefs}


def merge_account_into_prefs(
    current: dict[str, Any],
    account: dict[str, Any],
    *,
    device: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把账号数据合并进当前 prefs,返回新字典(不修改入参)。

    规则:
    - ACCOUNT_KEYS:用账号的值覆盖
    - DEVICE_KEYS:当前 prefs 里有就保留;没有才用档案里的兜底
    - 其他键(含 LOCAL_ONLY / guide_device_*):原样保留

    Args:
        current: 当前 prefs 内容。
        account: 目标账号的账号级数据。
        device: 目标账号档案里存的设备级数据,仅用于补齐缺失。

    Returns:
        合并后的新字典。
    """
    merged = dict(current)
    for k, v in account.items():
        merged[k] = v

    if device:
        for k, v in device.items():
            # 设备级键:优先信当前机器的,只有当前没有才用档案兜底
            if k not in merged or merged[k] in (None, ""):
                merged[k] = v
    return merged


# ---------------------------------------------------------------------------
# 账号档案
# ---------------------------------------------------------------------------


@dataclass
class Account:
    """一个账号的本地档案。

    Attributes:
        uid: 用户 ID。
        phone: 手机号(明文,凭据的一部分)。
        token: 登录 JWT。
        account: 账号级 prefs 键快照。
        device: 设备级键快照,兜底用。
        created_at: 建立时间(unix 秒)。
        note: 用户备注。
        duration_minutes: 最近一次查询到的剩余时长(分钟)。
        coin: 最近一次查询到的盒币。
        last_checked: 最近一次查询账号信息的时间。
    """

    uid: str
    phone: str = ""
    token: str = ""
    account: dict[str, Any] = field(default_factory=dict)
    device: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    note: str = ""
    duration_minutes: int | None = None
    coin: float | None = None
    last_checked: float | None = None

    @property
    def label(self) -> str:
        """人类可读的标签,手机号优先。"""
        return self.phone or f"uid:{self.uid}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "phone": self.phone,
            "token": self.token,
            "account": self.account,
            "device": self.device,
            "created_at": self.created_at,
            "note": self.note,
            "duration_minutes": self.duration_minutes,
            "coin": self.coin,
            "last_checked": self.last_checked,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Account":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @classmethod
    def from_prefs(cls, prefs: dict[str, Any], *, note: str = "") -> "Account":
        """从一份完整 prefs 构造账号档案(即「保存当前登录账号」)。

        Raises:
            ValueError: prefs 里没有 uid,说明当前没登录。
        """
        uid = prefs.get("flutter.uid")
        if uid in (None, "", 0, "0"):
            raise ValueError("当前 prefs 里没有 uid,似乎未登录")
        return cls(
            uid=str(uid),
            phone=str(prefs.get("flutter.userphone", "") or ""),
            token=str(prefs.get("flutter.token", "") or ""),
            account=account_snapshot(prefs),
            device=device_snapshot(prefs),
            note=note,
        )

    def duration_text(self) -> str:
        """剩余时长的展示文本。"""
        if self.duration_minutes is None:
            return "未知"
        return f"{self.duration_minutes} 分钟"

    def coin_text(self) -> str:
        """盒币的展示文本。"""
        if self.coin is None:
            return "未知"
        return f"{self.coin:.2f}"


def _safe_filename(s: str) -> str:
    """把 uid 变成安全的文件名。"""
    keep = "".join(c for c in s if c.isalnum() or c in "-_")
    return keep or "unknown"


class AccountStore:
    """账号档案仓库,一个账号对应 accounts/ 下的一个 json 文件。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, uid: str) -> Path:
        return self.root / f"{_safe_filename(str(uid))}.json"

    def save(self, acc: Account) -> Path:
        """写入账号档案(原子)。"""
        p = self.path_for(acc.uid)
        _atomic_write_json(p, acc.to_dict())
        return p

    def load(self, uid: str) -> Account:
        """按 uid 读取档案。

        Raises:
            FileNotFoundError: 档案不存在。
        """
        p = self.path_for(uid)
        if not p.exists():
            raise FileNotFoundError(f"找不到账号档案: {uid}")
        return Account.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def delete(self, uid: str) -> bool:
        """删除档案,返回是否真的删了。"""
        p = self.path_for(uid)
        if p.exists():
            p.unlink()
            return True
        return False

    def list(self) -> list[Account]:
        """列出全部档案,按手机号排序。"""
        out: list[Account] = []
        for p in sorted(self.root.glob("*.json")):
            try:
                out.append(Account.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, OSError, TypeError):
                # 单个档案坏了不该让整个列表挂掉
                continue
        out.sort(key=lambda a: a.label)
        return out

    def find_by_phone(self, phone: str) -> Account | None:
        """按手机号找档案。"""
        for a in self.list():
            if a.phone == phone:
                return a
        return None


# ---------------------------------------------------------------------------
# prefs 文件读写
# ---------------------------------------------------------------------------


def read_prefs(path: Path | str) -> dict[str, Any]:
    """读取 prefs 文件。

    Raises:
        FileNotFoundError: 文件不存在(应用从未运行过)。
        ValueError: 内容不是合法 JSON。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"找不到账号文件: {p}\n请先启动一次客户端并登录,让它生成该文件。"
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"账号文件不是合法 JSON(可能已损坏): {p}\n{e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"账号文件顶层不是对象: {p}")
    return data


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """原子写入 JSON:同目录临时文件 → fsync → os.replace。

    同目录是必须的,os.replace 跨盘符不保证原子。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_prefs(path: Path | str, data: dict[str, Any], *, backup: bool = True) -> Path | None:
    """原子写入 prefs,可选先备份原文件。

    Args:
        path: prefs 路径。
        data: 要写入的完整内容。
        backup: 是否先生成 .bak。

    Returns:
        备份文件路径;未备份时为 None。
    """
    p = Path(path)
    bak: Path | None = None
    if backup and p.exists():
        bak = p.with_suffix(p.suffix + ".bak")
        shutil.copy2(p, bak)
    _atomic_write_json(p, data)
    return bak


def switch_account_on_disk(
    prefs_path: Path | str,
    target: Account,
    *,
    backup: bool = True,
) -> dict[str, Any]:
    """把磁盘上的 prefs 切换到目标账号(白名单合并 + 原子写入)。

    **调用方必须确保应用已退出**,否则应用退出时会用内存状态回写覆盖。

    Args:
        prefs_path: prefs 文件路径。
        target: 目标账号档案。
        backup: 是否备份原文件。

    Returns:
        合并后写盘的完整 prefs 字典。
    """
    current = read_prefs(prefs_path)
    merged = merge_account_into_prefs(current, target.account, device=target.device)
    write_prefs(prefs_path, merged, backup=backup)
    return merged
