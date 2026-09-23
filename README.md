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
2. **`accessKey` 和 `machine_id` 是设备级凭证,同一台机器上所有账号共用。**
   所以「切换账号」只换 `uid` / `token` / `phone`,**绝不能**覆盖设备字段。
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
python -m panghebox.cli signin             # 当前账号
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

**选区为什么跟着账号走?**
`lastFlowZone` / `lastFlowZoneName` 是「上次选的区」。不跟着账号切换的话,
切完号还停在上一个账号的区。

## 已知限制

- **签到接口的响应字段名未经实测校准。** 端点
  (`getcheckinlist` / `checkinprize`) 是从客户端二进制里确认的,但 Dart
  AOT 会压缩字段名,静态读不出 schema。本工具用**多候选键名容错解析**,
  解析失败时会明确报错而不是静默给错结果。若你的环境解析失败,请用
  `signin --dump-raw` 打印原始响应,按实际字段名调整
  `panghebox/signin.py` 里的候选键名。
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
