# EVE 钱包查询工具（MySQL 多角色版）

通过 EVE Online 官方 ESI API 获取角色的**钱包余额**和**余额变动流水**，数据持久化到 **MySQL**，支持**多角色**授权与管理。

## 功能

- 🔐 EVE SSO OAuth2 授权（授权码 + PKCE，token 自动刷新）
- 👥 **多角色支持**：为多个角色授权，按角色分别存储 token
- 🗄️ **MySQL 持久化**：角色、token、钱包流水、余额快照全部入库
- 💰 查询角色钱包当前余额（并记录余额历史快照）
- 📒 查询钱包变动流水（journal），包含时间 / 变动金额 / 变动后余额 / 描述
- 📊 自动统计总收入、总支出、税费与净变动

## 环境要求

- Python 3.8+
- MySQL / MariaDB 数据库
- 一个 [EVE Developers](https://developers.eveonline.com) 应用
  - **Client ID** 与 **Secret Key**
  - Callback URL 注册为你的公网地址（本项目使用 `http://YOUR_HOST:8000/callback/`）
  - 权限范围（Scopes）：`esi-wallet.read_character_wallet.v1`

## 安装与配置

```bash
cd eve_esi
pip install -r requirements.txt   # requests + pymysql + matplotlib

# 创建数据库与用户（示例）
mysql -e "CREATE DATABASE eve_esi CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mysql -e "CREATE USER 'eve_esi'@'localhost' IDENTIFIED BY '你的密码';
          GRANT ALL ON eve_esi.* TO 'eve_esi'@'localhost';"

# 复制配置模板并填写 EVE 凭证与数据库信息
cp config.example.json config.json
```

`config.json` 需包含 `db` 字段：
```json
"db": {
  "host": "127.0.0.1",
  "port": 3306,
  "user": "eve_esi",
   "password": "你的密码",
   "database": "eve_esi"
 }
```

### 环境变量覆盖密钥（可选）

敏感配置（client_secret、DB 密码、OneBot token 等）可通过环境变量覆盖
`config.json` 中的值，避免密钥常驻明文文件（环境变量优先）：

| 环境变量 | 覆盖字段 |
|---|---|
| `EVE_CLIENT_ID` / `EVE_CLIENT_SECRET` / `EVE_CALLBACK_URL` | 顶层 OAuth 配置 |
| `EVE_DB_HOST` / `EVE_DB_PORT` / `EVE_DB_USER` / `EVE_DB_PASSWORD` / `EVE_DB_NAME` | `db.*` |
| `EVE_PUSH_ACCESS_TOKEN` | `push.access_token` |
| `EVE_USER_AGENT` | `user_agent` |

## 使用方法

```bash
# 初始化数据库表结构（首次）
python main.py --init-db

# 新增一个角色的授权（会弹出浏览器完成 EVE 登录）
python main.py --add-account

# 列出所有已授权角色
python main.py --list

# 查询所有角色的余额与流水
python main.py

# 查询指定角色（按名称或 ID）
python main.py --char chuxins1
python main.py --char 2124544250 --balance

# 仅查询余额 / 查询最近 100 条流水 / 同步并查看市场交易详情
python main.py --balance
python main.py --journal 100
python main.py --market-transactions

# 删除角色及其数据 / 清除角色 token 重新授权
python main.py --remove chuxins1
python main.py --reset-token chuxins1

# 从旧版 token.json 迁移单角色（v1 → v2 升级用）
python main.py --migrate
```

## 定时自动查询（每 2 分钟）

自动采集所有角色的钱包余额、流水和市场交易详情并写入数据库，同时**自动重新生成 HTML 报告**（日志写入 `auto_query.log`）：

```bash
# 后台启动（推荐，每 2 分钟一次）
cd eve_esi
nohup python3 -u auto_query.py --interval 120 > auto_query.log 2>&1 &

# 查看实时日志
tail -f auto_query.log

# 停止定时任务
pkill -f auto_query.py
```

### 数据自动清理（每日一次）

- **余额快照降采样**：最近 7 天完整保留，更早的历史每天只保留最后一条
  （余额每 2 分钟记录一次，不清理一年会积累几十万行）；
- **全宇宙 km 保留**：只保留最近 14 天（zKillboard 每天约 1.1 万条）；
- 由 `auto_query.py` 每日自动执行（通过 `push_state.json` 的 `last_prune_date` 守卫），
  手动触发：`python3 -c "from auto_query import prune_data; from main import get_db, load_config; prune_data(get_db(load_config()))"`。

### 日志轮转

三个常驻进程的日志已配置 logrotate（`/etc/logrotate.d/eve_esi`，每天轮转、保留 14 份、压缩）：
`auto_query.log` / `kill_monitor.log` / `qq_auth_bot.log`。进程通过 nohup/supervisor 追加写入，
故使用 `copytruncate` 策略（rename 会让已打开的 fd 继续写旧文件）。

### 报告按需重生成

`auto_query.py` 仅在**数据发生变化**（新增流水或余额变动）时才重绘 HTML 报告；
数据未变时每 30 轮（约 1 小时）兜底刷新一次，避免每 2 分钟调用 matplotlib 全量重绘。

## 图表化展示余额历史

基于 `wallet_balance` 表的历史快照生成折线图（PNG）：

```bash
python plot_balance.py                          # 所有角色，最近 200 条快照
python plot_balance.py --char chuxins1          # 指定角色
python plot_balance.py --limit 500 -o bal.png   # 最近 500 条，自定义文件名
```

默认输出文件为 `balance_history.png`，可在服务器上查看或用 VS Code 打开。

## HTML 报告

生成自包含的 HTML 可视化报告（内嵌余额趋势图 + 收支汇总 + 流水明细表，流水描述已翻译为中文）：

```bash
python report.py                          # 所有角色，生成 report.html
python report.py --char chuxins1          # 指定角色
python report.py --limit 500              # 取更多快照
python report.py -o my_report.html        # 自定义输出文件
```

在服务器上提供报告（可选）：
```bash
cd eve_esi && nohup python3 -m http.server 8081 --bind 0.0.0.0 > /tmp/http_report.log 2>&1 &
# 浏览器访问 http://YOUR_HOST:8081/report.html
```

## 推送流水到 QQ（NapCat OneBot）

通过 NapCat 的 OneBot HTTP 接口，将钱包流水推送到 QQ 私聊或群聊。

前置：NapCat 已运行且 OneBot HTTP 服务器开启（本项目配置为 `127.0.0.1:3000`）。

**自动推送（已集成到定时任务）**：`auto_query.py` 每次同步后自动检测新流水并推送到配置的目标（通过 `push_state.json` 记录最后处理的流水 ID，避免重复推送）。**自动推送只覆盖「玩家捐赠」**（描述形如 `X deposited cash into Y's account` / `Player donation`），其余类型流水自动跳过、不推送；按需查看完整流水请用 QQ 指令「流水 <角色名>」。

```bash
# 手动推送
python eve_push.py                        # 推送到 config.json 配置的目标
python eve_push.py --user 1234567         # 私聊推送
python eve_push.py --group 987654         # 群聊推送
python eve_push.py --from-db              # 从数据库读取（默认从 ESI）
python eve_push.py --dry-run              # 只预览不发送
```

推送配置（config.json 的 `push` 字段）：
```json
"push": { "target_user": 123456789, "target_group": null }
```

## QQ 机器人命令

在 QQ 中给机器人（`BOT_QQ`）发送命令，自动回复。

**支持命令：**
```
余额 <角色名>     查询角色 ISK 余额（如：余额 chuxins1）
余额             列出所有可用角色
流水 <角色名>     查询该角色最近 10 条钱包流水（如：流水 chuxins1）
装配 <角色名>     列出该角色已保存的装配方案；**列表发出后直接回复序号即可看详情**（如：装配 chuxins1 狂暴 按舰船名筛选）
图表 <角色名>     生成并推送该角色余额图表
击毁 [阈值亿|全部]  推送最近的 5 条舰船击毁（默认按 kill_monitor.min_isk 过滤，如：击毁 ｜ 击毁 5 ｜ 击毁 全部）
查价 <物品名称>*数量  查询该物品 Jita 收购/出售/中间价并推送走势图（名称支持模糊，数量可省，如：查价 三钛*1000）
批量查价 <物品名称>*数量  每行一个物品，输出所有物品的 Jita 4-4 总收购价/总出售价/总中间价（只有一行时等同「查价」）
                       支持直接粘贴表格/多列：「名称*数量」、「名称*市场分类*数量」、「名称␣␣␣␣数量␣␣␣␣-␣␣␣␣-」、「名称 数量」
添加账号          获取 EVE 账号授权链接（10 分钟内有效）
账号列表          列出所有已授权角色（别名：查看账号 / 账号）
菜单             列出所有可用指令及使用格式
```

> 输入容错：指令与参数之间可用空格或冒号等分隔符（如 `查价：三钛合金*100`、`流水：chuxins1`）；
> `物品*` 数量留空时按 1 计算（如 `艾玛穿梭机蓝图*` = 数量 1）。

**新增权限 / 重新授权**：EVE SSO 会复用你之前的授权同意记录 —— 在 `config.json` 里
新增 scope 后直接重新授权**可能不会生效**（token 中仍缺少新权限）。
此时需先在 EVE 账号设置里**撤销本应用的授权**（Third Party Applications → Revoke），
再发送「添加账号」重新授权。机器人会在授权完成时校验 JWT（`scp`）中实际授予的权限，
缺少时直接推送提示。

**运行命令服务：**
```bash
cd eve_esi
nohup python3 qq_auth_bot.py > qq_auth_bot.log 2>&1 &   # 启动（监听 127.0.0.1:8888）
pkill -f qq_auth_bot.py                            # 停止
```

原理：NapCat 通过 OneBot HTTP 上报将 QQ 消息事件 POST 到 `127.0.0.1:8888/onebot/event`，
`qq_auth_bot.py` 解析命令并从数据库快照/ESI 查询余额，通过 OneBot API 回复。

## 全宇宙高价值 km 监控（`kill_monitor.py`）

监控全宇宙被击毁的舰船，**估价 ≥ 阈值（默认 15 亿 ISK）时自动推送 km 链接**到 QQ。

### 数据源与原理（为什么必须用 zKillboard）
- ESI 没有「全宇宙 km 流」端点；单条 km 详情需要 hash，而 hash 只能从第三方拿；
- zKillboard `/api/kills/` **必须带实体过滤**（不带过滤返回
  `{"error":"Please provide an entity filter first."}`），所以按**星域**轮询 114 个星域：
  `/api/kills/regionID/{id}/`；
- 单次最多返回 200 条（`limit` 参数已被官方撤销），返回体即 ESI 格式 km + `zkb` 统计
  （`totalValue` 估价、`hash`），无需再调 ESI 取详情；
- **不要用 `pastSeconds`**：实测其索引滞后约 15 分钟（`pastSeconds/1800` 返回 0 条、
  `pastSeconds/86400` 的最新一条比无窗口查询旧），只有**不带窗口**的「最新 200 条」才新鲜；
  所以增量轮询一律用无窗口查询，靠 `killmail_id` 去重；
- zKillboard 有「冷缓存」现象：某星域久未查询时首次请求可能耗时 30～60 秒 →
  单请求读取超时 45 秒，超时的星域本轮跳过、下轮补上；
- 官方要求 UA 可联系、频率 ≤ 1 次/秒 → 星域之间默认 sleep 1 秒。

### 运行
```bash
supervisorctl status eve-kill-monitor        # 查看状态
supervisorctl restart eve-kill-monitor       # 改代码后重启
python3 kill_monitor.py --once --dry-run     # 自测（不写库不推送）
python3 kill_monitor.py --once --region-limit 3 --dry-run   # 快速自测
python3 kill_monitor.py --reset              # 重置初始化状态
```

### 初始化与推送策略
- **自动播报可由 `push_enabled` 关闭**：设为 `false` 后仅采集入库（每轮把达标记录标记为已处理，
  避免以后重新开启时补推一大批旧数据），需要时用 QQ 指令 `击毁` 按需查询最近 5 条；
- **数据起点（cutoff）**：初始化时记录 `cutoff_time = 当前时间 − lookback_hours`（存于
  `kill_monitor_state.json`），早于该时间的 km 一律丢弃 —— 因为无窗口查询对冷门星域会
  返回几个月甚至几年前的数据，不过滤会导致误推远古 km；
- 首次运行先按起点用 `pastSeconds` 拉一遍历史（该窗口数据较旧、且受 200 条上限影响只覆盖部分），
  紧接着再按无窗口拉一遍最新数据；
- 默认 `push_backfill=false`：初始化那批历史**只入库、不补推**（避免启动瞬间刷屏）；
  设为 `true` 则按 `max_push_per_cycle`（默认 30 条/轮）分多轮补推；
- 之后每 `interval_seconds`（默认 300s）扫一遍全部星域（无窗口，每星域最新 200 条），
  按 `killmail_id` 去重，进程重启不会重复推送；
- 日志中「截断星域 N 个」指这些星域返回已达 200 条上限（属正常，表示该星域还有更早的
  km 未取）；只有单星域在 10 分钟内被击毁 >200 艘时才可能漏单。

### 配置（`config.json` → `kill_monitor`）
| 键 | 默认 | 说明 |
|----|------|------|
| `min_isk` | `1500000000` | 推送阈值（15 亿 ISK），取 zKillboard `totalValue` 估价 |
| `push_enabled` | `true` | 是否自动播报；false = 只采集入库（用 QQ 指令「击毁」按需查询）|
| `interval_seconds` | `300` | 扫描周期（单轮扫描约 3～5 分钟，不建议低于 300）|
| `lookback_hours` | `24` | 初始化回溯小时数 |
| `push_backfill` | `false` | 初始化历史是否补推 |
| `max_push_per_cycle` | `30` | 每轮最多推送条数 |
| `request_interval_seconds` | `1.0` | 星域之间请求间隔（官方要求 ≤1 次/秒）|
| `target_user` / `target_group` | 取 `push` 段 | 推送目标 |

### 消息示例
```
💥 高价值舰船击毁（阈值 15.00 亿 ISK）
💰 估价：63,885,732,087 ISK（638.86 亿）
🚀 舰船：飞龙级
🏴 受击方：Huizel Tsero（Science and Trade Institute）
📍 星系：HY-RWO（30001159）｜攻击者 68 人
🕒 2026-09-20 02:08:31
🔗 https://zkillboard.com/kill/138557102/
```

### 实测流量参考（2026-09-20）
- 全宇宙 24 小时约 **1.1 万条** km 入库，其中估价 ≥15 亿的约 **230 条/天**（≥10 亿约 320 条/天）；
- 单轮扫描 114 个星域约需 **2～5 分钟**；zKillboard 入库本身有约 5～15 分钟延迟，
  因此从被击毁到收到推送通常在 **10 分钟内**。

## 数据库表

| 表 | 说明 |
|----|------|
| `characters` | 已授权角色 |
| `oauth_tokens` | 每个角色的 OAuth token（access / refresh）|
| `wallet_journal` | 钱包变动流水（按角色 + ref_id 去重）|
| `wallet_balance` | 钱包余额历史快照 |
| `wallet_transactions` | 市场交易详情（按角色 + transaction_id 去重）|
| `item_types` | EVE 物品类型表（type_id -> 名称，用于交易详情翻译）|
| `universe_killmails` | 全宇宙 km（zKillboard 轮询入库，`pushed_at` 标记是否已推送）|
| `universe_names` | ID → 名称缓存（星系/角色/军团，供 km 消息展示）|

## 输出示例

```
============================================================
角色：chuxins1 (ID: 2124544250)
当前钱包余额：1,904,579,755.75 ISK
------------------------------------------------------------
最近 10 条钱包变动流水（已持久化到 MySQL）：
时间                    变动(ISK)       变动后余额(ISK)  描述
2026-08-12 15:28:21    +53,789,000.00    1,904,579,755.76  robot xi deposited cash...
------------------------------------------------------------
收支汇总（基于上述记录）：
  记录条数：10
  总收入  ：335,572,500.00 ISK
  总支出  ：-725,010,000.00 ISK
  税费合计：0.00 ISK
  净变动  ：-389,437,500.00 ISK
------------------------------------------------------------
```

## 文件说明

| 文件 | 说明 |
|------|------|
| `main.py` | 主入口（多角色命令行交互）|
| `auth.py` | EVE SSO OAuth2 授权与 token 刷新（纯 OAuth 逻辑）|
| `db.py` | MySQL 数据库访问层（角色 / token / 流水 / 余额）|
| `esi_client.py` | ESI API 客户端（余额 / journal 查询与统计 / 市场行情）|
| `market_price.py` | Jita 行情查询（名称解析 / 收购价-出售价-中间价 / 走势图）|
| `fittings.py` | 角色装配方案查询（ESI fittings，槽位分组 + 参考估价）|
| `kill_monitor.py` | 全宇宙高价值 km 监控（zKillboard 星域轮询 + 阈值推送）|
| `build_item_index.py` | 从官方 SDE 构建本地物品名索引（供模糊查询）|
| `charts.py` | matplotlib 中文字体等公共辅助 |
| `schema.sql` | 数据库表结构（程序启动时自动初始化）|
| `config.example.json` | 配置模板 |

## 物品名模糊查询与价格兜底

「查价」在精确匹配失败时会回退到本地物品索引做模糊匹配（中英文均可）。
索引来自 EVE 官方 SDE 的 `types.jsonl`，取其中「已发布且在市场分类下」的约 1.9 万个物品：

```bash
python3 build_item_index.py            # 首次构建（下载 SDE，约 95MB）；之后默认复用本地 sde.zip
python3 build_item_index.py --stats    # 查看索引统计与抽样匹配
python3 build_item_index.py --reset    # 清空后重建（用于修正已写入的名称）
```

游戏更新后重新跑一次即可刷新索引；`sde.zip` 已在 `.gitignore` 中。

**无挂单物品的兜底**：PLEX（伊甸币）这类物品在 ESI 中**没有任何星域挂单与历史行情**，
此时会退回 `GET /v1/markets/prices/` 的全局参考均价，并在消息中标注来源。
该端点单次约 5~25 秒、约 1MB，因此改为缓存：

```bash
python3 market_price.py --refresh-prices   # 手动刷新参考价缓存
```

缓存写在 `market_prices.json`（已 gitignore）；`qq_auth_bot.py` 有后台线程每小时检查、
超过 24 小时自动刷新，指令本身始终秒回。

## 角色装配查询（`装配` 指令）

需要额外授权 `esi-fittings.read_fittings.v1`（已在 `config.example.json` 中列出）。

```bash
python3 fittings.py chuxins1            # 列出该角色全部装配
python3 fittings.py chuxins1 3          # 查看第 3 套详情
python3 fittings.py chuxins1 狂暴         # 按舰船名查看（列出狂暴级的所有装配）
```

机器人端交互：发送 `装配 <角色名>` 得到列表后，**直接回复序号**（如 `29`，或 `装配 29`）即可查看该套详情，
无需再带角色名。候选列表里的序号是同一套全局序号，可以直接使用。

**详情输出为 EFT 格式**（与游戏内「导出装配」一致，可直接粘贴进游戏导入或分享），例如：

```
[审判者级, WF 三开通刷审判 升级版]
暗影天蛇热能涂层
反应式装甲加固器
多谱式涂层 II
散热槽 II
小型装甲维修器 II

巴尔默序列紧凑型索敌扰断器 I
…
```

可选后缀：

| 写法 | 输出 |
|---|---|
| `装配 chuxins1 24` | **EFT（中文名）**，默认 |
| `装配 chuxins1 24 英文` | EFT（英文名，部分客户端只能识别英文） |
| `装配 chuxins1 24 详情` | 旧的分槽位视图（含装配 ID 与全局均价估价） |

EFT 正文下方会**空两行**再接一条横线与补充信息（复制进游戏时请**只复制横线以上部分**）：

```
…纳米体修复粘合剂 x134


────────────────
🆔 装配ID 123142393
💰 参考估价：13,651,614.00 ISK（Jita 4-4 出售价，15/15 种物品已定价，不含舰船）
```

参考估价的算法：对装配内**每个物品种类**取 **Jita 4-4 最低卖单价**（与「查价」同一口径），
乘以数量后汇总；**包含舰船船体**（按 1 艘计）。
首次查看一套约需 2~3 秒（并发查询 + 进程内缓存 10 分钟），重复查看同一套几乎不产生请求。

序号的有效期（`FITTING_CONTEXT_TTL` / `FITTING_HINT_TTL`，均在 `qq_auth_bot.py` 顶部）：

| 距上次列表的时间 | 输入序号的行为 |
|---|---|
| ≤ 30 秒 | 正常打开该套详情 |
| 30 ~ 60 秒 | 不打开详情，提示重新发送「装配 <角色名>」 |
| > 60 秒 | **不提示，直接按序号打开详情** |
| 从未列过表 | 纯数字静默；`装配 <序号>` 会提示先查列表 |

另外：若上次展示的是**候选列表**（例如按船名筛选出的 `29、30`），则只接受候选范围内的序号，
输入其它序号会提示可输入的范围（避免误开到无关的装配）。

**重要限制：ESI 只能读取「个人保存的装配」，「军团共享装配」没有任何接口能获取。**
2026-09-20 实测：游戏内「我的装配」34 套 = 接口返回 34 套，而「军团装配」474 套完全不在返回中；
第三方工具同样依赖 ESI，客户端也不在本地落地这些数据。
想把军团装配纳入查询，只能在游戏内先**「复制到我的装配」**，之后它就会出现在个人装配列表里。

物品名解析全部走本地 `item_types` 索引（约 1.95 万条），不产生额外请求；详情末尾的
「参考估价」用已缓存的 ESI 全局均价计算，同样不发起请求（仅作参考，非 Jita 实盘价）。

> 给角色新增 scope 后重新授权**可能不生效**（详见下面的授权说明）。

## 安全提示

- OAuth token（含 refresh_token）保存在数据库，请勿泄露数据库凭据。
- EVE 官方要求设置合理的 `User-Agent`，建议带上联系方式，避免被 ESI 限流
  （当前 `config.json` 中仍是占位邮箱，请替换为真实联系方式）。
- 回调服务器绑定 `0.0.0.0`，请确保 EVE 回调端口仅按需对公网开放。
- `config.json`（含 Client Secret、数据库密码、OneBot access_token）与 `.report_auth`
  等敏感文件均已列入 `.gitignore`，切勿提交；本仓库代码通过 `config.json` 注入凭据，
  亦可用[环境变量覆盖密钥](#环境变量覆盖密钥可选)进一步减少明文。
- **切勿用 `python3 -m http.server --directory /root` 之类把项目/主目录整体暴露到公网**，
  那会直接泄露 `config.json`、`.ssh`、`~/.git-credentials` 等。公网报告请走 nginx
  （本站已配置：HTTPS + Basic Auth，仅暴露 `/var/www/report/report.html` 单一文件）。
- `report.html` 含角色余额等隐私信息，公网暴露时请加 Basic Auth 或放在反向代理之后。
- OneBot 事件接收端点（127.0.0.1:8888）会校验 `push.access_token`：
  要求上报请求带合法的 `X-Signature: sha1=<HMAC-SHA1(token, body)>`（OneBot v11 标准，
  NapCat/SnowLuma 自动生成），也兼容 `Authorization: Bearer <token>`；签名不符返回 403。
  未配置 token 时保持不校验（向后兼容）。
- 若从旧版本升级，请检查 `git log` 确认历史中不含自己的密钥（如不慎提交需改写历史并轮换密钥）。

## 测试

纯逻辑单元测试（不依赖网络 / 数据库）位于 `tests/`，覆盖查询串解析、挂单价计算、
描述翻译、物品名规范化、OAuth PKCE 与 ISK 格式化等：

```bash
pip install pytest   # 或 pip install --user pytest
python -m pytest tests/ -q
```
