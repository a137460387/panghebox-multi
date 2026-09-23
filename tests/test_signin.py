"""signin 模块测试:响应解析的容错性。

字段名在 Dart 快照里被混淆,所以解析层必须对多种可能的键名都能工作。
这些测试用几种「可能的」响应形状验证解析器不会崩,并在无法识别时
明确报告 parsed=False 而不是静默给出错误结果。
"""

from __future__ import annotations

import pytest

from panghebox.signin import (
    CheckinDay,
    CheckinState,
    _as_bool,
    _as_int,
    check_in,
    parse_checkin_list,
)


# ---------------------------------------------------------------------------
# 基础转换
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1, True), (0, False), (2, True), (True, True), (False, False),
     ("1", True), ("0", False), ("true", True), ("false", False),
     ("已签到", True), ("未签到", False), (None, False)],
)
def test_as_bool_variants(raw, expected):
    assert _as_bool(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(30, 30), ("30", 30), ("30.9", 30), (None, None), ("abc", None)],
)
def test_as_int_variants(raw, expected):
    assert _as_int(raw) == expected


# ---------------------------------------------------------------------------
# 列表解析:多种可能的形状
# ---------------------------------------------------------------------------

def test_parse_canonical_shape():
    body = {
        "today": 1,
        "area_tag_id": 6310,
        "checkin_list": [
            {"day": 1, "is_sign": 1, "duration": 10},
            {"day": 2, "is_sign": 0, "duration": 10},
            {"day": 7, "is_sign": 0, "duration": 20},
        ],
    }
    st = parse_checkin_list(body)
    assert st.parsed
    assert st.today_index == 1
    assert st.area_tag_id == 6310
    assert len(st.days) == 3
    assert st.days[0].signed is True
    assert st.days[1].signed is False
    assert st.days[2].duration_minutes == 20


def test_parse_alternative_key_names():
    """服务端若用 signed / day_num / reward_duration 也能解析。"""
    body = {
        "current_day": 2,
        "list": [
            {"day_num": 1, "signed": True, "reward_duration": 10},
            {"day_num": 2, "signed": False, "reward_duration": 12},
        ],
    }
    st = parse_checkin_list(body)
    assert st.parsed
    assert st.today_index == 2
    assert st.days[0].duration_minutes == 10


def test_parse_nested_body():
    """有些接口把内容再包一层 body。"""
    body = {
        "body": {
            "area_tag_id": 43671,
            "checkin_list": [{"day": 1, "is_sign": 0}],
        }
    }
    st = parse_checkin_list(body)
    assert st.parsed
    assert st.area_tag_id == 43671
    assert len(st.days) == 1


def test_parse_unknown_shape_reports_unparsed():
    """字段名完全对不上时,必须显式标记未解析,而不是假装成功。"""
    st = parse_checkin_list({"unexpected": "shape"})
    assert st.parsed is False
    assert st.days == []
    assert "无法解析" in st.summary()


def test_parse_empty_list_is_parsed():
    st = parse_checkin_list({"checkin_list": []})
    assert st.parsed is True
    assert st.days == []
    assert st.summary() == "签到列表为空"


def test_parse_indexes_from_position_when_missing():
    body = {"checkin_list": [{"is_sign": 1}, {"is_sign": 0}]}
    st = parse_checkin_list(body)
    assert [d.index for d in st.days] == [1, 2]


def test_today_index_falls_back_to_first_unsigned():
    body = {"checkin_list": [{"day": 1, "is_sign": 1}, {"day": 2, "is_sign": 0}]}
    st = parse_checkin_list(body)
    assert st.today_index == 2


def test_today_index_out_of_range_is_dropped():
    body = {"today": 99, "checkin_list": [{"day": 1, "is_sign": 0}]}
    st = parse_checkin_list(body)
    assert st.today_index is None


# ---------------------------------------------------------------------------
# can_checkin / today / summary
# ---------------------------------------------------------------------------

def test_can_checkin_true_when_today_unsigned():
    st = CheckinState(
        days=[CheckinDay(index=1, signed=True), CheckinDay(index=2, signed=False)],
        today_index=2,
    )
    assert st.can_checkin is True
    assert st.today().index == 2


def test_can_checkin_false_when_all_signed():
    st = CheckinState(
        days=[CheckinDay(index=1, signed=True), CheckinDay(index=2, signed=True)],
        today_index=2,
    )
    assert st.can_checkin is False
    assert st.summary() == "已签到 2/2 天"


def test_summary_counts():
    st = CheckinState(
        days=[
            CheckinDay(index=1, signed=True),
            CheckinDay(index=2, signed=False),
            CheckinDay(index=3, signed=True),
        ]
    )
    assert st.summary() == "已签到 2/3 天"


def test_day_describe():
    d = CheckinDay(index=3, signed=False, duration_minutes=10)
    assert "第3天" in d.describe()
    assert "+10分钟" in d.describe()
    assert "未签" in d.describe()


# ---------------------------------------------------------------------------
# check_in:已签时不该再请求
# ---------------------------------------------------------------------------

class _ExplodingClient:
    """任何调用都失败的假客户端,用来验证「不该请求时不请求」。"""

    def checkin_prize(self, **kwargs):  # noqa: ANN003, ANN201
        raise AssertionError("不应该发起签到请求")


def test_check_in_skips_when_already_signed():
    st = CheckinState(days=[CheckinDay(index=1, signed=True)], today_index=1)
    result = check_in(_ExplodingClient(), state=st)  # type: ignore[arg-type]
    assert result.ok is False
    assert "已经签到过" in result.message


def test_check_in_success_reads_reward():
    class _Client:
        def checkin_prize(self, *, area_tag_id=None, extra=None, raw_log=None):  # noqa: ANN001, ANN202
            return {"duration": 20, "prize_id": 9}

    st = CheckinState(days=[CheckinDay(index=1, signed=False)], today_index=1)
    result = check_in(_Client(), state=st)  # type: ignore[arg-type]
    assert result.ok is True
    assert result.reward_minutes == 20
    assert "20 分钟" in result.message


def test_check_in_reports_api_error_not_raise():
    from panghebox.api import ApiError

    class _Client:
        def checkin_prize(self, *, area_tag_id=None, extra=None, raw_log=None):  # noqa: ANN001, ANN202
            raise ApiError("boom", code=40001)

    st = CheckinState(days=[CheckinDay(index=1, signed=False)], today_index=1)
    result = check_in(_Client(), state=st)  # type: ignore[arg-type]
    assert result.ok is False
    assert "40001" in result.message
