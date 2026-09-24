"""命令行入口。

命令一览::

    panghebox list                     列出已保存账号(手机号/ID/时长)
    panghebox save [--note 备注]       保存当前客户端登录的账号
    panghebox switch <手机号|uid>       切换账号(自动关客户端→写入→启动)
    panghebox switch --next            切到下一个账号
    panghebox signin [手机号|--all]     签到
    panghebox refresh [--all]          刷新账号信息(时长/盒币)
    panghebox current                  显示当前登录账号
    panghebox doctor                   环境自检
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Sequence

from . import __version__
from .api import (
    ApiCredentials,
    ApiError,
    PangHeClient,
    credentials_from_account,
    credentials_from_prefs,
)
from .paths import AppContext, PathError, read_registry_version
from .signin import check_in_account, get_state
from .store import (
    Account,
    AccountStore,
    clear_login_state,
    read_prefs,
    switch_account_on_disk,
)
from . import switcher


# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------

def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _warn(msg: str) -> None:
    print(f"  ! {msg}")


def _err(msg: str) -> None:
    print(f"  ✗ {msg}", file=sys.stderr)


def _hr(title: str = "") -> None:
    if title:
        print(f"\n{title}")
        print("-" * max(len(title), 40))
    else:
        print()


# ---------------------------------------------------------------------------
# 共享的上下文构造
# ---------------------------------------------------------------------------

def _make_store(args: argparse.Namespace) -> AccountStore:
    root = Path(args.accounts_dir) if args.accounts_dir else _default_accounts_dir()
    return AccountStore(root)


def _default_accounts_dir() -> Path:
    """账号目录默认放在仓库根的 accounts/。"""
    here = Path(__file__).resolve()
    # panghebox/cli.py → 仓库根
    return here.parent.parent / "accounts"


def _make_context(args: argparse.Namespace) -> AppContext:
    return AppContext.discover(args.install_dir)


def _client_for(
    acc: Account,
    *,
    version: str | None = None,
    token: str | None = None,
) -> PangHeClient:
    cred: ApiCredentials = credentials_from_account(
        acc, version=version or read_registry_version() or "1.10.8", token=token
    )
    return PangHeClient(cred)


def _resolve_account(store: AccountStore, key: str) -> Account:
    """把用户输入的手机号或 uid 解析成账号档案。"""
    key = key.strip()
    if not key:
        raise ValueError("账号不能为空")
    try:
        return store.load(key)
    except FileNotFoundError:
        pass
    by_phone = store.find_by_phone(key)
    if by_phone:
        return by_phone
    raise FileNotFoundError(f"找不到账号: {key}(用 `panghebox list` 查看已保存账号)")


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    store = _make_store(args)
    accounts = store.list()
    if not accounts:
        print("还没有保存任何账号。")
        print("请先在客户端里登录一个账号,然后运行:`panghebox save`")
        return 0

    current_uid = None
    try:
        prefs = read_prefs(_make_context(args).prefs)
        current_uid = str(prefs.get("flutter.uid", "") or "")
    except (FileNotFoundError, ValueError):
        pass

    _hr(f"已保存账号 ({len(accounts)})")
    print(f"{'':2} {'手机号':<14} {'ID':<10} {'剩余时长':<12} {'盒币':<8} 备注")
    print("-" * 70)
    for a in accounts:
        mark = "*" if a.uid == current_uid else " "
        note = f" {a.note}" if a.note else ""
        print(
            f"{mark:2} {a.label:<14} {a.uid:<10} {a.duration_text():<12} "
            f"{a.coin_text():<8}{note}"
        )
    if current_uid:
        print("\n* = 当前客户端登录的账号")
    return 0


def cmd_current(args: argparse.Namespace) -> int:
    ctx = _make_context(args)
    try:
        prefs = read_prefs(ctx.prefs)
    except FileNotFoundError:
        _err(f"找不到账号文件: {ctx.prefs}")
        return 1
    except ValueError as e:
        _err(str(e))
        return 1

    uid = prefs.get("flutter.uid")
    if uid in (None, "", 0, "0"):
        print("当前未登录。")
        return 0

    print(f"当前登录账号:")
    _ok(f"手机号: {prefs.get('flutter.userphone') or '未知'}")
    _ok(f"ID:     {uid}")
    _ok(f"自动登录: {prefs.get('flutter.isAutoLogin')}")
    _ok(f"上次选区: {prefs.get('flutter.lastFlowZoneName') or '未知'}")
    return 0


def cmd_save(args: argparse.Namespace) -> int:
    store = _make_store(args)
    ctx = _make_context(args)

    if switcher.app_is_running():
        _warn("客户端正在运行,登录态可能还没写入磁盘。")
        _warn("建议:确认已登录成功后按回车继续,或先退出客户端再保存。")
        try:
            input("  按回车继续,_错误Ctrl+C 取消: ")
        except KeyboardInterrupt:
            print("\n已取消。")
            return 130

    try:
        prefs = read_prefs(ctx.prefs)
    except (FileNotFoundError, ValueError) as e:
        _err(str(e))
        return 1

    try:
        acc = Account.from_prefs(prefs, note=args.note or "")
    except ValueError as e:
        _err(str(e))
        return 1

    path = store.save(acc)
    _ok(f"已保存账号 {acc.label} (ID {acc.uid})")
    print(f"    档案: {path}")

    if args.fetch:
        _refresh_one(acc, store)
    return 0


def cmd_switch(args: argparse.Namespace) -> int:
    store = _make_store(args)
    ctx = _make_context(args)
    accounts = store.list()

    if not accounts:
        _err("还没有保存任何账号,先运行 `panghebox save`。")
        return 1

    if args.next:
        target = _pick_next(store, ctx, accounts)
        if target is None:
            _err("无法确定下一个账号。")
            return 1
    elif args.account:
        try:
            target = _resolve_account(store, args.account)
        except (FileNotFoundError, ValueError) as e:
            _err(str(e))
            return 1
    else:
        _err("请指定账号(手机号或 uid),或用 --next。")
        return 1

    _hr(f"切换到 {target.label} (ID {target.uid})")

    # 1) 关掉客户端 —— 必须先关,否则退出时会用内存状态回写覆盖
    if switcher.any_running():
        _ok("检测到客户端在运行,正在退出…")
        if not switcher.kill_app():
            _err("无法结束客户端进程,请手动关闭后重试。")
            return 1
        _ok("客户端已退出")
    else:
        _ok("客户端未在运行")

    # 2) 等文件句柄释放
    if not switcher.wait_for_prefs_release(ctx.prefs):
        _warn(f"账号文件仍被占用: {ctx.prefs}")
        _warn("继续尝试写入,若失败请手动关闭残留进程。")

    # 2.5) 预刷新目标账号的 token(免短信续签)。
    # 失败也不阻塞:客户端启动时的自动登录会自行续签,这里只是为了
    # 让写进去的 token 立即可用(便于工具自身的验证调用)。
    _refresh_one(target, store, quiet=True)

    # 3) 写入
    try:
        merged = switch_account_on_disk(ctx.prefs, target, backup=not args.no_backup)
    except (FileNotFoundError, ValueError, OSError) as e:
        _err(f"写入失败: {e}")
        return 1

    _ok("账号数据已写入")
    if not args.no_backup:
        _ok(f"原文件已备份为 {ctx.prefs.name}.bak")
    _ok(f"设备标识 machine_id={merged.get('flutter.machine_id')}(随账号走)")
    _ok(f"渠道标识 setup_channel={merged.get('flutter.setup_channel')}(保持不变)")

    # 4) 启动
    if not args.no_launch:
        try:
            pid = switcher.launch_app(ctx.exe)
            _ok(f"客户端已启动 (pid {pid})")
        except (FileNotFoundError, OSError) as e:
            _err(f"启动失败: {e}")
            return 1
    else:
        _warn("按要求未自动启动客户端")

    return 0


def _pick_next(store: AccountStore, ctx: AppContext, accounts: list[Account]) -> Account | None:
    """按列表顺序取当前账号的下一个(循环)。"""
    current_uid = None
    try:
        prefs = read_prefs(ctx.prefs)
        current_uid = str(prefs.get("flutter.uid", "") or "")
    except (FileNotFoundError, ValueError):
        pass

    if current_uid:
        for i, a in enumerate(accounts):
            if a.uid == current_uid:
                return accounts[(i + 1) % len(accounts)]
    return accounts[0] if accounts else None


def cmd_signin(args: argparse.Namespace) -> int:
    store = _make_store(args)
    ctx = _make_context(args)
    store_list = store.list()

    if args.all:
        if not store_list:
            _err("还没有保存任何账号。")
            return 1
        targets = store_list
    elif args.account:
        try:
            targets = [_resolve_account(store, args.account)]
        except (FileNotFoundError, ValueError) as e:
            _err(str(e))
            return 1
    else:
        # 默认:对当前登录账号签到
        try:
            prefs = read_prefs(ctx.prefs)
            acc = Account.from_prefs(prefs)
        except (FileNotFoundError, ValueError) as e:
            _err(f"读取当前账号失败: {e}")
            _err("可改用 `panghebox signin --all` 或指定账号。")
            return 1
        targets = [acc]

    failures = 0
    for acc in targets:
        _hr(f"签到 {acc.label} (ID {acc.uid})")
        rc = _signin_one(acc, store, dump_raw=args.dump_raw, force=args.force)
        if rc != 0:
            failures += 1

    if len(targets) > 1:
        _hr()
        print(f"完成:{len(targets) - failures}/{len(targets)} 成功")
    return 1 if failures else 0


def _signin_one(acc: Account, store: AccountStore, *, dump_raw: bool = False, force: bool = False) -> int:
    raw_log: list = []
    result = None

    # 签到前先续签 token(空 token 头,对过期/损坏的档案都安全,失败不阻塞)。
    # 服务端 token 有效期约 10 小时,预刷新保证签到用的永远是活 token。
    _refresh_one(acc, store, quiet=True)

    try:
        client = _client_for(acc)
    except ValueError as e:
        _err(str(e))
        return 1

    try:
        with client:
            state = get_state(client, raw_log=raw_log)
            if state.parsed:
                print("  签到进度: " + state.summary())
                print(state.table())
            result = check_in_account(client, force=force, raw_log=raw_log)
    except ApiError as e:
        _err(f"接口调用失败: {e}")
        if dump_raw:
            _dump(raw_log)
        return 1

    print()
    if result.ok:
        _ok(result.message)
    elif result.already_done:
        _warn(result.message)
    else:
        _warn(result.message)

    if dump_raw and (raw_log or result.raw):
        if result.raw and not any(r[1] is result.raw for r in raw_log):
            raw_log.append(("(checkinprize)", result.raw))
        _dump(raw_log)

    return 0 if result.ok else 1


def _dump(raw_log: list) -> None:
    """打印原始响应,用于首次校准字段名。"""
    print("\n  --- 原始响应 dump ---")
    for ep, body in raw_log:
        print(f"  [{ep}]")
        print(json.dumps(body, ensure_ascii=False, indent=4)[:4000])
    print("  --- dump 结束 ---")


def cmd_refresh(args: argparse.Namespace) -> int:
    store = _make_store(args)
    if args.all:
        accounts = store.list()
    elif args.account:
        try:
            accounts = [_resolve_account(store, args.account)]
        except (FileNotFoundError, ValueError) as e:
            _err(str(e))
            return 1
    else:
        _err("请指定账号或用 --all。")
        return 1

    if not accounts:
        _err("还没有保存任何账号。")
        return 1

    for a in accounts:
        _refresh_one(a, store)
    return 0


def _refresh_one(acc: Account, store: AccountStore, *, quiet: bool = False) -> bool:
    """checklogin 刷新 token,并回写档案(token/手机号/时长/盒币)。

    返回是否成功。HTTP 层的 getuserinfo 长期 500,这里统一走 checklogin
    ——它既续签 token,又一并带回时长与盒币。
    """
    if not quiet:
        print(f"  刷新 {acc.label} …")
    try:
        client = _client_for(acc)
    except ValueError as e:
        if not quiet:
            _err(str(e))
        return False
    try:
        # 刷新一律用空 Authorization 头:实测过期 token 可通过,但格式
        # 损坏的 token 会被拒;空头对两种情况都安全。
        with _client_for(acc, token="") as client:
            info = client.check_login()
    except ApiError as e:
        if not quiet:
            _err(f"刷新失败: {e}")
        return False

    if info.token:
        acc.token = info.token
        acc.account["flutter.token"] = info.token
    if info.phone:
        acc.phone = info.phone
        acc.account["flutter.userphone"] = info.phone
    acc.duration_minutes = info.duration_minutes
    acc.coin = info.coin
    acc.last_checked = time.time()
    store.save(acc)
    if not quiet:
        _ok(f"{acc.label}: 剩余 {info.duration_text()}, 盒币 {info.coin_text()}, token 已续签")
    return True


def cmd_clear(args: argparse.Namespace) -> int:
    """清空客户端登录态,以便登录一个新账号。"""
    ctx = _make_context(args)

    # 必须先退出客户端,否则退出时会用内存状态回写覆盖
    if switcher.any_running():
        _ok("检测到客户端在运行,正在退出…")
        if not switcher.kill_app():
            _err("无法结束客户端进程,请手动关闭后重试。")
            return 1
        _ok("客户端已退出")

    try:
        removed = clear_login_state(ctx.prefs, backup=not args.no_backup)
    except (FileNotFoundError, ValueError, OSError) as e:
        _err(f"清理失败: {e}")
        return 1

    _ok(f"已删除 {len(removed)} 个登录相关键(等价新装首次启动)")
    if not args.no_backup:
        _ok(f"原文件已备份为 {ctx.prefs.name}.bak")
    _ok("machine_id 已一并删除,下次登录客户端会生成全新设备标识")

    if args.launch:
        try:
            pid = switcher.launch_app(ctx.exe)
            _ok(f"客户端已启动 (pid {pid}),请在登录页登录新账号")
        except (FileNotFoundError, OSError) as e:
            _err(f"启动失败: {e}")
            return 1
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    _hr("环境自检")

    # 安装目录
    try:
        ctx = _make_context(args)
        _ok(f"安装目录: {ctx.install_dir}")
        _ok(f"主程序存在: {ctx.exe.exists()}")
    except PathError as e:
        _err(str(e))
        return 1

    # 账号文件
    try:
        prefs = read_prefs(ctx.prefs)
        uid = prefs.get("flutter.uid")
        _ok(f"账号文件: {ctx.prefs}")
        _ok(f"当前登录: {prefs.get('flutter.userphone') or '未知'} (uid {uid})")
        if not prefs.get("flutter.machine_id"):
            _warn("prefs 里没有 machine_id")
        if not prefs.get("flutter.last_login_access_key"):
            _warn("prefs 里没有 last_login_access_key")
    except FileNotFoundError:
        _warn(f"账号文件不存在: {ctx.prefs}(客户端从未运行过?)")
    except ValueError as e:
        _err(str(e))

    # 进程
    running = switcher.running_pids()
    if running:
        _warn(f"客户端正在运行 (pid: {running})")
    else:
        _ok("客户端未运行")

    # 账号档案
    store = _make_store(args)
    accounts = store.list()
    _ok(f"已保存账号: {len(accounts)} 个 ({store.root})")

    # 版本
    ver = read_registry_version()
    _ok(f"客户端版本: {ver or '未知(将用默认值)'}")

    return 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="panghebox",
        description="胖盒云电脑 多账号切换 + 自动签到工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--version", action="version", version=f"panghebox-multi {__version__}")
    p.add_argument("--install-dir", help="胖盒安装目录(自动探测失败时指定)")
    p.add_argument("--accounts-dir", help="账号档案目录(默认 仓库根/accounts)")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出已保存账号").set_defaults(func=cmd_list)
    sub.add_parser("current", help="显示当前登录账号").set_defaults(func=cmd_current)

    sp = sub.add_parser("save", help="保存当前客户端登录的账号")
    sp.add_argument("--note", help="备注")
    sp.add_argument("--fetch", action="store_true", help="顺带查询时长/盒币")
    sp.set_defaults(func=cmd_save)

    sp = sub.add_parser("switch", help="切换账号")
    sp.add_argument("account", nargs="?", help="手机号或 uid")
    sp.add_argument("--next", action="store_true", help="切到下一个账号")
    sp.add_argument("--no-launch", action="store_true", help="切完不自动启动客户端")
    sp.add_argument("--no-backup", action="store_true", help="不备份原 prefs")
    sp.set_defaults(func=cmd_switch)

    sp = sub.add_parser("signin", help="每日签到")
    sp.add_argument("account", nargs="?", help="手机号或 uid")
    sp.add_argument("--all", action="store_true", help="所有已保存账号都签")
    sp.add_argument("--force", action="store_true", help="即使显示已签也再试一次")
    sp.add_argument("--dump-raw", action="store_true", help="打印原始响应(用于校准字段名)")
    sp.set_defaults(func=cmd_signin)

    sp = sub.add_parser("refresh", help="续签 token 并刷新账号信息(时长/盒币)")
    sp.add_argument("account", nargs="?", help="手机号或 uid")
    sp.add_argument("--all", action="store_true", help="刷新全部")
    sp.set_defaults(func=cmd_refresh)

    sub.add_parser("doctor", help="环境自检").set_defaults(func=cmd_doctor)

    sp = sub.add_parser(
        "clear",
        help="清空客户端登录态(用于登录新账号;删键而非置空,不会导致客户端转圈)",
    )
    sp.add_argument("--launch", action="store_true", help="清理后自动启动客户端")
    sp.add_argument("--no-backup", action="store_true", help="不备份原 prefs")
    sp.set_defaults(func=cmd_clear)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except PathError as e:
        _err(str(e))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
