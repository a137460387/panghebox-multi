"""账号档案的读写,以及 prefs 的白名单按键合并与原子写入。

设计要点(每一条都对应一个真实会踩的坑):

1. **绝不整文件覆盖 prefs。** prefs 共 50+ 个键,其中大部分和账号无关
   (guide_device_* 引导进度 30 个、qualitys 画质、setup_channel 渠道、
   ocpc 广告归因)。整文件覆盖会把当前设备的这些状态一起带过去。

2. **切换的本质是换 token。** 实测确认(2026-09-24):
   - ``token`` 是唯一身份凭据。JWT payload 里嵌了 ``acceessKey``,
     服务端会比对它与请求头 ``Accesskey`` 是否一致;两者不符直接报
     3584901,所以 accessKey **不能伪造、也不能按账号分别存**。
   - ``uid`` 只是请求参数,**不参与鉴权**。传错 uid 配上有效 token,
     服务端仍按 token 识别身份。
   - ``machine_id`` 只作请求头上报,同样不参与鉴权;换任意 UUID 都能
     正常调用。它与客户端本地引导进度绑定,因此**随账号走**(见 ACCOUNT_KEYS)
     ——各号表现为独立设备,引导状态互不干扰。

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
#
# 注意 machine_id 也在其中。实测结论(2026-09-24):
#   * machine_id 只作为请求头 Machineid 上报,**不参与鉴权**——换任意
#     UUID(包括随机值)都能正常调用接口。
#   * 真正决定身份的是 token(JWT 里嵌了 accessKey,服务端比对两者)。
#   * 客户端把 machine_id 与本地引导进度绑定(日志里可见
#     "reset cloud_game to 0 (localDevice: , currentDevice: <新UUID>)")。
# 因此让每个账号持有自己的 machine_id:各号在客户端看来是不同设备,
# 引导状态互不干扰,也不会因为共用同一个设备标识而被关联。
ACCOUNT_KEYS: tuple[str, ...] = (
    "flutter.uid",
    "flutter.token",
    "flutter.userphone",
    "flutter.isAutoLogin",
    "flutter.last_login_access_key",
    "flutter.personalDiskLoginSessionId",
    "flutter.new_user_ctivity",
    "flutter.machine_id",
    # 选区随账号走,否则切完还停在上一个号的区
    "flutter.lastFlowZone",
    "flutter.lastFlowZoneName",
)

# 设备级键:与登录账号无关、同机共享,切换时保留当前值(不随账号走)。
# setup_channel / ocpc 是安装渠道与归因标识,由安装包决定,不该被账号切换改动。
DEVICE_KEYS: tuple[str, ...] = (
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
    """把手机号/uid 变成安全的文件名。"""
    keep = "".join(c for c in s if c.isalnum() or c in "-_")
    return keep or "unknown"


class AccountStore:
    """账号档案仓库,一个账号对应 accounts/ 下的一个 json 文件。

    文件名优先用**手机号**(如 ``13800000001.json``),手机号缺失时回退到
    uid。手机号是用户日常识别账号的方式,看文件列表能直接对上人;
    uid 是一串数字,肉眼不好认。

    为保证兼容,读取时会依次尝试:手机号文件名 → uid 文件名。
    早期版本用 uid 命名,这些档案仍能被找到,并在下次保存时自动改名为手机号。
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def filename_for(self, acc: Account) -> str:
        """返回该账号应使用的文件名(不含路径)。"""
        key = acc.phone or acc.uid
        return f"{_safe_filename(str(key))}.json"

    def path_for(self, acc: Account) -> Path:
        """返回该账号档案的规范路径。"""
        return self.root / self.filename_for(acc)

    def path_for_key(self, key: str) -> Path:
        """按「手机号或 uid」构造候选路径(仅用于查旧档案)。"""
        return self.root / f"{_safe_filename(str(key))}.json"

    def find_file(self, key: str) -> Path | None:
        """按手机号或 uid 找到已有档案文件,找不到返回 None。

        先试直接按 key 命名的文件(兼容旧版 uid 命名),再遍历内容匹配
        phone / uid 字段。
        """
        direct = self.path_for_key(key)
        if direct.exists():
            return direct

        for p in sorted(self.root.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if str(data.get("uid")) == str(key) or str(data.get("phone")) == str(key):
                return p
        return None

    def save(self, acc: Account) -> Path:
        """写入账号档案(原子)。

        若该账号此前以别的文件名存在(例如旧版的 ``<uid>.json``),
        会顺带把旧文件删掉,避免同一账号出现两份档案。
        """
        target = self.path_for(acc)

        # 清理指向同一账号的其他命名文件(如旧版 uid 命名)
        for p in sorted(self.root.glob("*.json")):
            if p == target:
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            same = (
                str(data.get("uid")) == str(acc.uid)
                or (acc.phone and str(data.get("phone")) == str(acc.phone))
            )
            if same:
                p.unlink(missing_ok=True)

        _atomic_write_json(target, acc.to_dict())
        return target

    def load(self, key: str) -> Account:
        """按手机号或 uid 读取档案。

        Raises:
            FileNotFoundError: 档案不存在。
        """
        p = self.find_file(key)
        if p is None:
            raise FileNotFoundError(f"找不到账号档案: {key}")
        return Account.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def delete(self, key: str) -> bool:
        """删除档案,返回是否真的删了。"""
        p = self.find_file(key)
        if p is not None and p.exists():
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


def clear_login_state(
    prefs_path: Path | str,
    *,
    backup: bool = True,
) -> list[str]:
    """删除账号相关键,回到「未登录」状态,等价新装后的首次启动。

    **必须删键,不能置空。** 实测教训(2026-09-24):把 uid/lastFlowZone
    这类整数字段写成 null,客户端启动时的 checkSharedPreferences 会抛
    ``type 'Null' is not a subtype of type 'Object'``(单次会话 40+ 处),
    首页初始化被打断、一直转圈,需要重启一次(客户端规范化坏值后)才恢复。
    而键缺失是 Flutter prefs 的「新装」路径,客户端有完善处理。

    删除的键 = ACCOUNT_KEYS(含 machine_id,由客户端重新生成独立标识)
    + isAutoLogin。保留 setup_channel / ocpc(渠道归因,与账号无关)。

    Args:
        prefs_path: prefs 文件路径。
        backup: 是否先备份。

    Returns:
        被删除的键名列表。
    """
    data = read_prefs(prefs_path)
    removed = [k for k in (*ACCOUNT_KEYS, "flutter.isAutoLogin") if k in data]
    for k in removed:
        data.pop(k, None)
    write_prefs(prefs_path, data, backup=backup)
    return removed


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
