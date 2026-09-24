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

逆向与实测得到的关键事实(全部有客户端日志或接口实验佐证):

1. **接口没有请求签名。** 认证只靠 6 个固定请求头(`Authorization` JWT、
   `Version`、`Machineid`、`Accesskey`、`Platform`、`X-Client-Request-Id`),
   没有 HMAC / nonce / timestamp。因此不需要改动或注入客户端进程,普通
   HTTP 请求即可完成签到。
2. **`token` 是唯一身份凭据,切换的本质就是换 token。** JWT payload 内嵌
   `acceessKey`(三 e 为服务端拼写,照抄),服务端比对它与请求头 `Accesskey`
   是否一致——不一致报 `3584901`。所以 accessKey **不能伪造,也不能按账号
   分别存**,同机多号共用同一个。
3. **`uid` 与 `machine_id` 都不参与鉴权**(均经对照实验验证):
   - `uid` 只是请求参数,传错 uid 配上有效 token,服务端仍按 token 识别;
   - `machine_id` 只作请求头上报,换成随机 UUID 也能调用。但它与客户端本地
     引导进度绑定,所以**随账号走**——每个号一个独立设备身份,引导状态互不干扰。
4. **剩余时长与盒币来自登录形状的响应**(`duration` 分钟 / `coin`)。
   HTTP 层的 `getuserinfo` 长期返回 500(服务端问题),实际应使用
   `checklogin`(见下文「token 有效期与自动续签」),两者响应字段相同。

## 安装

```bash
git clone <this-repo>
cd panghebox-multi
pip install -r requirements.txt
```

要求 Python 3.10+ / Windows(客户端只有 Windows 版)。

安装为命令(可选):`pip install -e .`,之后可直接用 `panghebox`;
不安装则全程用 `python -m panghebox.cli`。

## 使用

### 添加一个账号(每个号只需手动登录一次)

```bash
python -m panghebox.cli clear --launch   # 清空客户端登录态并启动(会弹 UAC)
# → 客户端打开后登录新账号(微信扫码页可切换手机号+验证码登录)
# → 登录成功后关闭客户端窗口(不要点"退出登录",那会作废 token)
python -m panghebox.cli save --note "大号"   # 保存为档案
```

### 日常切换与签到

```bash
python -m panghebox.cli list                  # 总览(手机号/ID/时长/盒币)
python -m panghebox.cli switch 13900000002    # 切换(自动关客户端→写入→启动)
python -m panghebox.cli switch --next         # 按列表顺序循环切
python -m panghebox.cli signin                # 当前账号签到(打印 7 天进度)
python -m panghebox.cli signin --all          # 所有账号签到(token 过期自动续签)
python -m panghebox.cli refresh --all         # 只续签 token + 更新时长/盒币
```

### 全部命令

| 命令 | 作用 |
|---|---|
| `list` | 列出已保存账号(手机号 / ID / 剩余时长 / 盒币) |
| `current` | 显示客户端当前登录的账号 |
| `save [--note 备注] [--fetch]` | 把客户端当前登录态保存为档案 |
| `switch <手机号或uid>` / `switch --next` | 切换账号 |
| `signin [账号] / --all / --force / --dump-raw` | 每日签到 |
| `refresh [账号] / --all` | 续签 token 并更新时长/盒币 |
| `clear [--launch]` | 清空客户端登录态(用于登录新账号) |
| `doctor` | 环境自检(安装目录/账号文件/进程/版本) |

### 常用参数

| 参数 | 作用 |
|---|---|
| `--install-dir <路径>` | 手动指定客户端安装目录(自动探测失败时) |
| `--accounts-dir <路径>` | 指定账号档案目录 |
| `switch --no-launch` | 切完不自动启动客户端 |
| `switch --no-backup` | 不备份原 `shared_preferences.json` |
| `clear --launch` | 清理后自动启动客户端 |
| `signin --force` | 即使显示已签到也再请求一次 |
| `signin --dump-raw` | 打印接口原始响应(排障用) |

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

**这个目录已在 `.gitignore` 里,不会被提交。** 但请注意它包含**明文凭据**,
而且是可以免短信续签的长期有效凭据,详见 [SECURITY.md](SECURITY.md)。

## token 有效期与自动续签

实测服务端 token 的有效期在 **10 小时量级**(JWT 无 `exp` 字段,过期由
服务端判定,业务码 `3584901`「用户token失效」)。

客户端的"自动登录"就是 `POST /v1/nika/client/checklogin`,body 只要
`access_key + uid` 两项——**无需短信验证码**,且 Authorization 头里带过期
token 也能通过。响应即登录形状(`uid / phone / token / coin / duration`),
一并带回剩余时长与盒币。

本工具的 `refresh`、`signin` 与 `switch` 的预刷新都走这条路径:
**只要档案里有 accessKey,token 永远可以免短信续签**,过期不会让账号档案
报废。注意刷新时 Authorization 头必须传空——实测过期 token 能通过,但
格式损坏的 token 会被拒;空头对两种情况都安全。

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

- `sale_value` 是奖励分钟数,`sale_type: 101` 表示时长卡,`sale_period`
  是有效小时数(0 = 当天有效)
- `date` 为空串 = 该天未领取
- `checkinprize` 的请求参数**只有 `prize_id`**(对应目标那天的值);
  不传会直接 HTTP 500。二进制里旁边的 `area_tag_id` 实测非必需。
- 每天只能领一次,重复领返回业务码 `3584316`(该日时间无法领取奖励)。
  因此判断「今天是否已签」看的是**最后一条领取记录的 `date` 是否等于今天**,
  而不是 `checkin_count`(它区分不了"今天刚签"和"昨天签的")。

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
它不参与鉴权,但和客户端本地引导进度绑定:客户端日志里能看到
`reset cloud_game to 0 (localDevice: , currentDevice: <新UUID>)` —— 换了
machine_id 就会重置引导状态。让每个账号持有自己的 machine_id,各号在
客户端看来是不同设备,引导进度互不影响;渠道标识(`setup_channel`)和
归因标识(`ocpc`)由安装包决定,切换账号时保持不变。

**清理登录态为什么是删键而不是置空?**
实测教训:把 `flutter.uid`、`flutter.lastFlowZone` 这类整数字段写成
null,客户端启动时的 `checkSharedPreferences` 会抛
`type 'Null' is not a subtype of type 'Object'`(单次会话 40+ 处),
首页初始化被打断、一直转圈,需要重启一次才能恢复。而**键缺失**是
Flutter prefs 的「新装首次启动」路径,客户端处理完善。所以 `clear`
命令删除账号键(含 machine_id,由客户端重新生成),绝不写入 null。

**选区为什么跟着账号走?**
`lastFlowZone` / `lastFlowZoneName` 是「上次选的区」。不跟着账号切换的话,
切完号还停在上一个账号的区。

## 常见问题

**客户端一直转圈?**
v0.1.x 时代的清理方式(置空而非删键)导致,已修复。若你在用旧版本或手动
改过 prefs,关闭并重开一次客户端即可恢复(客户端会把坏值规范化重写)。

**提示 token 失效(3584901)?**
`refresh --all` 或直接 `signin --all` 即可,签到/切换前会自动免短信续签。

**时长显示"未知"或接口 500?**
HTTP 层的 `getuserinfo` 在服务端长期故障,本工具已全部改走 `checklogin`,
正常不会再出现;若出现请更新到最新版。

**启动客户端报 WinError 740?**
客户端 exe 要求管理员提升。工具已自动回退到 ShellExecute(会弹 UAC 窗口,
点"是"即可)。

**签到成功但时长没变?**
以 `refresh` 后的 `checklogin` 返回为准;客户端界面可能有缓存延迟。
另外「新人礼包-30分钟」只在账号**首次注册**时发放一次:拿注册过的老号
登录只会有签到的 10 分钟,不是工具丢了时长,也不需要任何补领操作。

## 已知限制

- **只能管理「在这台机器上登录过」的账号。** 添加新账号必须先在客户端里
  手动登录一次(需要短信验证码),让服务端把该 uid 与设备 accessKey 绑定。
- **同机多号在服务端侧可关联。** 所有账号共用设备 accessKey(嵌在 JWT 里,
  无法伪造或分离),服务端技术上能识别它们来自同一设备。`machine_id` 独立
  能降低一部分关联性,但不改变这一事实。
- **微信扫码登录的账号未验证。** 其凭据可能涉及
  `%APPDATA%\Panghebox\panghebox\wechat_webview2\`(WebView2 数据目录),
  本工具目前不处理该目录;用手机号登录的账号不受影响。

## 项目结构

```
panghebox/
├─ paths.py      # 定位安装目录 / prefs / 注册表信息
├─ store.py      # 账号档案、白名单按键合并、原子写入、clear_login_state
├─ switcher.py   # 进程检测/结束/启动(含 UAC 回退)
├─ api.py        # HTTP 客户端(checklogin/签到等,无签名,6 个固定头)
├─ signin.py     # 签到业务逻辑(实测 schema 解析)
└─ cli.py        # 命令行入口
scripts/
├─ check_leaks.py      # 提交前凭据泄露自检
├─ watch_log.py        # 从客户端日志捕获接口调用(调试用)
└─ capture_checkin.py  # mitmproxy 插件(调试用)
accounts/              # 账号档案(gitignore,绝不入库)
tests/                 # pytest 测试
```

## 开发

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q          # 90 个测试
python scripts/check_leaks.py       # 提交前凭据泄露自检
```

变更历史见 [CHANGELOG.md](CHANGELOG.md)。

## 免责声明

本项目为个人学习与自用的非官方工具,与胖盒云电脑官方无关,未获其授权或认可。
使用者需自行承担账号风险,包括但不限于因违反服务条款导致的账号受限。
请勿用于批量注册、刷量或任何商业用途。

## License

MIT,见 [LICENSE](LICENSE)。
