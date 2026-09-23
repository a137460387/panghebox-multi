"""监听胖盒客户端日志,捕获签到接口的请求与响应。

不需要代理:客户端自己会把完整的请求参数与响应体写进 log/nika-*.txt。

用法::

    python scripts/watch_log.py            # 持续监听,出现签到请求就打印
    python scripts/watch_log.py --once     # 只扫一次当前日志(不等待)

命中后会打印请求参数与响应体,并写一份 JSON 到 dumps/checkin_capture.json,
可直接用于校准 panghebox/signin.py 的字段名。
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

INSTALL_DIR = Path(r"D:\胖盒云电脑\PangHeBox")
LOG_DIR = INSTALL_DIR / "log"
OUTDIR = Path(__file__).resolve().parent.parent / "dumps"

# 我们关心的接口
TARGETS = ("getcheckinlist", "checkinprize", "checkinvitecode", "zonelist", "getuserinfo")

# 匹配: {requesturl: <url>, params: {...}, result: {...}}
REQ_RE = re.compile(
    r"\{requesturl:\s*(?P<url>\S+?),\s*params:\s*(?P<params>\{.*?\}),\s*result:\s*(?P<result>\{.*?\})\}\s*$"
)
# 匹配: [api_trace] phase=RESPONSE_BODY api=<name> ... data=<json>
BODY_RE = re.compile(
    r"\[api_trace\]\s+phase=RESPONSE_BODY\s+api=(?P<api>\w+).*?\sdata=(?P<data>\{.*\})\s*$"
)
# 匹配: [api_trace] phase=REQUEST api=<name> ... url=<url>
TRACE_RE = re.compile(
    r"\[api_trace\]\s+phase=REQUEST\s+api=(?P<api>\w+)\s+request_id=(?P<rid>\S+)\s+uid=(?P<uid>\d+)\s+method=(?P<method>\w+)\s+url=(?P<url>\S+)"
)


def newest_log() -> Path | None:
    if not LOG_DIR.is_dir():
        return None
    logs = sorted(LOG_DIR.glob("nika-*-log.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def _mask(s: str, keep: int = 12) -> str:
    """把 token 打码。"""
    s = re.sub(r"(eyJ[A-Za-z0-9_-]{6})[A-Za-z0-9_.-]{20,}", r"\1...<masked>", s)
    if len(s) > 400:
        return s[:400] + f"...(共 {len(s)} 字符)"
    return s


def load_capture() -> list[dict]:
    p = OUTDIR / "checkin_capture.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def save_capture(items: list[dict]) -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    (OUTDIR / "checkin_capture.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def scan_text(text: str, *, verbose: bool = False) -> list[dict]:
    """从日志文本里抽出目标接口的调用记录。"""
    found: list[dict] = []

    for line in text.splitlines():
        m = REQ_RE.search(line.strip())
        if m:
            url = m.group("url")
            api = url.rsplit("/", 1)[-1]
            if api in TARGETS:
                found.append({
                    "source": "requesturl",
                    "api": api,
                    "url": url,
                    "params": m.group("params"),
                    "result": _mask(m.group("result")),
                })
            continue

        m = BODY_RE.search(line.strip())
        if m and m.group("api") in TARGETS:
            found.append({
                "source": "api_trace",
                "api": m.group("api"),
                "data": _mask(m.group("data")),
            })

    return found


def report(new_items: list[dict]) -> None:
    """打印并保存捕获结果。"""
    print("\n" + "=" * 78)
    print(f"捕获到 {len(new_items)} 条签到相关记录")
    print("=" * 78)
    for it in new_items:
        print(f"\n[{it.get('api')}]  (来源: {it.get('source')})")
        for k in ("url", "params", "result", "data"):
            if k in it:
                print(f"  {k}:")
                val = it[k]
                print(f"    {val if len(val) <= 2000 else val[:2000] + ' ...'}")
    print("=" * 78)

    existing = load_capture()
    existing.extend(new_items)
    save_capture(existing)
    print(f"\n已追加保存到 {OUTDIR / 'checkin_capture.json'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="监听客户端日志捕获签到接口")
    ap.add_argument("--once", action="store_true", help="只扫一次,不持续等待")
    ap.add_argument("--timeout", type=float, default=300.0, help="持续监听的最长秒数")
    args = ap.parse_args(argv)

    log = newest_log()
    if log is None:
        print(f"找不到日志文件: {LOG_DIR}")
        return 1

    print(f"监听日志: {log.name}")
    print("请现在去客户端点击「立即签到」…\n")

    baseline = scan_text(log.read_text(encoding="utf-8", errors="ignore"))
    seen = len(baseline)
    print(f"(日志里已有的签到记录: {seen} 条, 从现在起只报告新增)")

    if args.once:
        if baseline:
            report(baseline)
            return 0
        print("当前日志里没有签到记录。去掉 --once 可持续监听。")
        return 1

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        time.sleep(1.5)
        cur = newest_log()
        if cur is None:
            continue
        if cur != log:
            print(f"切换到新日志: {cur.name}")
            log, seen = cur, 0

        items = scan_text(log.read_text(encoding="utf-8", errors="ignore"))
        if len(items) > seen:
            report(items[seen:])
            # 收到 checkinprize 就说明签到动作已完成
            if any(i.get("api") == "checkinprize" for i in items[seen:]):
                print("\n检测到 checkinprize 调用,签到动作已完成 ✓")
                return 0
            seen = len(items)

    print("\n超时:未捕获到签到请求。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
