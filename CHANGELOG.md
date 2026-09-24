# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式。

## [0.2.0] - 2026-09-24

### 新增
- **免短信续签**:接入 `checklogin` 接口(body 仅需 `access_key + uid`),
  token 过期(服务端约 10 小时)不再需要重新短信登录;`refresh` / `signin` /
  `switch` 均自动预刷新,档案具备自愈能力
- **`clear` 子命令**:清空客户端登录态用于登录新账号,支持 `--launch`
  自动启动;顺带把"退出客户端→备份→清键"固化为标准流程
- 账号档案文件名改用**手机号**(旧 uid 命名档案保存时自动迁移,不留重复)
- `checklogin` 响应带回 `duration`/`coin`,`refresh` 一并更新,绕开
  服务端长期 500 的 `getuserinfo`

### 修复
- **清理登录态导致客户端转圈**:改为**删除键**而非置空。实测把整数型键
  写成 null 会触发客户端 `checkSharedPreferences` 的类型转换崩溃
  (`type 'Null' is not a subtype`,单会话 40+ 处),首页初始化被打断;
  键缺失才是其完善处理的"新装首次启动"路径
- 启动客户端遇 `WinError 740`(exe 要求提升)时自动回退 ShellExecute,
  正常弹出 UAC
- token 失效自愈的验证方式:改以 JWT `iat` 更新为准(原先比对长度/差异,
  坏 token 同为 192 字符,造成已修复的误判)
- 签到"今日已签"判定中写死日期导致跨天测试失败的问题

### 变更
- `machine_id` 从"设备级永不覆盖"改为**随账号走**:对照实验确认它不参与
  鉴权(换随机 UUID 也能调用),但与客户端本地引导进度绑定,每号独立可避免
  引导状态串味;`uid` 同样确认不参与鉴权(以 token 为准)
- `checkinprize` 参数确认为仅 `prize_id`(不传 HTTP 500);二进制常量池里
  相邻的 `area_tag_id` 实测非必需,弃用

## [0.1.0] - 2026-09-23

### 首个版本
- 多账号切换:白名单按键合并 prefs,保留渠道/本地状态;切换前自动退出
  客户端(SharedPreferences 退出时整体回写),原子写入 + 自动备份
- 每日签到:`getcheckinlist` / `checkinprize`,支持 `--all` 批量
- 账号档案:本地 JSON 存储(含 token/accessKey/machine_id,gitignore 挡护)
- 凭据泄露自检:`scripts/check_leaks.py`(JWT/accessKey UUID/手机号等模式)
- 逆向结论:接口无请求签名(6 个固定头即完成认证);切换本质是换 token
