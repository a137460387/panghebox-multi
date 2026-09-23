"""store 模块测试:白名单合并、原子写入、档案读写。

这些测试覆盖的全是最容易踩坑的地方,所以用真实形状的 prefs 数据
(键名与真实客户端一致,值全部是伪造的)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panghebox.store import (
    ACCOUNT_KEYS,
    DEVICE_KEYS,
    Account,
    AccountStore,
    account_snapshot,
    device_snapshot,
    merge_account_into_prefs,
    read_prefs,
    switch_account_on_disk,
    write_prefs,
)

# 一份形状贴近真实客户端的 prefs(54 键的缩小版,值都是假的)
DEVICE_MACHINE_ID = "11111111-2222-3333-4444-555555555555"
DEVICE_ACCESS_KEY = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def fake_prefs(uid: str = "100001", phone: str = "13800000001") -> dict:
    return {
        # 账号级
        "flutter.uid": uid,
        "flutter.token": f"FAKE.JWT.FOR.{uid}",
        "flutter.userphone": phone,
        "flutter.isAutoLogin": True,
        "flutter.last_login_access_key": DEVICE_ACCESS_KEY,
        "flutter.personalDiskLoginSessionId": f"sess-{uid}",
        "flutter.new_user_ctivity": "101-30-24",
        "flutter.lastFlowZone": 93,
        "flutter.lastFlowZoneName": "广东2区(标配)",
        # 设备级(跨账号共享,绝不能被动)
        "flutter.machine_id": DEVICE_MACHINE_ID,
        "flutter.setup_channel": "bing-pc",
        "flutter.ocpc": "deadbeefdeadbeefdeadbeefdeadbeef",
        # 纯本机状态
        "flutter.qualitys": "20000000-1",
        "flutter.quality": "20000000-1",
        "flutter.speed_test_cooldown_by_uid": '{"100001":{"full":123}}',
        "flutter.leListenPort": 26660,
        # 引导进度(按 uid 分键)
        "flutter.guide_device_100001_charge": DEVICE_MACHINE_ID,
        "flutter.guide_device_100002_charge": DEVICE_MACHINE_ID,
    }


@pytest.fixture()
def prefs_file(tmp_path: Path) -> Path:
    p = tmp_path / "shared_preferences.json"
    p.write_text(json.dumps(fake_prefs(), ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 快照抽取
# ---------------------------------------------------------------------------

def test_account_snapshot_only_takes_account_keys():
    snap = account_snapshot(fake_prefs())
    assert set(snap) <= set(ACCOUNT_KEYS)
    assert snap["flutter.uid"] == "100001"
    assert "flutter.machine_id" not in snap


def test_device_snapshot_takes_device_keys():
    snap = device_snapshot(fake_prefs())
    assert snap["flutter.machine_id"] == DEVICE_MACHINE_ID
    assert set(snap) <= set(DEVICE_KEYS)


# ---------------------------------------------------------------------------
# 合并逻辑(核心)
# ---------------------------------------------------------------------------

def test_merge_switches_account_fields():
    cur = fake_prefs("100001", "13800000001")
    target = account_snapshot(fake_prefs("200002", "13900000002"))
    merged = merge_account_into_prefs(cur, target)

    assert merged["flutter.uid"] == "200002"
    assert merged["flutter.userphone"] == "13900000002"
    assert merged["flutter.token"] == "FAKE.JWT.FOR.200002"


def test_merge_never_touches_device_identity():
    """machine_id / 渠道 / ocpc 必须原样保留——这是最关键的不变式。"""
    cur = fake_prefs("100001")
    target = account_snapshot(fake_prefs("200002"))
    merged = merge_account_into_prefs(cur, target)

    assert merged["flutter.machine_id"] == DEVICE_MACHINE_ID
    assert merged["flutter.setup_channel"] == "bing-pc"
    assert merged["flutter.ocpc"] == cur["flutter.ocpc"]


def test_merge_preserves_local_only_state():
    """画质、测速冷却、引导进度不该被账号切换带走。"""
    cur = fake_prefs("100001")
    target = account_snapshot(fake_prefs("200002"))
    merged = merge_account_into_prefs(cur, target)

    assert merged["flutter.qualitys"] == cur["flutter.qualitys"]
    assert merged["flutter.speed_test_cooldown_by_uid"] == cur["flutter.speed_test_cooldown_by_uid"]
    assert merged["flutter.guide_device_100001_charge"] == DEVICE_MACHINE_ID
    assert merged["flutter.guide_device_100002_charge"] == DEVICE_MACHINE_ID


def test_merge_keeps_key_count_stable():
    """合并不该凭空增删键,否则可能破坏 Flutter 侧预期。"""
    cur = fake_prefs("100001")
    target = account_snapshot(fake_prefs("200002"))
    merged = merge_account_into_prefs(cur, target)
    assert set(merged) == set(cur)


def test_merge_carries_zone_with_account():
    """选区跟着账号走,否则切完还停在上一个号的区。"""
    cur = fake_prefs("100001")
    other = fake_prefs("200002")
    other["flutter.lastFlowZone"] = 7
    other["flutter.lastFlowZoneName"] = "江苏2区(标配)"

    merged = merge_account_into_prefs(cur, account_snapshot(other))
    assert merged["flutter.lastFlowZone"] == 7
    assert merged["flutter.lastFlowZoneName"] == "江苏2区(标配)"


def test_merge_fills_missing_device_key_from_archive():
    """当前 prefs 缺设备键时,才允许用档案兜底补齐。"""
    cur = fake_prefs("100001")
    del cur["flutter.machine_id"]
    merged = merge_account_into_prefs(
        cur,
        account_snapshot(fake_prefs("200002")),
        device={"flutter.machine_id": "from-archive"},
    )
    assert merged["flutter.machine_id"] == "from-archive"


def test_merge_does_not_overwrite_present_device_key():
    """当前 prefs 有设备键时,档案里的值不能覆盖它。"""
    cur = fake_prefs("100001")
    merged = merge_account_into_prefs(
        cur,
        account_snapshot(fake_prefs("200002")),
        device={"flutter.machine_id": "should-not-win"},
    )
    assert merged["flutter.machine_id"] == DEVICE_MACHINE_ID


def test_merge_does_not_mutate_inputs():
    cur = fake_prefs("100001")
    target = account_snapshot(fake_prefs("200002"))
    cur_copy = json.loads(json.dumps(cur))
    merge_account_into_prefs(cur, target)
    assert cur == cur_copy


# ---------------------------------------------------------------------------
# 档案读写
# ---------------------------------------------------------------------------

def test_account_from_prefs_roundtrip(tmp_path: Path):
    acc = Account.from_prefs(fake_prefs("300003", "13700000003"), note="小号")
    store = AccountStore(tmp_path / "accounts")
    store.save(acc)

    loaded = store.load("300003")
    assert loaded.uid == "300003"
    assert loaded.phone == "13700000003"
    assert loaded.note == "小号"
    assert loaded.account["flutter.token"] == "FAKE.JWT.FOR.300003"
    assert loaded.device["flutter.machine_id"] == DEVICE_MACHINE_ID


def test_account_from_prefs_rejects_logged_out():
    prefs = fake_prefs()
    prefs["flutter.uid"] = 0
    with pytest.raises(ValueError, match="没有 uid"):
        Account.from_prefs(prefs)


def test_store_list_is_sorted_and_skips_corrupt(tmp_path: Path):
    store = AccountStore(tmp_path / "accounts")
    store.save(Account.from_prefs(fake_prefs("1", "13900000000")))
    store.save(Account.from_prefs(fake_prefs("2", "13700000000")))
    (store.root / "broken.json").write_text("{not json", encoding="utf-8")

    listed = store.list()
    assert [a.phone for a in listed] == ["13700000000", "13900000000"]


def test_store_find_by_phone(tmp_path: Path):
    store = AccountStore(tmp_path / "accounts")
    store.save(Account.from_prefs(fake_prefs("1", "13900000000")))
    assert store.find_by_phone("13900000000") is not None
    assert store.find_by_phone("10000000000") is None


def test_store_delete(tmp_path: Path):
    store = AccountStore(tmp_path / "accounts")
    store.save(Account.from_prefs(fake_prefs("1", "13900000000")))
    assert store.delete("1") is True
    assert store.delete("1") is False


def test_duration_and_coin_text():
    acc = Account(uid="1", phone="139", duration_minutes=30, coin=0.0)
    assert acc.duration_text() == "30 分钟"
    assert acc.coin_text() == "0.00"

    unknown = Account(uid="2")
    assert unknown.duration_text() == "未知"
    assert unknown.coin_text() == "未知"


# ---------------------------------------------------------------------------
# 文件读写
# ---------------------------------------------------------------------------

def test_read_prefs_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        read_prefs(tmp_path / "nope.json")


def test_read_prefs_bad_json(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError, match="合法 JSON"):
        read_prefs(p)


def test_atomic_write_replaces_content(prefs_file: Path):
    write_prefs(prefs_file, {"flutter.uid": "999"}, backup=False)
    assert read_prefs(prefs_file) == {"flutter.uid": "999"}


def test_atomic_write_leaves_no_temp_files(prefs_file: Path):
    write_prefs(prefs_file, fake_prefs("777"), backup=False)
    leftovers = [p.name for p in prefs_file.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_write_creates_backup(prefs_file: Path):
    original = read_prefs(prefs_file)
    bak = write_prefs(prefs_file, {"flutter.uid": "5"}, backup=True)

    assert bak is not None and bak.exists()
    assert json.loads(bak.read_text(encoding="utf-8")) == original


def test_switch_account_on_disk(prefs_file: Path):
    """端到端:切到另一个账号后,设备身份保留、账号字段更新、有备份。"""
    target = Account.from_prefs(fake_prefs("400004", "13600000004"))
    merged = switch_account_on_disk(prefs_file, target)

    on_disk = read_prefs(prefs_file)
    assert on_disk == merged
    assert on_disk["flutter.uid"] == "400004"
    assert on_disk["flutter.userphone"] == "13600000004"
    # 设备身份不能变
    assert on_disk["flutter.machine_id"] == DEVICE_MACHINE_ID
    assert on_disk["flutter.setup_channel"] == "bing-pc"
    # 备份存在
    assert prefs_file.with_suffix(".json.bak").exists()


def test_switch_preserves_unicode(prefs_file: Path):
    """确保中文选区名不会因为编码问题损坏。"""
    target = Account.from_prefs(fake_prefs("500005", "13500000005"))
    switch_account_on_disk(prefs_file, target)
    on_disk = read_prefs(prefs_file)
    assert on_disk["flutter.lastFlowZoneName"] == "广东2区(标配)"
