# -*- coding: utf-8 -*-
"""Redis IPC 共享配置 — Mac 端发布 / Windows QMT 端订阅

   连接方式 (优先级从高到低):
     1. 环境变量 CB_REDIS_URL      — redis://[:password@]host:port/db
     2. 环境变量 CB_REDIS_HOST 等  — 分别指定 host/port/password/db
     3. 默认 localhost:6379/0       — 本地开发

   Redis 数据约定:
     Key:  cb:signal:latest   (String, JSON)  — 最新一期信号
     Key:  cb:signal:log      (List, JSON)    — 历史信号追加 (最多 52 条)
     Chan: cb:signal:update   (Pub/Sub)       — 信号发布通知, message="<signal_id>"
"""
import os, json, logging
from datetime import datetime

logger = logging.getLogger("cb.redis")

# ---- 连接参数 ----
REDIS_URL = os.environ.get("CB_REDIS_URL", "")
REDIS_HOST = os.environ.get("CB_REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("CB_REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("CB_REDIS_PASSWORD", None)
REDIS_DB = int(os.environ.get("CB_REDIS_DB", "0"))

# ---- Key 命名 ----
KEY_SIGNAL = "cb:signal:latest"
KEY_LOG = "cb:signal:log"
CHAN_UPDATE = "cb:signal:update"


def get_redis():
    """延迟导入, 避免在没有 redis-py 的环境 import 就报错"""
    import redis
    if REDIS_URL:
        return redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return redis.Redis(
        host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
        password=REDIS_PASSWORD, decode_responses=True,
        socket_connect_timeout=5, socket_timeout=5,
    )


def build_signal(holdings: list, meta: dict) -> dict:
    """将 holdings.json 的最新一条 + 元信息打包为 Redis 信号"""
    signal_id = meta.get("date", datetime.now().strftime("%Y-%m-%d"))
    return {
        "signal_id": signal_id,
        "timestamp": datetime.now().isoformat(),
        "center": meta.get("center"),
        "center_pct3y": meta.get("center_pct3y"),
        "light": meta.get("light"),
        "n_bonds": len(holdings),
        "holdings": holdings,
    }


def push_signal(signal: dict) -> str:
    """写入 Redis key + 发布通知, 返回 signal_id"""
    r = get_redis()
    body = json.dumps(signal, ensure_ascii=False)

    pipe = r.pipeline()
    pipe.set(KEY_SIGNAL, body)
    pipe.lpush(KEY_LOG, body)
    pipe.ltrim(KEY_LOG, 0, 51)  # 最多保留 52 条
    pipe.publish(CHAN_UPDATE, signal["signal_id"])
    pipe.execute()

    logger.info(f"信号已发布: {signal['signal_id']}, {signal['n_bonds']} 只, 中枢 {signal['center']}%")
    return signal["signal_id"]


def fetch_signal() -> dict | None:
    """读取最新信号, 无信号返回 None"""
    r = get_redis()
    body = r.get(KEY_SIGNAL)
    if not body:
        return None
    return json.loads(body)
