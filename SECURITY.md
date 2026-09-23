# 安全说明

本工具处理的是**真实登录凭据**,请务必读完本文再使用或分发。

## 凭据落在哪里

| 路径 | 内容 | 敏感度 |
|---|---|---|
| `accounts/*.json` | token、accessKey、machine_id、手机号、uid | **极高**,等于账号 |
| `%APPDATA%\Panghebox\panghebox\shared_preferences.json` | 同上(客户端自己的存储) | **极高** |
| `accounts/*.json.bak` | 切换前的旧凭据 | **极高** |
| `%APPDATA%\...\shared_preferences.json.bak` | 客户端 prefs 的备份 | **极高** |
| `%APPDATA%\...\wechat_webview2\` | 微信登录会话(WebView2) | **极高** |
| **客户端日志** `log/nika-*-log.txt` | **明文 token、手机号、accessKey** | **极高** |

全部是**明文**。本工具**不做加密存储**——这是刻意的取舍:任何本地加密方案
的密钥同样得放在本地,对能读到文件的攻击者不构成实质阻碍,反而制造「已加密」
的错觉。请用磁盘加密(BitLocker)和文件权限来保护,而不是依赖本工具。

### ⚠️ 客户端日志是最容易被忽略的泄露面

经实测,客户端日志 `log/nika-*.txt` 会把**完整 JWT token、明文手机号、
accessKey** 直接写进去。例如:

```
[api_trace] phase=REQUEST_HEADERS ... data={
  "Authorization":"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9....",
  "Machineid":"<uuid>","Accesskey":"<uuid>","Platform":"window"}
tryLogin ------> : {uid: ..., phone: 138xxxxxxxx, token: eyJ...}
```

所以**排查问题时不要直接把日志贴到公开 issue 里**。如需提交日志,请先删除
含 `Authorization` / `Accesskey` / `token` / `phone` 的行。

## 分发与开源注意事项

如果你要 fork 或分发本工具:

1. **绝不要提交 `accounts/` 目录。** 已在 `.gitignore`,但请务必确认:
   `git status --ignored` 与 `git ls-files | grep accounts`。
2. **提交前跑自检:**
   ```bash
   python scripts/check_leaks.py            # 扫描工作区
   python scripts/check_leaks.py --staged   # 只扫暂存区
   ```
   它会检出三段式 JWT、accessKey UUID、手机号、含值形态的 `acceessKey`
   等模式。**注意:它是启发式扫描,不是保证**;测试文件里的占位值会被放行,
   所以仍需人工过一眼 diff。
3. **不要打包日志。** 若你的工作流会打包安装目录,记得排除 `log/`。
4. **README / 截图里不要出现真实手机号、uid、token。** 截图打码时注意
   账号面板会同时露出「手机号 + ID」。

## 权限建议

只允许你自己的用户账户读取账号目录:

```powershell
# Windows: 移除继承,仅当前用户可读写
icacls "D:\code\Ai\panghebox-multi\accounts" /inheritance:r /grant:r "$env:USERNAME:(OI)(CI)F"
```

## 服务端侧说明

- 接口**没有请求签名**,认证仅靠 bearer JWT + 设备级 accessKey。这意味着
  拿到 token 即可在该设备上冒充登录,请像保护密码一样保护它。
- token 是 HS256 签名,**无法离线伪造或延长**;过期后必须重新登录。
- 本工具不绕过任何验证码、风控或付费限制。

## 报告问题

如果你在本项目中发现凭据泄露或安全缺陷,请**不要**开公开 issue。
改用仓库的 Security Advisories(私密报告)或直接联系维护者。
