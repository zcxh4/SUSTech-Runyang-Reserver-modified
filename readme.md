# SUSTech Venue Reserver

南方科技大学场馆空位扫描与辅助预约工具。

项目会自动扫描、刷新、选择场地和时段，并在官方预约页面中自动填写和点击“预约”。旋转验证码仍由用户在可视浏览器中人工完成；验证通过后，官方前端会使用验证码服务返回的 `id` 自动提交订单。

> 当前实现依据 2026-09-09 的生产前端。请合理设置刷新间隔并遵守场馆预约规则。

## 当前流程

```text
读取本地 token / wxOpenid
  -> 按配置顺序处理目标时间窗口，并行扫描当前窗口的候选场地
  -> 当前窗口中最先确认符合连续空闲规则的场地直接胜出
  -> 打开官方场地/日期页面
  -> 自动选择首尾时段、填写手机号和人数、点击预约
  -> 用户人工完成官方 ROTATE 验证码
  -> 官方前端携带短期 id 调用 saveOrder
```

项目不会读取、识别或计算验证码图片，也不会构造验证码轨迹。

## 环境要求

- Python 3.10 或更高版本
- Microsoft Edge、Google Chrome，或 Playwright Chromium
- 一个当前有效的企业微信预约系统登录态

安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config.example.json config.local.json
```

Windows 默认使用已安装的 Microsoft Edge，因此通常不需要下载额外浏览器。若没有 Edge/Chrome，可执行：

```powershell
python -m playwright install chromium
```

然后把配置中的 `browser.channel` 改成 `null`。

## 获取登录信息

当前预约页面的 OAuth 使用 `scope=snsapi_base`，生产前端没有传递 `agentid`，因此不需要寻找或猜测 `agentid`。普通浏览器仍不能代替企业微信身份环境；请先在企业微信中正常进入一次场馆预约页面。

在企业微信开发者工具中打开预约页面的 Web 检查器 (下载`devtools_resources.pak`并放在企业微信的安装目录里，Ctrl + Alt + Shift + D进入调试模式)，然后进入 Application/Storage -> Local Storage。当前前端通过 Vuex 持久化用户对象，默认键通常是 `vuex`。也可以在 Console 中仅在自己的设备上查看：

```javascript
JSON.parse(localStorage.getItem("vuex")).user
```

将结果中的字段写入 `config.local.json` (自己copy一份config.example.json再改名即可)：

- `wxOpenid` -> `user_id`
- `token` -> `token`

这两个字段都是登录凭据，不要截图、提交到 Git 或发送给他人。若页面或脚本提示鉴权/认证失败，需要从企业微信页面重新获取。

## 配置

推荐使用被 `.gitignore` 忽略的 `config.local.json`。程序未显式传入路径时，会优先读取它；不存在时才兼容读取旧的 `config.json`。

主要字段：

- `start_time` / `end_time`：一一对应的目标时间，按数组顺序决定优先级。每一对起止时间表示一个扫描窗口，时间必须在同一天且秒为 `00`。
- `ground_url`：本地场地编号到服务器场地 ID 的映射。
- `ground_name`：用于终端显示的名称。
- `ground_priority`：仅为兼容旧配置而保留；并行扫描不再依据该字段决定场地优先级。
- `student_tel`：官方页面中的联系电话。
- `user_num`：使用人数。
- `title`：仅部分场地要求的用途标题，可不填，默认使用“场地使用”。
- `scan.interval_seconds`：两轮扫描间隔。建议不少于 5 秒，程序最低限制为 1 秒。
- `scan.jitter_seconds`：附加随机延迟，避免形成严格固定频率。
- `scan.max_rounds`：`0` 表示持续扫描，正整数表示最多扫描轮数。
- `scan.max_concurrent_requests`：当前时间窗口同时发出的最大场地查询数，默认 `3`。命中空位后不会等待其他场地查询完成。
- `scan.selection_mode`：`exact` 要求扫描窗口内所有基础时段都连续空闲，并提交完整窗口；`longest_contiguous` 会选择窗口内最长的连续空闲子区间，长度相同时优先较早的子区间。
- `scan.min_duration_minutes`：`longest_contiguous` 模式接受的最短连续空闲时长，必须大于 `0`；`exact` 模式不使用该限制。
- `scan.third_day_release_time`：第三天场次在提前两天的本地放出时间，格式为 `HH:MM`，当前默认 `20:00`。
- `scan.release_grace_seconds`：放出时间后的缓冲秒数，默认 `3`，用于避开客户端与服务器时钟或数据切换的瞬时偏差。

对于第三天及更远日期，程序会根据“目标日期减两天”的 `third_day_release_time` 计算可查询时刻；到达该时刻前不会向日程接口发送请求，只会保持本地轮询。到时后若接口响应正常、但目标日期仍未出现在 `configList` 中，仍会将其视为场次尚未放出并继续轮询。`--once` 仍然只检查一轮。

时间窗口优先级仍严格按照 `start_time` / `end_time` 的数组顺序执行。当前窗口的所有场地均确认没有候选后，程序才会检查下一个窗口；同一窗口内则由最先确认空闲的场地胜出，不等待原 `ground_priority` 中排在前面的慢请求。

- `browser.channel`：默认 `msedge`，也可以设为 `chrome`；使用 Playwright Chromium 时设为 `null`。
- `browser.captcha_timeout_seconds`：等待人工完成验证码的最长时间。

场地 ID 可能随系统配置变化。如果官方页面能显示场地、脚本却持续报告缺少日期或场地数据，应在 Network 中查看当前 `groundId` 并更新映射。

## 使用

持续扫描，命中后进入可视浏览器流程：

```powershell
python src/main.py --config config.local.json
```

只检查一轮：

```powershell
python src/main.py --config config.local.json --once
```

只验证扫描逻辑，不打开浏览器：

```powershell
python src/main.py --config config.local.json --once --dry-run
```

浏览器弹出后不要关闭窗口。程序会预选时段并打开官方旋转验证码；人工完成后，页面自动显示预约成功或失败，程序随后输出结果和订单号。

如果扫描命中的时段在浏览器打开期间被他人占用，程序会关闭该窗口并恢复扫描。

## 与旧版的差异

- 企业微信 OAuth 不再依赖 `agentid`。
- 验证码已经从 AJ-Captcha `blockPuzzle` 迁移到 TianAi Captcha `ROTATE`。
- 旧的 `/api/captcha/get`、`pointJson`、AES 和 `captchaVerification` 流程已经停用。
- 新版预约数据使用官方验证码成功后返回的短期 `id`。
- `src/captchaVerification.py` 只保留兼容提示，不再包含自动验证码算法。
- 配置路径不再依赖启动时的当前目录。

## 测试

```powershell
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

单元测试覆盖连续空闲时间块、已占用时间块和时段缺口。真实提交必须使用你自己的有效登录态和人工验证码，因此不会出现在自动测试中。
