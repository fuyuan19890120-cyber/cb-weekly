# 可转债双低 · 周度自动化

每周五 13:00 自动筛选可转债双低组合（排雷版），飞书推送名单；GitHub Pages 面板追踪持仓收益与溢价率中枢。

> 策略：双低 = 现价 + 转股溢价率百分点。七重排雷 + 强赎三道防线。取最低 20 只等权，周五调仓，周度轮动。回测：样本内 +35.5% / 样本外 +26.2%。

## 架构

```
每周五 13:00 (北京时间)    GitHub Actions 自动执行 scripts/weekly_run.py
    ↓                       拉取实时快照 → 结算上周 → 排雷选券 → 写回 data/
    ↓
├─ GitHub Pages 面板   读取 data/ JSON → 持仓表/净值曲线/溢价率中枢
├─ 📱 飞书卡片通知    20只名单 + 代码 + 调仓 + 强赎预警 → 手机推送
└─ Redis IPC → Windows QMT 自动执行
```

## 面板地址

部署后：`https://<你的用户名>.github.io/cb-weekly/`

## 📱 飞书通知（每周手机推送）  ← 新增

### 1. 创建飞书机器人
- 打开飞书 → 群聊 → 设置 → 机器人 → 添加自定义机器人
- 复制 Webhook 地址（以 `https://open.feishu.cn/open-apis/bot/v2/hook/` 开头）

### 2. 配置 Secret
GitHub → Settings → Secrets and variables → Actions → 新建 `CB_FEISHU_WEBHOOK`，粘贴 Webhook 地址。

### 3. 效果
每次选券完成后，飞书会推送一张卡片，包含：
- 🔴🟡🟢 溢价率信号灯 + 中枢分位
- 上期收益 / 净值
- 20 只持仓名单（双低排序）
- 🆕 新入选 / 🚫 调出 变动提醒

## Redis IPC 部署 (Mac 选券 → Windows QMT 执行)

### 前提

- **Redis 服务**一台，两台电脑都能访问（推荐 [Redis Cloud](https://redis.com/try-free/) 免费 30MB，或同局域网自建）
- Windows 上已安装 MiniQMT + xtquant Python 包（随 QMT 附带）

### 1. 配置 Redis 连接

两台电脑都设置环境变量（二选一）：

```bash
# 方式 A: 完整 URL
export CB_REDIS_URL="redis://default:your-password@host.redislabs.com:12345/0"

# 方式 B: 分开设置
export CB_REDIS_HOST=host.redislabs.com
export CB_REDIS_PORT=12345
export CB_REDIS_PASSWORD=your-password
```

### 2. Mac 端：选券 + 推送

```bash
pip install redis>=5.0
python scripts/weekly_run.py          # 选券并自动推 Redis
# 或已有 holdings.json 后单独推送:
python scripts/signal_publish.py
```

GitHub Actions 自动运行 `weekly_run.py` 时，只要 Secrets 里配了 `CB_REDIS_URL`，推送全自动。

### 3. Windows 端：接收 + 执行

```bash
pip install redis>=5.0
# 先 dry-run 查看计划
python scripts/qmt_redis_daemon.py --once --dry-run

# 确认执行
python scripts/qmt_redis_daemon.py --once --confirm --capital 200000

# 常驻模式 (Pub/Sub 实时监听)
python scripts/qmt_redis_daemon.py --confirm --capital 200000
```

### 数据流

```
Mac (选券)                      Redis                         Windows (执行)
─────────                      ─────                         ─────────
weekly_run.py 完成
  │
  └→ SET cb:signal:latest  ──→  信号持久化
     PUBLISH cb:signal:update ──→ 频道通知  ──→  qmt_redis_daemon.py 收到
                                                      │
                                                      ├─ 对比持仓
                                                      ├─ 生成调仓计划
                                                      ├─ 卖出不在名单的
                                                      ├─ 买入新入选的
                                                      └─ 写入 last_signal.txt (去重)
```

## 手动本地运行

```bash
pip install -r requirements.txt
python scripts/weekly_run.py
```
