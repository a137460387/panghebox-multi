"""signin 模块测试。

响应形状取自实测的真实 schema(见 panghebox/signin.py 模块文档),
值全部是伪造的。
"""

from __future__ import annotations

import pytest

from panghebox.api import ApiError
from panghebox.signin import (
    CODE_ALREADY_DONE,
    CheckinDay,
    CheckinState,
    _as_int,
    check_in,
    check_in_account,
    get_state,
    parse_checkin_list,
)


def real_shape_body(*, signed_days: int = 0, today: str = "2026-09-23") -> dict:
    """构造一份与实测形状一致的 getcheckinlist 响应。

    Args:
        signed_days: 已签几天(前 N 天的 date 非空)。
        today: 用于生成已签日期。
    """
    values = [10, 10, 10, 12, 12, 15, 20]
    days = []
    for i, v in enumerate(values, start=1):
        days.append({
            "prize_id": 99 + i,
            "prize_sec_id": i,
            "prize_name": f"第{i}天签到礼包",
            "sale_info": [{
                "sale_id": f"sale_{i}",
                "sale_name": f"签到奖励-{v}分钟卡",
                "sale_type": 101,
                "sale_value": v,
                "sale_period": 0 if i == 1 else 24,
            }],
            "date": f"{today} 17:19:21" if i <= signed_days else "",
        })
    return {
        "checkin_list": days,
        "checkin_count": signed_days,
        "actvity_day_count": signed_days,
    }


# ---------------------------------------------------------------------------
# 基础转换
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [(30, 30), ("30", 30), ("30.9", 30), (None, None), ("abc", None)],
)
def test_as_int_variants(raw, expected):
    assert _as_int(raw) == expected


# ---------------------------------------------------------------------------
# 解析真实 schema
# ---------------------------------------------------------------------------

def test_parse_real_shape():
    st = parse_checkin_list(real_shape_body(signed_days=1))
    assert st.parsed
    assert st.checkin_count == 1
    assert st.activity_day_count == 1
    assert len(st.days) == 7

    first = st.days[0]
    assert first.prize_id == 100
    assert first.day_index == 1
    assert first.signed is True
    assert first.signed_at == "2026-09-23 17:19:21"
    assert first.duration_minutes == 10
    assert first.sale_name == "签到奖励-10分钟卡"
    assert first.name == "第1天签到礼包"

    second = st.days[1]
    assert second.signed is False
    assert second.duration_minutes == 10


def test_all_reward_values_parsed():
    st = parse_checkin_list(real_shape_body())
    assert [d.duration_minutes for d in st.days] == [10, 10, 10, 12, 12, 15, 20]


def test_prize_ids_are_distinct():
    st = parse_checkin_list(real_shape_body())
    assert [d.prize_id for d in st.days] == [100, 101, 102, 103, 104, 105, 106]


def test_parse_empty_or_malformed():
    assert parse_checkin_list({}).parsed is False
    assert parse_checkin_list({"checkin_list": "not-a-list"}).parsed is False
    assert parse_checkin_list({"checkin_list": []}).parsed is False


def test_parse_tolerates_missing_sale_info():
    body = {"checkin_list": [{"prize_id": 1, "prize_sec_id": 1, "date": ""}]}
    st = parse_checkin_list(body)
    assert len(st.days) == 1
    assert st.days[0].duration_minutes is None
    assert st.days[0].day_index == 1


def test_day_index_falls_back_to_position():
    body = {"checkin_list": [{"prize_id": 1}, {"prize_id": 2}]}
    st = parse_checkin_list(body)
    assert [d.day_index for d in st.days] == [1, 2]


# ---------------------------------------------------------------------------
# 今日是否已签(核心逻辑)
# ---------------------------------------------------------------------------

def test_signed_today_true_when_last_date_is_today():
    st = parse_checkin_list(real_shape_body(signed_days=1, today="2026-09-23"))
    assert st.signed_today("2026-09-23") is True
    assert st.signed_today("2026-09-24") is False


def test_signed_today_false_when_nothing_signed():
    st = parse_checkin_list(real_shape_body(signed_days=0))
    assert st.signed_today("2026-09-23") is False


def test_signed_today_uses_last_signed_entry():
    """签了 3 天但最后一次是昨天 → 今天还没签。"""
    body = real_shape_body(signed_days=3, today="2026-09-22")
    st = parse_checkin_list(body)
    assert st.signed_today("2026-09-23") is False


def test_can_checkin_false_when_signed_today():
    st = parse_checkin_list(real_shape_body(signed_days=1))
    assert st.signed_today("2026-09-23") is True
    assert st.can_checkin is False


def test_can_checkin_true_when_not_signed_today():
    st = parse_checkin_list(real_shape_body(signed_days=1, today="2026-09-22"))
    assert st.can_checkin is True


def test_can_checkin_false_when_all_seven_signed():
    st = parse_checkin_list(real_shape_body(signed_days=7))
    assert st.next_day() is None
    assert st.can_checkin is False


def test_next_day_is_first_unsigned():
    st = parse_checkin_list(real_shape_body(signed_days=2))
    nxt = st.next_day()
    assert nxt is not None
    assert nxt.day_index == 3


# ---------------------------------------------------------------------------
# summary / table
# ---------------------------------------------------------------------------

def test_summary_mentions_today_state():
    signed = parse_checkin_list(real_shape_body(signed_days=1))
    assert "今天已签" in signed.summary()
    assert "1/7" in signed.summary()

    not_signed = parse_checkin_list(real_shape_body(signed_days=1, today="2026-09-22"))
    assert "今天未签" in not_signed.summary()


def test_table_lists_all_days():
    st = parse_checkin_list(real_shape_body(signed_days=1))
    table = st.table()
    assert "第1天签到礼包" in table
    assert "第7天签到礼包" in table
    assert "已签" in table


def test_day_describe_marks_signed_and_reward():
    d = CheckinDay(prize_id=1, day_index=2, name="第2天签到礼包",
                   signed=False, duration_minutes=10)
    text = d.describe()
    assert "第2天签到礼包" in text
    assert "+10分钟" in text
    assert "未签" in text


# ---------------------------------------------------------------------------
# check_in:参数与错误处理
# ---------------------------------------------------------------------------

class _FakeClient:
    """记录调用参数的假客户端。"""

    def __init__(self, response=None, error=None):  # noqa: ANN001
        self.response = response or {}
        self.error = error
        self.calls: list = []

    def checkin_prize(self, *, prize_id, extra=None, raw_log=None):  # noqa: ANN001, ANN202
        self.calls.append(prize_id)
        if self.error:
            raise self.error
        return self.response


def test_check_in_uses_prize_id_of_next_day():
    """必须用目标那天的 prize_id,而不是固定值。"""
    st = parse_checkin_list(real_shape_body(signed_days=2, today="2026-09-22"))
    client = _FakeClient(response={"sale_value": 10})
    result = check_in(client, state=st)  # type: ignore[arg-type]

    assert result.ok is True
    assert client.calls == [102]  # 第三天
    assert result.reward_minutes == 10


def test_check_in_skips_when_signed_today():
    st = parse_checkin_list(real_shape_body(signed_days=1))
    client = _FakeClient()
    result = check_in(client, state=st)  # type: ignore[arg-type]

    assert result.ok is False
    assert result.already_done is True
    assert "已经签到过" in result.message
    assert client.calls == []  # 不该发请求


def test_check_in_force_bypasses_today_guard():
    st = parse_checkin_list(real_shape_body(signed_days=1))
    client = _FakeClient(response={"sale_value": 10})
    result = check_in(client, state=st, force=True)  # type: ignore[arg-type]
    assert result.ok is True
    assert len(client.calls) == 1


def test_check_in_handles_already_done_code():
    """服务端返回 3584316 时要识别为「已领取」而不是普通失败。"""
    st = parse_checkin_list(real_shape_body(signed_days=2, today="2026-09-22"))
    client = _FakeClient(
        error=ApiError("该日时间无法领取奖励", code=CODE_ALREADY_DONE)
    )
    result = check_in(client, state=st)  # type: ignore[arg-type]
    assert result.ok is False
    assert result.already_done is True
    assert "无法领取" in result.message


def test_check_in_reports_other_errors():
    st = parse_checkin_list(real_shape_body(signed_days=2, today="2026-09-22"))
    client = _FakeClient(error=ApiError("boom", code=12345))
    result = check_in(client, state=st)  # type: ignore[arg-type]
    assert result.ok is False
    assert result.already_done is False
    assert "12345" in result.message


def test_check_in_without_state_reports_nothing_to_claim():
    client = _FakeClient()
    result = check_in(client)  # type: ignore[arg-type]
    assert result.ok is False
    assert "没有可领取" in result.message


def test_check_in_uses_explicit_day():
    day = CheckinDay(prize_id=105, day_index=6, duration_minutes=15)
    client = _FakeClient(response={})
    result = check_in(client, day=day)  # type: ignore[arg-type]
    assert result.ok is True
    assert client.calls == [105]
    assert result.reward_minutes == 15  # 回退到列表里的值


# ---------------------------------------------------------------------------
# check_in_account
# ---------------------------------------------------------------------------

class _StatefulClient:
    """getcheckinlist 返回固定状态,checkinprize 记录调用。"""

    def __init__(self, body):  # noqa: ANN001
        self.body = body
        self.calls: list = []

    def get_checkin_list(self, *, raw_log=None):  # noqa: ANN001, ANN202
        return self.body

    def checkin_prize(self, *, prize_id, extra=None, raw_log=None):  # noqa: ANN001, ANN202
        self.calls.append(prize_id)
        return {"sale_value": 10}


def test_check_in_account_skips_when_signed_today():
    client = _StatefulClient(real_shape_body(signed_days=1))
    result = check_in_account(client)  # type: ignore[arg-type]
    assert result.ok is False
    assert result.already_done is True
    assert client.calls == []


def test_check_in_account_signs_when_not_signed_today():
    client = _StatefulClient(real_shape_body(signed_days=1, today="2026-09-22"))
    result = check_in_account(client)  # type: ignore[arg-type]
    assert result.ok is True
    assert client.calls == [101]  # 第二天


def test_check_in_account_handles_empty_list():
    client = _StatefulClient({"checkin_list": []})
    result = check_in_account(client)  # type: ignore[arg-type]
    assert result.ok is False
    assert "格式异常" in result.message or "为空" in result.message
