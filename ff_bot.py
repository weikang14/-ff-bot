"""
Forex Factory USD High-Impact Calendar Bot + Claude AI 分析
- 红色USD事件：2小时/1小时/30分钟前提醒
- 报告公布后3分钟：自动抓取实际数据 → Claude AI 分析对美金/黄金的影响 → Telegram推送
"""

import requests
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─────────────────────────────────────────
# ⚙️  配置区 — 只需修改这里
# ─────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = "YOUR_BOT_TOKEN"
TELEGRAM_CHAT_ID    = "YOUR_CHAT_ID"
GOOGLE_API_KEY      = "YOUR_GOOGLE_API_KEY"       # https://aistudio.google.com/apikey

LOCAL_TZ     = ZoneInfo("Asia/Kuala_Lumpur")
UTC_TZ       = ZoneInfo("UTC")
ALERT_OFFSETS = [120, 60, 30]   # 提前提醒（分钟）
REFRESH_HOURS = 6
POST_DELAY_MIN = 3              # 报告公布后等多少分钟再抓实际值
# ─────────────────────────────────────────

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

scheduled_jobs: set[str] = set()


# ══════════════════════════════════════════
# Telegram
# ══════════════════════════════════════════
async def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=10)
        r.raise_for_status()
        log.info("✅ Telegram 已发送")
    except Exception as e:
        log.error(f"❌ Telegram 失败: {e}")


# ══════════════════════════════════════════
# Claude AI 分析
# ══════════════════════════════════════════
async def gemma_analysis(title: str, forecast: str, previous: str, actual: str) -> str:
    prompt = f"""你是一位专业外汇和黄金交易分析师。
以下是刚公布的美国重大经济数据：

📌 报告名称：{title}
📊 预测值：{forecast}
📊 前值：{previous}
📊 实际值：{actual}

请分析：
1. 实际值 vs 预测值 — 超预期/低于预期/符合预期
2. 对 美元(USD) 的短期影响（看涨/看跌/中性，及原因）
3. 对 黄金 XAUUSD 的短期影响（看涨/看跌/中性，及原因）
4. 交易建议（简短，1-2句）

语言：中文，简洁专业，每点不超过2句话。"""

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemma-4:generateContent?key={GOOGLE_API_KEY}"
    )
    try:
        r = requests.post(
            url,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        log.error(f"❌ Gemma API 失败: {e}")
        return "⚠️ AI 分析暂时不可用，请手动判断市场影响。"


# ══════════════════════════════════════════
# 公布后：抓实际值 + AI 分析
# ══════════════════════════════════════════
async def post_release_analysis(title: str, dt_utc: datetime,
                                 forecast: str, previous: str) -> None:
    log.info(f"🔍 抓取实际值: {title}")

    # 重新拉取日历，找到对应事件的 actual 值
    actual = "N/A"
    try:
        r = requests.get(FF_URL, timeout=15)
        r.raise_for_status()
        for ev in r.json():
            if ev.get("country", "").upper() != "USD":
                continue
            try:
                ev_dt = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            except Exception:
                continue
            if ev.get("title") == title and abs((ev_dt - dt_utc).total_seconds()) < 120:
                actual = ev.get("actual") or "尚未公布"
                break
    except Exception as e:
        log.error(f"❌ 抓取实际值失败: {e}")

    # Claude 分析
    analysis = await gemma_analysis(title, forecast, previous, actual)

    time_str = dt_utc.astimezone(LOCAL_TZ).strftime("%H:%M MYT")
    msg = (
        f"🤖 <b>AI 市场分析 — {title}</b>\n"
        f"{'─' * 30}\n"
        f"🕒 公布时间：{time_str}\n"
        f"📊 预测：{forecast}　前值：{previous}　<b>实际：{actual}</b>\n"
        f"{'─' * 30}\n"
        f"{analysis}\n"
        f"{'─' * 30}\n"
        f"⚠️ 以上为AI分析仅供参考，请结合图表判断！"
    )
    await send_telegram(msg)


# ══════════════════════════════════════════
# Forex Factory 日历
# ══════════════════════════════════════════
def fetch_usd_high_impact() -> list[dict]:
    try:
        r = requests.get(FF_URL, timeout=15)
        r.raise_for_status()
        events = r.json()
    except Exception as e:
        log.error(f"❌ 拉取日历失败: {e}")
        return []

    result = []
    for ev in events:
        if ev.get("country", "").upper() != "USD":
            continue
        if ev.get("impact", "").lower() != "high":
            continue
        try:
            dt_utc   = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            dt_local = dt_utc.astimezone(LOCAL_TZ)
        except Exception:
            continue
        result.append({
            "title":    ev.get("title", "Unknown"),
            "dt_utc":   dt_utc,
            "dt_local": dt_local,
            "forecast": ev.get("forecast") or "—",
            "previous": ev.get("previous") or "—",
        })

    log.info(f"📅 找到 {len(result)} 个 USD 红色事件")
    return result


# ══════════════════════════════════════════
# 消息格式
# ══════════════════════════════════════════
def fmt_alert(ev: dict, mins: int) -> str:
    emoji = {120: "⏰", 60: "🔔", 30: "🚨"}.get(mins, "📢")
    return (
        f"{emoji} <b>USD 重大报告预告</b>\n"
        f"{'─' * 28}\n"
        f"📌 <b>{ev['title']}</b>\n"
        f"🕒 公布时间：{ev['dt_local'].strftime('%Y-%m-%d %H:%M')} MYT\n"
        f"⏳ 距公布：<b>{mins} 分钟</b>\n"
        f"📊 预测：{ev['forecast']}　前值：{ev['previous']}\n"
        f"{'─' * 28}\n"
        f"⚠️ 请注意仓位与风险管理！"
    )

def fmt_daily_summary(events: list[dict]) -> str:
    today = datetime.now(LOCAL_TZ).date()
    today_ev = sorted(
        [e for e in events if e["dt_local"].date() == today],
        key=lambda x: x["dt_utc"]
    )
    if not today_ev:
        return "📅 <b>今日无 USD 红色重大报告</b>\n安心持仓，注意盘面即可。"

    lines = ["📅 <b>今日 USD 重大报告（含AI分析）</b>", "─" * 28]
    for ev in today_ev:
        t = ev["dt_local"].strftime("%H:%M")
        lines.append(
            f"🔴 {t} MYT — <b>{ev['title']}</b>\n"
            f"    预测：{ev['forecast']}　前值：{ev['previous']}\n"
            f"    📊 报告公布后将推送 AI 分析"
        )
    lines += ["─" * 28, "⚠️ 请提前做好仓位管理！"]
    return "\n".join(lines)


# ══════════════════════════════════════════
# 调度
# ══════════════════════════════════════════
def schedule_alerts(scheduler: AsyncIOScheduler, events: list[dict]) -> None:
    now = datetime.now(UTC_TZ)

    for ev in events:
        # ── 提前提醒 ──
        for mins in ALERT_OFFSETS:
            jid = f"alert_{ev['title']}_{ev['dt_utc'].isoformat()}_{mins}"
            if jid not in scheduled_jobs:
                fire_at = ev["dt_utc"] - timedelta(minutes=mins)
                if fire_at > now:
                    scheduler.add_job(
                        send_telegram,
                        trigger="date", run_date=fire_at,
                        args=[fmt_alert(ev, mins)],
                        id=jid, replace_existing=True,
                    )
                    scheduled_jobs.add(jid)
                    log.info(f"📌 预告: {ev['title']} | -{mins}min @ "
                             f"{fire_at.astimezone(LOCAL_TZ).strftime('%H:%M MYT')}")

        # ── 公布后 AI 分析 ──
        jid_ai = f"ai_{ev['title']}_{ev['dt_utc'].isoformat()}"
        if jid_ai not in scheduled_jobs:
            fire_at = ev["dt_utc"] + timedelta(minutes=POST_DELAY_MIN)
            if fire_at > now:
                scheduler.add_job(
                    post_release_analysis,
                    trigger="date", run_date=fire_at,
                    args=[ev["title"], ev["dt_utc"], ev["forecast"], ev["previous"]],
                    id=jid_ai, replace_existing=True,
                )
                scheduled_jobs.add(jid_ai)
                log.info(f"🤖 AI分析: {ev['title']} @ "
                         f"{fire_at.astimezone(LOCAL_TZ).strftime('%H:%M MYT')}")


# ══════════════════════════════════════════
# 主程序
# ══════════════════════════════════════════
async def refresh_and_schedule(scheduler: AsyncIOScheduler) -> None:
    log.info("🔄 刷新 Forex Factory 日历...")
    events = fetch_usd_high_impact()
    if events:
        await send_telegram(fmt_daily_summary(events))
        schedule_alerts(scheduler, events)

async def main() -> None:
    log.info("🚀 Forex Factory + AI Bot 启动中...")
    scheduler = AsyncIOScheduler(timezone=str(UTC_TZ))
    scheduler.start()

    await refresh_and_schedule(scheduler)

    scheduler.add_job(
        refresh_and_schedule,
        trigger="interval", hours=REFRESH_HOURS,
        args=[scheduler], id="auto_refresh",
    )

    log.info("✅ Bot 运行中，按 Ctrl+C 停止")
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        log.info("🛑 Bot 已停止")
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
