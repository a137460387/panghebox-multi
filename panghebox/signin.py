"""签到(每日签到领时长卡)业务逻辑。

接口 schema 已通过实测确认为真实字段名
------------------------------------------
``getcheckinlist`` 的响应(无参数)::

    {
      "checkin_list": [
        {
          "prize_id": 100,              # 第 1 天的奖励 ID
          "prize_sec_id": 1,            # 第几天(1..7)
          "prize_name": "第一天签到礼包",
          "sale_info": [
            {"sale_id": "2211_x7mzuych",
             "sale_name": "签到奖励-10分钟卡",
             "sale_type": 101,          # 101 = 时长卡
             "sale_value": 10,          # 奖励分钟数
             "sale_period": 0}          # 有效期(小时);0 = 当天有效
          ],
          "date": "2026-09-23 17:19:21" # 已领取的时间;空串 = 未领取
        },
        ...共 7 天
      ],
      "checkin_count": 1,               # 已连续签到天数
      "actvity_day_count": 1            # 活动已进行天数
    }

``checkinprize`` 的请求参数**只有 ``prize_id``**(实测确认):

* 传 ``prize_id`` → 正常业务响应(已领取时返回 ``3584316 该日时间无法领取奖励``)
* 不传 → HTTP 500

二进制常量池里 ``checkinprize`` 旁边的 ``area_tag_id`` 经实测**并非必需**,
传不传不影响结果。

判断「今天是否已签」的可靠方式:看 ``checkin_list`` 里
``prize_sec_id == checkin_count + 1`` 那一天,其 ``date`` 是否非空。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .api import ApiError, PangHeClient

# 业务错误码
CODE_ALREADY_DONE = 3584316  # 该日时间无法领取奖励(已领取 / 未到时间)


@dataclass
class CheckinDay:
    """签到列表里的一天。"""

    prize_id: int = 0
    day_index: int = 0          # prize_sec_id,1..7
    name: str = ""
    signed: bool = False
    signed_at: str = ""
    duration_minutes: int | None = None
    sale_name: str = ""
    sale_period: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        mark = "✓已签" if self.signed else "未签"
        dur = f"+{self.duration_minutes}分钟" if self.duration_minutes else ""
        label = self.name or f"第{self.day_index}天"
        return f"{label:<12} {dur:<10} [{mark}]"


@dataclass
class CheckinState:
    """一次 getcheckinlist 的解析结果。"""

    days: list[CheckinDay] = field(default_factory=list)
    checkin_count: int = 0      # 已连续签到天数
    activity_day_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def parsed(self) -> bool:
        return bool(self.days)

    def last_signed(self) -> CheckinDay | None:
        """返回最近一次已领取的那天。"""
        signed = [d for d in self.days if d.signed]
        return signed[-1] if signed else None

    def signed_today(self, today: str | None = None) -> bool:
        """今天是否已经签过。

        判定依据是最近一次领取记录的 ``date`` 是否就是今天。
        不能用 ``checkin_count`` 判断——比如已签 1 天时,``count`` 是 1,
        但无法区分「今天刚签的第 1 天」和「昨天签的、今天还没签」。

        Args:
            today: 用于比较的日期(``YYYY-MM-DD``);默认取本机当天。
        """
        last = self.last_signed()
        if last is None:
            return False
        if today is None:
            from datetime import date  # noqa: PLC0415

            today = date.today().isoformat()
        return last.signed_at.startswith(today) or last.signed_at[:10] == today

    def next_day(self) -> CheckinDay | None:
        """返回下一个该签的那天(date 为空的第一条)。"""
        for d in self.days:
            if not d.signed:
                return d
        return None

    @property
    def can_checkin(self) -> bool:
        """今天是否还能签。

        两个条件都要满足:① 7 天里还有没领的;② 今天还没领过。
        实测服务端每天只允许领一次,重复领会返回 3584316。
        """
        return self.next_day() is not None and not self.signed_today()

    def summary(self) -> str:
        signed = sum(1 for d in self.days if d.signed)
        tail = "(今天已签)" if self.signed_today() else "(今天未签)"
        return f"已签到 {signed}/{len(self.days)} 天{tail}"

    def table(self) -> str:
        return "\n".join("  " + d.describe() for d in self.days)


@dataclass
class CheckinResult:
    """一次签到尝试的结果。"""

    ok: bool
    message: str
    reward_minutes: int | None = None
    already_done: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


def _as_int(v: Any) -> int | None:
    """尽量把值转成 int;转不了返回 None。

    服务端目前发的是整数,但字符串形式的整数或浮点(如 "30.9")也一并容忍。
    """
    if isinstance(v, bool):
        return int(v)
    try:
        return int(v)
    except (TypeError, ValueError):
        pass
    try:
        # 兼容 "30.9" 这类小数形式;经 float 中转后截断
        return int(float(v))
    except (TypeError, ValueError):
        return None


def parse_checkin_list(body: dict[str, Any]) -> CheckinState:
    """解析 getcheckinlist 的响应体(真实字段名)。"""
    state = CheckinState(raw=body)
    state.checkin_count = _as_int(body.get("checkin_count")) or 0
    state.activity_day_count = _as_int(body.get("actvity_day_count")) or 0

    raw_list = body.get("checkin_list")
    if not isinstance(raw_list, list):
        return state

    for i, item in enumerate(raw_list, start=1):
        if not isinstance(item, dict):
            continue
        sale = (item.get("sale_info") or [{}])[0]
        if not isinstance(sale, dict):
            sale = {}
        signed_at = str(item.get("date") or "").strip()
        state.days.append(
            CheckinDay(
                prize_id=_as_int(item.get("prize_id")) or 0,
                day_index=_as_int(item.get("prize_sec_id")) or i,
                name=str(item.get("prize_name") or ""),
                signed=bool(signed_at),
                signed_at=signed_at,
                duration_minutes=_as_int(sale.get("sale_value")),
                sale_name=str(sale.get("sale_name") or ""),
                sale_period=_as_int(sale.get("sale_period")),
                raw=item,
            )
        )
    return state


def get_state(client: PangHeClient, *, raw_log: list | None = None) -> CheckinState:
    """取并解析签到状态。"""
    body = client.get_checkin_list(raw_log=raw_log)
    return parse_checkin_list(body)


def check_in(
    client: PangHeClient,
    *,
    day: CheckinDay | None = None,
    state: CheckinState | None = None,
    force: bool = False,
    raw_log: list | None = None,
) -> CheckinResult:
    """执行一次签到领奖。

    Args:
        client: 已带凭据的客户端。
        day: 要领取的那天;未给则用 state 里的下一天。
        state: 已解析的签到状态。
        force: 即使今天已签也再请求一次(一般不需要,服务端会拒绝)。
        raw_log: 收集原始响应用。

    Returns:
        CheckinResult;不抛异常(网络/业务错误都收敛为 ok=False)。
    """
    if day is None and state is not None:
        day = state.next_day()

    if day is None:
        return CheckinResult(ok=False, message="没有可领取的签到奖励(7 天可能已领完)")

    if state is not None and state.signed_today() and not force:
        return CheckinResult(
            ok=False,
            message=f"今天已经签到过了({state.summary()})",
            already_done=True,
        )

    try:
        body = client.checkin_prize(prize_id=day.prize_id, raw_log=raw_log)
    except ApiError as e:
        if e.code == CODE_ALREADY_DONE:
            return CheckinResult(
                ok=False,
                message=f"该日奖励无法领取(已领取或未到时间):{day.name or day.day_index}",
                already_done=True,
                raw=e.raw or {},
            )
        return CheckinResult(
            ok=False,
            message=f"签到失败(code={e.code}): {e}",
            raw=e.raw or {},
        )

    reward = _as_int(body.get("sale_value")) or day.duration_minutes
    if reward is None:
        rewards = body.get("sale_info")
        if isinstance(rewards, list) and rewards and isinstance(rewards[0], dict):
            reward = _as_int(rewards[0].get("sale_value"))

    tail = f",获得 {reward} 分钟时长卡" if reward else ""
    return CheckinResult(
        ok=True,
        message=f"签到成功({day.name or f'第{day.day_index}天'}){tail}",
        reward_minutes=reward,
        raw=body,
    )


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
            message="签到列表为空或格式异常",
            raw=state.raw,
        )

    if not force and not state.can_checkin:
        return CheckinResult(
            ok=False,
            message=f"今天已经签到过了({state.summary()})",
            already_done=True,
        )

    return check_in(client, state=state, force=force, raw_log=raw_log)
