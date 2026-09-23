"""api 模块测试:请求头构造、响应信封解包、凭据提取。

全部用假 HTTP 层,不发真实请求。
"""

from __future__ import annotations

import json

import pytest

from panghebox.api import (
    ApiCredentials,
    ApiError,
    PangHeClient,
    UserInfo,
    credentials_from_account,
    credentials_from_prefs,
)
from panghebox.store import Account


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200):  # noqa: ANN001
        self._payload = payload
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):  # noqa: ANN201
        if isinstance(self._payload, str):
            raise json.JSONDecodeError("bad", self._payload, 0)
        return self._payload


class _FakeSession:
    """记录最后一次请求,返回预设响应。"""

    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.last: dict = {}

    def request(self, method, url, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.last = {"method": method, "url": url, **kwargs}
        return self.response

    def close(self) -> None:
        pass


def _client(response: _FakeResponse, cred: ApiCredentials | None = None) -> tuple[PangHeClient, _FakeSession]:
    sess = _FakeSession(response)
    c = PangHeClient(
        cred or ApiCredentials(uid="100001", token="JWT", access_key="AK", machine_id="MID"),
        session=sess,  # type: ignore[arg-type]
    )
    return c, sess


# ---------------------------------------------------------------------------
# 请求头
# ---------------------------------------------------------------------------

def test_headers_match_client_contract():
    """这 6 个头是逆向确认的契约,少一个服务端都可能拒绝。"""
    c, _ = _client(_FakeResponse({"ret": {"code": 0}, "body": {}}))
    h = c.headers()
    for key in ("Authorization", "Version", "Machineid", "Accesskey", "Platform",
                "X-Client-Request-Id"):
        assert key in h, f"缺少请求头 {key}"
    assert h["Authorization"] == "JWT"
    assert h["Machineid"] == "MID"
    assert h["Accesskey"] == "AK"
    assert h["Platform"] == "window"


def test_request_id_is_unique_per_call():
    c, _ = _client(_FakeResponse({"ret": {"code": 0}, "body": {}}))
    assert c.headers()["X-Client-Request-Id"] != c.headers()["X-Client-Request-Id"]


def test_no_signing_headers_added():
    """确认我们没引入任何签名头(服务端本来也不校验)。"""
    c, _ = _client(_FakeResponse({"ret": {"code": 0}, "body": {}}))
    lowered = {k.lower() for k in c.headers()}
    for forbidden in ("sign", "x-sign", "x-signature", "nonce", "hmac", "timestamp"):
        assert forbidden not in lowered


# ---------------------------------------------------------------------------
# 响应解包
# ---------------------------------------------------------------------------

def test_request_unwraps_body():
    c, _ = _client(_FakeResponse({"ret": {"code": 0, "msg": "OK"}, "body": {"duration": 30}}))
    assert c.whoisip() == {"duration": 30}


def test_request_raises_on_business_error():
    c, _ = _client(_FakeResponse({"ret": {"code": 40001, "msg": "token 失效"}, "body": {}}))
    with pytest.raises(ApiError) as ei:
        c.whoisip()
    assert ei.value.code == 40001
    assert "40001" in str(ei.value)


def test_request_raises_on_http_error():
    c, _ = _client(_FakeResponse("server exploded", status_code=500))
    with pytest.raises(ApiError, match="500"):
        c.whoisip()


def test_request_raises_on_non_json():
    c, _ = _client(_FakeResponse("<html>not json</html>"))
    with pytest.raises(ApiError, match="合法 JSON"):
        c.whoisip()


def test_request_injects_uid_and_access_key():
    c, sess = _client(_FakeResponse({"ret": {"code": 0}, "body": {}}))
    c.zonelist(scene_type=1)
    body = sess.last["json"]
    assert body["uid"] == 100001  # uid 以数字发出
    assert body["access_key"] == "AK"
    assert body["scene_type"] == 1


def test_checkin_prize_sends_prize_id():
    """prize_id 是实测确认必需的参数。"""
    c, sess = _client(_FakeResponse({"ret": {"code": 0}, "body": {"sale_value": 10}}))
    c.checkin_prize(prize_id=100)
    assert sess.last["json"]["prize_id"] == 100


def test_checkin_prize_rejects_missing_prize_id():
    c, _ = _client(_FakeResponse({"ret": {"code": 0}, "body": {}}))
    with pytest.raises(ValueError, match="prize_id"):
        c.checkin_prize(prize_id=None)  # type: ignore[arg-type]


def test_checkin_prize_does_not_send_area_tag_id():
    """area_tag_id 经实测非必需,不应再发送。"""
    c, sess = _client(_FakeResponse({"ret": {"code": 0}, "body": {}}))
    c.checkin_prize(prize_id=100)
    assert "area_tag_id" not in sess.last["json"]


def test_raw_log_collects_endpoint_and_body():
    c, _ = _client(_FakeResponse({"ret": {"code": 0}, "body": {"sale_value": 10}}))
    log: list = []
    c.checkin_prize(prize_id=100, raw_log=log)
    assert log and log[0][0].endswith("checkinprize")
    assert log[0][1] == {"sale_value": 10}


# ---------------------------------------------------------------------------
# UserInfo
# ---------------------------------------------------------------------------

def test_user_info_parses_login_fields():
    info = UserInfo.from_login_body(
        {"uid": 100001, "phone": "13800000000", "token": "T",
         "coin": 0, "duration": 30, "is_real_name": 1}
    )
    assert info.uid == "100001"
    assert info.phone == "13800000000"
    assert info.duration_minutes == 30
    assert info.coin == 0.0
    assert info.is_real_name is True
    assert info.duration_text() == "30 分钟"
    assert info.coin_text() == "0.00"


def test_user_info_tolerates_renamed_fields():
    info = UserInfo.from_login_body({"user_id": 7, "mobile": "139", "left_duration": "45"})
    assert info.uid == "7"
    assert info.phone == "139"
    assert info.duration_minutes == 45


def test_user_info_missing_fields_default_safely():
    info = UserInfo.from_login_body({})
    assert info.uid == ""
    assert info.duration_minutes == 0
    assert info.coin == 0.0


# ---------------------------------------------------------------------------
# 凭据提取
# ---------------------------------------------------------------------------

def test_credentials_from_prefs_ok():
    cred = credentials_from_prefs({
        "flutter.uid": "100001",
        "flutter.token": "JWT",
        "flutter.last_login_access_key": "AK",
        "flutter.machine_id": "MID",
    })
    assert cred.uid == "100001"
    assert cred.access_key == "AK"


def test_credentials_from_prefs_lists_all_missing():
    with pytest.raises(ValueError) as ei:
        credentials_from_prefs({"flutter.uid": "1"})
    msg = str(ei.value)
    assert "flutter.token" in msg
    assert "flutter.last_login_access_key" in msg
    assert "flutter.machine_id" in msg


def test_credentials_from_account_uses_device_machine_id():
    acc = Account(
        uid="1",
        token="JWT",
        account={"flutter.last_login_access_key": "AK"},
        device={"flutter.machine_id": "MID"},
    )
    cred = credentials_from_account(acc)
    assert cred.uid == "1"
    assert cred.access_key == "AK"
    assert cred.machine_id == "MID"


def test_credentials_from_account_reports_missing_access_key():
    acc = Account(uid="1", account={}, device={"flutter.machine_id": "MID"})
    with pytest.raises(ValueError, match="access_key"):
        credentials_from_account(acc)


def test_credentials_from_account_reports_missing_machine_id():
    acc = Account(uid="1", account={"flutter.last_login_access_key": "AK"}, device={})
    with pytest.raises(ValueError, match="machine_id"):
        credentials_from_account(acc)
