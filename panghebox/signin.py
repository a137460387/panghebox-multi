"""签到(每日签到领时长卡)业务逻辑。

关于字段名的说明
----------------
从 Dart AOT 快照能确认端点存在,但 Dart 会压缩字段名,静态读不出响应 schema。
本模块因此采用**容错解析**:用一组候选键名去试,解析不出来时给出明确提示,
并支持 ``--dump-raw`` 打印原始响应,方便按真实 schema 校正。

已确认的线索(二进制常量池):
* ``getcheckinlist`` 无参数,返回里含 ``checkin_list`` / ``prize_list``
* ``checkinprize`` 带 ``area_tag_id``,返回含 ``prize_id``
* UI 侧类名 ``_WeekdaySignCard`` / ``_SundaySignCard`` —— 说明卡片分
  平日与周日两套(对应资源图 day / sunday-sign-*.png)
* ``duration`` 是时长(分钟)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .api import PangHeClient, ApiError

# 候选键名:服务端字段名未知,按可能性从高到低排列
_KEYS_LIST = ("checkin_list", "check_in_list", "list", "checkinlist", "days")
_KEYS_TODAY = ("today", "today_index", "current_day", "cur_day", "day")
_KEYS_SIGNED = ("is_sign", "is_signed", "signed", "checked", "is_checkin", "status")
_KEYS_DAY_NUM = ("day", "day_num", "day_index", "checkin_day", "index", "num")
_KEYS_REWARD = ("duration", "reward_duration", "reward", "minutes", "value")
_KEYS_AREA = ("area_tag_id", "tag_id", "area_id")


@dataclass
class CheckinDay:
    """签到列表里的一天。"""

    index: int = 0
    label: str = ""
    signed: bool = False
    duration_minutes: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        mark = "已签" if self.signed else "未签"
        dur = f"+{self.duration_minutes}分钟" if self.duration_minutes else ""
        name = self.label or f"第{self.index}天"
        return f"{name} {dur} [{mark}]".strip()


@dataclass
class CheckinState:
    """一次 getcheckinlist 的解析结果。"""

    days: list[CheckinDay] = field(default_factory=list)
    today_index: int | None = None
    area_tag_id: int | str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    parsed: bool = True

    @property
    def can_checkin(self) -> bool:
        """是否还有可签的(今天未签)。"""
        for d in self.days:
            if self.today_index is not None and d.index == self.today_index:
                return not d.signed
        return any(not d.signed for d in self.days)

    def today(self) -> CheckinDay | None:
        for d in self.days:
            if self.today_index is not None and d.index == self.today_index:
                return d
        return next((d for d in self.days if not d.signed), None)

    def summary(self) -> str:
        if not self.parsed:
            return "无法解析签到列表(字段名未知,请用 --dump-raw 查看原始响应)"
        if not self.days:
            return "签到列表为空"
        signed = sum(1 for d in self.days if d.signed)
        return f"已签到 {signed}/{len(self.days)} 天"


@dataclass
class CheckinResult:
    """一次签到尝试的结果。"""

    ok: bool
    message: str
    reward_minutes: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _pick(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """按候选键名取值,取不到返回 None。"""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _as_int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _as_bool(v: Any) -> bool:
    """把各种表示「已签」的值归一成 bool。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        # 约定:1 / 2 常见于「已签」;0 为未签
        return int(v) != 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "y", "signed", "done", "已签到", "已签"):
            return True
        if s in ("0", "false", "no", "n", "unsigned", "未签到", "未签", ""):
            return False
        return bool(s)
    return bool(v)


def parse_checkin_list(body: dict[str, Any]) -> CheckinState:
    """解析 getcheckinlist 的响应体。

    字段名未知,用候选键名逐层试探。解析不出天数列表时 ``parsed=False``,
    调用方应提示用户 dump 原始响应。
    """
    state = CheckinState(raw=body)

    area = _pick(body, _KEYS_AREA)
    if area is not None:
        state.area_tag_id = area

    raw_list = _pick(body, _KEYS_LIST)
    if not isinstance(raw_list, list):
        # 有些接口把列表塞在 body["body"] 里
        inner = body.get("body")
        if isinstance(inner, dict):
            raw_list = _pick(inner, _KEYS_LIST)
            if state.area_tag_id is None:
                state.area_tag_id = _pick(inner, _KEYS_AREA)

    if not isinstance(raw_list, list):
        state.parsed = False
        return state

    today_raw = _pick(body, _KEYS_TODAY)
    if today_raw is None:
        inner = body.get("body")
        if isinstance(inner, dict):
            today_raw = _pick(inner, _KEYS_TODAY)
    ti = _as_int(today_raw)
    if ti is not None:
        state.today_index = ti

    for i, item in enumerate(raw_list, start=1):
        if not isinstance(item, dict):
            continue
        day = CheckinDay(
            index=_as_int(_pick(item, _KEYS_DAY_NUM)) or i,
            label=str(_pick(item, ("label", "name", "title", "desc")) or ""),
            signed=_as_bool(_pick(item, _KEYS_SIGNED)),
            duration_minutes=_as_int(_pick(item, _KEYS_REWARD)),
            raw=item,
        )
        state.days.append(day)

    # 修正 index:若服务端没给,就用位置;并把 today_index 落到实际存在的值上
    if state.days and state.today_index is None:
        first_unsigned = next((d for d in state.days if not d.signed), None)
        if first_unsigned is not None:
            state.today_index = first_unsigned.index

    # 若 today_index 跟任何一天都对不上,尝试按 1-based 位置匹配
    if state.today_index is not None and not any(
        d.index == state.today_index for d in state.days
    ):
        if 1 <= state.today_index <= len(state.days):
            state.today_index = state.days[state.today_index - 1].index
        else:
            state.today_index = None

    return state


def check_in(
    client: PangHeClient,
    *,
    area_tag_id: int | str | None = None,
    state: CheckinState | None = None,
    raw_log: list | None = None,
) -> CheckinResult:
    """执行一次签到领奖。

    Args:
        client: 已带好凭据的客户端。
        area_tag_id: 分区标签;未给则尝试从 state 里取。
        state: 已解析的签到状态,用于补 area_tag_id / 判断是否已签。
        raw_log: 传入列表则收集原始响应。

    Returns:
        CheckinResult;不抛异常(网络/业务错误都收敛为 ok=False)。
    """
    if state is not None and area_tag_id is None:
        area_tag_id = state.area_tag_id

    if state is not None and not state.can_checkin:
        return CheckinResult(ok=False, message="今天已经签到过了")

    try:
        body = client.checkin_prize(area_tag_id=area_tag_id, raw_log=raw_log)
    except ApiError as e:
        msg = str(e)
        if e.code is not None:
            msg = f"签到失败(code={e.code}): {e}"
        return CheckinResult(ok=False, message=msg, raw=e.raw or {})

    reward = _as_int(
        _pick(body, ("duration", "reward_duration", "reward", "minutes", "sale_value"))
    )
    inner = body.get("body") if isinstance(body.get("body"), dict) else {}
    if reward is None and inner:
        reward = _as_int(_pick(inner, ("duration", "reward_duration", "reward")))

    tail = f",获得 {reward} 分钟时长卡" if reward else ""
    return CheckinResult(ok=True, message=f"签到成功{tail}", reward_minutes=reward, raw=body)


def get_state(client: PangHeClient, *, raw_log: list | None = None) -> CheckinState:
    """取并解析签到状态。"""
    body = client.get_checkin_list(raw_log=raw_log)
    return parse_checkin_list(body)


def check_in_account(
    client: PangHeClient,
    *,
    force: bool = False,
    raw_log: list | None = None,
) -> CheckinResult:
    """「查状态 + 按需签到」的完整流程。"""
    state = get_state(client, raw_log=raw_log)
    if not state.parsed:
        return CheckinResult(
            ok=False,
            message="无法解析签到列表(字段名未知),请用 --dump-raw 查看原始响应",
            raw=state.raw,
        )
    if not force and not state.can_checkin:
        return CheckinResult(ok=False, message=f"今天已经签到过了({state.summary()})")
    return check_in(client, state=state, raw_log=raw_log)
