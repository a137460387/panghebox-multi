"""胖盒云电脑客户端的 HTTP API 封装。

逆向结论(来自客户端日志,均有证据):

* 服务端: ``https://h5-proxy.panghebox.com``
* 认证: 请求头固定 6 项 —— Authorization(JWT) / Version / Machineid /
  Accesskey / Platform / X-Client-Request-Id。
  **没有任何请求签名**(无 HMAC / nonce / timestamp / sign),
  所以纯 Python 可以直接复现请求。
* 响应信封: ``{"ret":{"code":0,"msg":"OK","request_id":"nika_..."},"body":{...}}``
* JWT payload: ``{"acceessKey":"<uuid>","iat":<ts>,"uid":<n>}``
  (``acceessKey`` 三个 e 是服务端自己的拼写,照抄即可)

账号的剩余时长与盒币来自**登录响应**,字段为 ``duration``(分钟) 与 ``coin``。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import requests

DEFAULT_BASE_URL = "https://h5-proxy.panghebox.com"

# 端点路径(从 Dart AOT 快照提取)
EP_WHOISIP = "/v1/nika/client/whoisip"
EP_LOGIN = "/v1/nika/client/login"
EP_GET_USER_INFO = "/v1/nika/client/getuserinfo"
EP_CHECK_LOGIN = "/v1/nika/client/checklogin"
EP_ZONELIST = "/v1/nika/client/zonelist"
EP_GET_CHECKIN_LIST = "/v1/nika/client/getcheckinlist"
EP_CHECKIN_PRIZE = "/v1/nika/client/checkinprize"
EP_CHECKIN_INVITE_CODE = "/v1/nika/client/checkinvitecode"

# 客户端自称的版本,服务端可能校验;可从注册表读取覆盖
DEFAULT_VERSION = "1.10.8"


class ApiError(RuntimeError):
    """接口调用失败(网络或业务错误码)。"""

    def __init__(self, message: str, *, code: int | None = None, raw: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.raw = raw


@dataclass
class UserInfo:
    """账号概要。对应登录响应里的字段。"""

    uid: str = ""
    phone: str = ""
    token: str = ""
    coin: float = 0.0
    duration_minutes: int = 0
    is_real_name: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_login_body(cls, body: dict[str, Any]) -> "UserInfo":
        """从登录/用户信息响应体构造。字段名做了兼容取值。"""
        return cls(
            uid=str(_first(body, "uid", "user_id", default="")),
            phone=str(_first(body, "phone", "mobile", "name", default="")),
            token=str(_first(body, "token", "access_token", default="")),
            coin=_as_float(_first(body, "coin", "coins", "balance", default=0)),
            duration_minutes=int(_as_float(_first(body, "duration", "left_duration", default=0))),
            is_real_name=bool(_first(body, "is_real_name", "is_auth", default=False)),
            raw=body,
        )

    def duration_text(self) -> str:
        return f"{self.duration_minutes} 分钟"

    def coin_text(self) -> str:
        return f"{self.coin:.2f}"


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """按顺序取第一个存在的键,兼容服务端可能的字段改名。"""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class ApiCredentials:
    """调用接口所需的一组凭据。

    Attributes:
        uid: 用户 ID。
        token: 登录 JWT。
        access_key: 设备级 accessKey。
        machine_id: 设备 ID。
        version: 客户端版本。
        platform: 平台标识,固定 window。
    """

    uid: str
    token: str
    access_key: str
    machine_id: str
    version: str = DEFAULT_VERSION
    platform: str = "window"


class PangHeClient:
    """同步 HTTP 客户端。

    Example:
        >>> cred = ApiCredentials(uid="1", token="t", access_key="k", machine_id="m")
        >>> client = PangHeClient(cred)          # doctest: +SKIP
        >>> info = client.get_user_info()        # doctest: +SKIP
    """

    def __init__(
        self,
        cred: ApiCredentials,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ) -> None:
        self.cred = cred
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = session or requests.Session()
        self._owns_session = session is None

    # -- 基础设施 ---------------------------------------------------------

    def headers(self) -> dict[str, str]:
        """构造与客户端一致的请求头。"""
        return {
            "Authorization": self.cred.token,
            "Version": self.cred.version,
            "Machineid": self.cred.machine_id,
            "Accesskey": self.cred.access_key,
            "Platform": self.cred.platform,
            "X-Client-Request-Id": f"c_{uuid.uuid4().hex[:12]}_{uuid.uuid4().hex[:6]}",
            "Content-Type": "application/json",
        }

    def request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        method: str = "POST",
    ) -> dict[str, Any]:
        """发一次请求并解包响应信封。

        Args:
            path: 形如 ``/v1/nika/client/...`` 的路径。
            payload: 请求体,会补上 uid / access_key / scene_type。
            method: GET 或 POST。

        Returns:
            响应里的 ``body`` 部分(可能为空字典)。

        Raises:
            ApiError: 网络错误、非 2xx、或 ``ret.code != 0``。
        """
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        body = dict(payload or {})
        body.setdefault("uid", _as_int_or_str(self.cred.uid))
        body.setdefault("access_key", self.cred.access_key)

        try:
            resp = self._session.request(
                method,
                url,
                headers=self.headers(),
                json=body if method.upper() != "GET" else None,
                params=body if method.upper() == "GET" else None,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise ApiError(f"网络请求失败: {e}") from e

        if resp.status_code >= 400:
            raise ApiError(
                f"服务端返回 HTTP {resp.status_code}: {resp.text[:300]}",
                raw=resp.text,
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise ApiError(f"响应不是合法 JSON: {resp.text[:300]}", raw=resp.text) from e

        ret = data.get("ret") or {}
        code = ret.get("code")
        if code not in (0, None):
            raise ApiError(
                f"接口返回错误 code={code} msg={ret.get('msg')!r}",
                code=code,
                raw=data,
            )
        out = data.get("body")
        return out if isinstance(out, dict) else {}

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> "PangHeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 业务接口 ---------------------------------------------------------

    def whoisip(self) -> dict[str, Any]:
        """探活 + 校验登录态是否有效。"""
        return self.request(EP_WHOISIP, method="GET")

    def get_user_info(self, *, raw_log: list | None = None) -> UserInfo:
        """获取账号信息(时长/盒币)。

        优先调 getuserinfo;该接口在历史上出现过 500,失败时回退到
        登录响应字段。调用方若要严格模式可自行捕获 ApiError。
        """
        body = self.request(EP_GET_USER_INFO)
        if raw_log is not None:
            raw_log.append((EP_GET_USER_INFO, body))
        return UserInfo.from_login_body(body)

    def check_login(self, *, raw_log: list | None = None) -> UserInfo:
        """用 accessKey+uid 换取新 token(即客户端启动时的"自动登录")。

        实测确认(2026-09-24):
        * 只需要 body 里的 ``access_key`` + ``uid``,**无需短信验证码**;
        * Authorization 头里带**过期** token 也能正常刷新;
        * 响应即登录响应的形状:``uid / phone / is_real_name / token /
          is_login / coin / duration`` —— 一并带回剩余时长(分钟)与盒币。

        服务端 token 存在约 10 小时量级的有效期,档案里的 token 会过期;
        本接口是档案"自愈"的基础:任何账号只要有 accessKey 就能随时续签。

        另注:HTTP 层的 ``getuserinfo`` 长期返回 500(服务端问题),
        查询账号信息应优先使用本接口。
        """
        body = self.request(EP_CHECK_LOGIN)
        if raw_log is not None:
            raw_log.append((EP_CHECK_LOGIN, body))
        return UserInfo.from_login_body(body)

    def get_checkin_list(self, *, raw_log: list | None = None) -> dict[str, Any]:
        """取签到 7 天列表。返回原始 body,由 signin 模块解析。"""
        body = self.request(EP_GET_CHECKIN_LIST)
        if raw_log is not None:
            raw_log.append((EP_GET_CHECKIN_LIST, body))
        return body

    def checkin_prize(
        self,
        *,
        prize_id: int | str,
        extra: dict[str, Any] | None = None,
        raw_log: list | None = None,
    ) -> dict[str, Any]:
        """领取某一天的签到奖励。

        **``prize_id`` 是必需的**(实测:传了才有正常业务响应,不传直接 HTTP 500)。
        它的值来自 ``getcheckinlist`` 响应里每天的 ``prize_id`` 字段。

        注:二进制常量池里 ``checkinprize`` 旁还有个 ``area_tag_id``,但实测
        传不传都不影响结果,故不再使用。

        Args:
            prize_id: 目标那天的奖励 ID。
            extra: 额外的请求字段(一般不需要)。
            raw_log: 收集原始响应。

        Raises:
            ValueError: prize_id 为空。
        """
        if prize_id in (None, "", 0):
            raise ValueError("checkinprize 需要 prize_id")

        payload: dict[str, Any] = {"prize_id": prize_id}
        if extra:
            payload.update(extra)
        body = self.request(EP_CHECKIN_PRIZE, payload)
        if raw_log is not None:
            raw_log.append((EP_CHECKIN_PRIZE, body))
        return body

    def zonelist(self, scene_type: int = 1) -> dict[str, Any]:
        """取分区列表(用于拿 area_tag_id 等)。"""
        return self.request(EP_ZONELIST, {"scene_type": scene_type})


def _as_int_or_str(v: str) -> int | str:
    """服务端 uid 有时是数字有时是字符串,尽量给数字。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def credentials_from_prefs(prefs: dict[str, Any]) -> ApiCredentials:
    """从 prefs 字典构造 API 凭据。

    Raises:
        ValueError: 缺少 uid / token 等必要字段。
    """
    uid = prefs.get("flutter.uid")
    token = prefs.get("flutter.token")
    access_key = prefs.get("flutter.last_login_access_key")
    machine_id = prefs.get("flutter.machine_id")

    missing = [
        name
        for name, val in (
            ("flutter.uid", uid),
            ("flutter.token", token),
            ("flutter.last_login_access_key", access_key),
            ("flutter.machine_id", machine_id),
        )
        if val in (None, "")
    ]
    if missing:
        raise ValueError(f"prefs 缺少必要字段: {', '.join(missing)}")

    return ApiCredentials(
        uid=str(uid),
        token=str(token),
        access_key=str(access_key),
        machine_id=str(machine_id),
    )


def credentials_from_account(
    acc: Any,
    *,
    version: str = DEFAULT_VERSION,
    token: str | None = None,
) -> ApiCredentials:
    """从 Account 档案构造 API 凭据。

    Args:
        acc: 账号档案。
        version: 客户端版本号。
        token: 显式指定 Authorization 用的 token。传空串表示不带 token
            ——``check_login`` 刷新时应当传 ``""``:实测过期 token 能通过,
            但格式损坏的 token 会被服务端拒绝,空头则永远安全。
    """
    acc_data = getattr(acc, "account", {}) or {}
    dev_data = getattr(acc, "device", {}) or {}

    access_key = (
        acc_data.get("flutter.last_login_access_key")
        or dev_data.get("_access_key")
        or ""
    )
    # machine_id 自 v0.1 起随账号走,存在 account 段;旧档案在 device 段,做兼容回退
    machine_id = (
        acc_data.get("flutter.machine_id")
        or dev_data.get("flutter.machine_id")
        or ""
    )

    if not access_key:
        raise ValueError(f"账号 {getattr(acc, 'label', acc)} 档案里没有 access_key")
    if not machine_id:
        raise ValueError(f"账号 {getattr(acc, 'label', acc)} 档案里没有 machine_id")

    return ApiCredentials(
        uid=str(getattr(acc, "uid", "")),
        token=str(getattr(acc, "token", "")) if token is None else token,
        access_key=str(access_key),
        machine_id=str(machine_id),
        version=version,
    )
