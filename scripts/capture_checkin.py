"""mitmproxy 抓包插件:专抓胖盒云电脑的签到接口。

用法::

    mitmdump -s scripts/capture_checkin.py --listen-port 8899 \
        --set connection_strategy=lazy --mode regular \
        --save-stream-file dumps/checkin.mitm

设计要点:
* **只落盘签到相关流量**,其余一律放行不记录,避免把整个客户端流量
  (含 RTC 长连接)写进文件——那会非常大,而且会把无关的凭据也存下来。
* 命中签到端点时,把 request body / headers 与 response body 完整打印到
  控制台,同时写一份精简 JSON 到 dumps/,方便直接交给工具校准字段名。
* 打印时会**对 token 打码**,防止抓包输出被顺手贴到公开地方。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from mitmproxy import http

# 我们关心的端点(签到相关,以及用于定位 area_tag_id 的 zonelist)
TARGET_PATTERNS = [
    re.compile(r"/v1/nika/client/getcheckinlist"),
    re.compile(r"/v1/nika/client/checkinprize"),
    re.compile(r"/v1/nika/client/checkinvitecode"),
    re.compile(r"/v1/nika/client/zonelist"),
    re.compile(r"/v1/nika/client/getuserinfo"),
]

HOST_HINT = "panghebox.com"

OUTDIR = Path(__file__).resolve().parent.parent / "dumps"
CAPTURED: list[dict] = []

# 打码用:Authorization 的 JWT 只留头尾
_JWT_RE = re.compile(r"(eyJ[A-Za-z0-9_-]{6})[A-Za-z0-9_.-]{20,}([A-Za-z0-9_-]{6})")


def _mask(s: str) -> str:
    """把长 token 打码,保留可辨认的头尾。"""
    return _JWT_RE.sub(r"\1...\2", s)


def _is_target(flow: http.HTTPFlow) -> bool:
    url = flow.request.pretty_url
    if HOST_HINT not in url:
        return False
    return any(p.search(url) for p in TARGET_PATTERNS)


def _body_text(message) -> str:  # noqa: ANN001
    """安全地取出 body 文本。"""
    try:
        t = message.get_text(strict=False)
        return t if t is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def response(flow: http.HTTPFlow) -> None:
    """请求完成时调用。"""
    if not _is_target(flow):
        return

    req = flow.request
    resp = flow.response

    entry = {
        "ts": time.strftime("%H:%M:%S"),
        "method": req.method,
        "url": req.pretty_url,
        "request_headers": {k: _mask(v) for k, v in req.headers.items()},
        "request_body": _body_text(req),
        "status": resp.status_code if resp else None,
        "response_body": _body_text(resp) if resp else "",
    }
    CAPTURED.append(entry)

    # 控制台输出(凭据已打码)
    ep = req.path.split("?")[0]
    print("\n" + "=" * 78)
    print(f"[签到抓包] {entry['ts']}  {req.method} {ep}  -> {entry['status']}")
    print("-" * 78)
    print("请求体:")
    print(entry["request_body"] or "(空)")
    print("-" * 78)
    print("响应体:")
    body = entry["response_body"]
    print(body if len(body) <= 4000 else body[:4000] + f"\n...(共 {len(body)} 字节)")
    print("=" * 78, flush=True)

    # 落盘精简 JSON
    OUTDIR.mkdir(parents=True, exist_ok=True)
    out = OUTDIR / "checkin_capture.json"
    out.write_text(
        json.dumps(CAPTURED, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def done() -> None:
    """退出时汇总。"""
    if not CAPTURED:
        print("\n[签到抓包] 全程没有捕获到签到接口调用。")
        print("  可能原因:① 没点「立即签到」 ② 客户端没走代理 ③ 没装 mitmproxy CA 证书")
        return
    print(f"\n[签到抓包] 共捕获 {len(CAPTURED)} 个请求,已写入 {OUTDIR / 'checkin_capture.json'}")
