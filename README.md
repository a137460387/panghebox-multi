# 胖盒云电脑 多账号切换 + 自动签到工具

一个非官方的本地命令行工具,用于管理胖盒云电脑(PangHeBox)客户端的多个账号:
**一键切换账号**、**查询剩余时长/盒币**、**每日自动签到领时长卡**。

```
$ panghebox list
已保存账号 (3)
   手机号          ID         剩余时长     盒币     备注
----------------------------------------------------------------------
*  138****0001    100001     30 分钟      0.00
   139****0002    100002     18 分钟      0.00
   137****0003    100003     0 分钟       0.00
* = 当前客户端登录的账号
```

---

## 它是怎么工作的

不像「多开客户端」的做法,本工具**复用客户端自身的登录态**:直接读写 Flutter
的 `shared_preferences.json`,并以纯 HTTP 客户端调用同一套服务端接口。

逆向得到的三个关键事实(全部有客户端日志佐证):

1. **接口没有请求签名。** 认证只靠 6 个固定请求头(`Authorization` JWT、
   `Version`、`Machineid`、`Accesskey`、`Platform`、`X-Client-Request-Id`),
   没有 HMAC / nonce / timestamp。因此不需要改动或注入客户端进程,普通
   HTTP 请求即可完成签到。
2. **切换的本质是换 `token`。** 实测确认(逐项验证过):
   - `token` 是唯一身份凭据。JWT payload 内嵌 `acceessKey`,服务端比对它与
     请求头 `Accesskey` 是否一致——不一致直接报 `3584901`。所以 accessKey
     **不能伪造,也不能按账号分别存**,同机多号共用同一个。
   - `uid` 只是请求参数,**不参与鉴权**。传错 uid 但配上有效 token,
     服务端仍按 token 识别身份。
   - `machine_id` 只作请求头上报,**同样不参与鉴权**(换成随机 UUID 也能
     正常调用)。但它与客户端本地引导进度绑定,所以**随账号走**——每个号
     持有自己的值,引导状态互不干扰。
3. **剩余时长与盒币来自登录响应的 `duration`(分钟)和 `coin` 字段。**

## 安装

```bash
git clone <this-repo>
cd panghebox-multi
pip install -r requirements.txt
```

要求 Python 3.10+ / Windows(客户端只有 Windows 版)。

## 使用

```bash
# 1) 先在客户端里正常登录一个账号,然后保存它
python -m panghebox.cli save --note "大号"

# 查看已保存账号(手机号 / ID / 剩余时长 / 盒币)
python -m panghebox.cli list

# 2) 切换账号:会自动关客户端 → 写入 → 重新启动
python -m panghebox.cli switch 13900000002
python -m panghebox.cli switch --next      # 按列表顺序循环切

# 3) 签到
python -m panghebox.cli signin             # 当前账号(会打印 7 天进度)
python -m panghebox.cli signin --all       # 所有已保存账号
python -m panghebox.cli signin --dump-raw  # 打印原始响应(排障用)

# 其它
python -m panghebox.cli current            # 当前登录的是谁
python -m panghebox.cli refresh --all      # 刷新时长/盒币
python -m panghebox.cli doctor             # 环境自检
```

未安装时,把 `python -m panghebox.cli` 换成 `python -m panghebox.cli` 即可;
也可以 `pip install -e .` 后用 `panghebox` 命令(见 `pyproject.toml`)。

### 常用参数

| 参数 | 作用 |
|---|---|
| `--install-dir <路径>` | 手动指定客户端安装目录(自动探测失败时) |
| `--accounts-dir <路径>` | 指定账号档案目录 |
| `switch --no-launch` | 切完不自动启动客户端 |
| `switch --no-backup` | 不备份原 `shared_preferences.json` |
| `signin --force` | 即使显示已签到也再请求一次 |

## 账号档案存在哪

默认在仓库根的 `accounts/`,一个账号一个 JSON 文件:

```
accounts/
├─ 13800000001.json
└─ 13900000002.json
```

**文件名用手机号**(手机号缺失时才回退到 uid),这样看文件列表能直接对上人。
读取时手机号和 uid 都能当 key 用,`switch 13800000001` 和 `switch <uid>`
效果一样。早期版本以 uid 命名的档案,下次保存时会自动改名为手机号,
不会留下重复文件。

**这个目录已在 `.gitignore` 里,不会被提交。** 但请注意它包含**明文凭据**
(token / accessKey / 手机号),详见 [SECURITY.md](SECURITY.md)。

## 设计上的几个关键取舍

**为什么不整文件覆盖 `shared_preferences.json`?**
该文件有 50+ 个键,其中大部分与账号无关(30 个 `guide_device_*` 引导进度、
画质、渠道、广告归因)。整文件覆盖会把当前设备的这些状态一起带过去,导致
切换后引导反复弹出、选区错乱。本工具**只替换账号白名单键**,其余原样保留。

**为什么切换前必须先关掉客户端?**
Flutter 的 `SharedPreferences` 在内存里持有完整状态,**退出时会整体回写磁盘**。
应用运行期间改文件,一关就被覆盖回去。所以顺序必须是
「检测进程 → 结束 → 等文件锁释放 → 原子写入 → 启动」。

**为什么写入要原子?**
`shared_preferences.json` 一旦写坏,Flutter 启动时解析失败可能直接丢登录态。
本工具用「同目录临时文件 → fsync → `os.replace`」,并默认先备份一份 `.bak`。

**为什么 machine_id 跟着账号走?**

实测它不参与鉴权,但它和客户端本地引导进度绑定:客户端日志里能看到
`reset cloud_game to 0 (localDevice: , currentDevice: <新UUID>)` —— 换了
machine_id 就会重置引导状态。让每个账号持有自己的 machine_id,各号在
客户端看来是不同设备,引导进度互不影响;渠道标识(`setup_channel`)和
归因标识(`ocpc`)由安装包决定,切换账号时保持不变。

**选区为什么跟着账号走?**
`lastFlowZone` / `lastFlowZoneName` 是「上次选的区」。不跟着账号切换的话,
切完号还停在上一个账号的区。

## token 有效期与自动续签

实测服务端 token 的有效期在 **10 小时量级**(无 `exp` 字段,过期由服务端判定,
表现为人报业务码 `3584316`/`3584901` 之一的"token 失效")。

好消息:客户端的"自动登录"就是 `POST /v1/nika/client/checklogin`,
body 只要 `access_key + uid` 两项——**无需短信验证码**,且 Authorization
头里带过期 token 也能通过。响应即登录响应的形状(`uid / phone / token /
coin / duration`),一并带回剩余时长与盒币。

本工具的 `refresh` 命令、`signin` 与 `switch` 的预刷新都走这条路径:
**只要档案里有 accessKey,token 永远可以免短信续签**,过期不会让账号档案报废。
注意刷新时 Authorization 头必须传空——实测过期 token 能通过,但格式损坏的
token 会被拒;空头对两种情况都安全。

## 签到接口的实测 schema

字段名已通过实测确认(非猜测)。`getcheckinlist` 无参数,返回:

```json
{
  "checkin_list": [
    {"prize_id": 100, "prize_sec_id": 1, "prize_name": "第1天签到礼包",
     "sale_info": [{"sale_name": "签到奖励-10分钟卡", "sale_type": 101,
                    "sale_value": 10, "sale_period": 0}],
     "date": "2026-09-23 17:19:21"}
  ],
  "checkin_count": 1,
  "actvity_day_count": 1
}
```

- `sale_value` 是奖励分钟数,`sale_type: 101` 表示时长卡
- `date` 为空串 = 该天未领取
- `checkinprize` 的请求参数**只有 `prize_id`**(对应目标那天的值);
  不传会直接 HTTP 500。二进制里旁边的 `area_tag_id` 实测非必需。
- 每天只能领一次,重复领返回业务码 `3584316`(该日时间无法领取奖励)。
  因此判断「今天是否已签」看的是**最后一条领取记录的 `date` 是否等于今天**,
  而不是 `checkin_count`。

## 已知限制

- **只能管理「在这台机器上登录过」的账号。** 因为 `accessKey` 是设备级凭证,
  服务端按 `(accessKey, uid)` 判定身份;从未在此设备登录过的 uid 会被拒绝。
- **无法离线续签 token。** JWT 是 HS256 签名,密钥在服务端。token 过期后
  必须回到客户端重新登录一次。
- **微信登录的账号凭据不在 prefs 里**,而在
  `%APPDATA%\Panghebox\panghebox\wechat_webview2\`(WebView2 数据目录)。
  本工具目前不处理该目录。

## 开发

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q          # 74 个测试
python scripts/check_leaks.py       # 提交前凭据泄露自检
```

## 免责声明

本项目为个人学习与自用的非官方工具,与胖盒云电脑官方无关,未获其授权或认可。
使用者需自行承担账号风险,包括但不限于因违反服务条款导致的账号受限。
请勿用于批量注册、刷量或任何商业用途。

## License

MIT,见 [LICENSE](LICENSE)。
