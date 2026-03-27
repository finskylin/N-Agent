"""
Realtime Money Flow Skill
实时资金流向分析技能 - 获取实时主力资金、北向资金、融资融券数据

数据源优先级:
1. 东方财富历史日线API (push2his) — 最近30-60日日线数据，延迟约1天
2. 东方财富分钟级API (push2) — 今日盘中实时分钟级资金流向
3. AkShare — 备用降级方案
"""
import os
import sys
import json
import logging
import asyncio
import re
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _safe_float(val) -> float:
    """安全转换浮点数"""
    if val is None or val == '-' or val == '':
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _parse_eastmoney_klines(klines: list) -> list:
    """解析东方财富 kline 字符串列表为标准 daily 格式"""
    daily = []
    for kline in klines:
        parts = kline.split(",")
        if len(parts) >= 7:
            try:
                daily.append({
                    "date": parts[0].split(" ")[0],  # 兼容 "2026-03-19 15:00" 格式
                    "main_net": round(_safe_float(parts[1]) / 10000, 2),
                    "main_net_pct": round(_safe_float(parts[6]), 2),
                    "super_large_net": round(_safe_float(parts[5]) / 10000, 2),
                    "large_net": round(_safe_float(parts[4]) / 10000, 2),
                    "medium_net": round(_safe_float(parts[3]) / 10000, 2),
                    "small_net": round(_safe_float(parts[2]) / 10000, 2),
                })
            except Exception:
                continue
    return sorted(daily, key=lambda x: x["date"], reverse=True)


def _fetch_eastmoney_playwright(code: str, market: str) -> Dict[str, Any]:
    """
    通过 Playwright 获取东方财富资金流向数据（兜底方案）
    绕过东方财富对新 TCP 连接的反爬机制
    """
    result = {"daily": [], "today_summary": None, "available": False, "error": None}
    try:
        from playwright.sync_api import sync_playwright
        secid = f"1.{code}" if market.upper() == "SH" else f"0.{code}"

        daily_url = (
            f"https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
            f"?lmt=60&klt=101&secid={secid}"
            f"&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
        )
        intraday_url = (
            f"https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
            f"?lmt=0&klt=1&secid={secid}"
            f"&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
        )

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                extra_http_headers={"Referer": "https://data.eastmoney.com/zjlx/"},
            )
            page = ctx.new_page()

            # 获取日线数据
            try:
                resp = page.goto(daily_url, timeout=20000)
                if resp and resp.status == 200:
                    text = page.inner_text("body") or ""
                    m = re.search(r'\{.*\}', text, re.DOTALL)
                    if m:
                        data = json.loads(m.group())
                        klines = data.get("data", {}).get("klines", [])
                        if klines:
                            result["daily"] = _parse_eastmoney_klines(klines)
                            result["available"] = True
            except Exception as e:
                result["error"] = f"playwright daily: {e}"

            # 获取盘中数据
            try:
                resp2 = page.goto(intraday_url, timeout=15000)
                if resp2 and resp2.status == 200:
                    text2 = page.inner_text("body") or ""
                    m2 = re.search(r'\{.*\}', text2, re.DOTALL)
                    if m2:
                        data2 = json.loads(m2.group())
                        klines2 = data2.get("data", {}).get("klines", [])
                        if klines2:
                            last = klines2[-1].split(",")
                            if len(last) >= 5:
                                today_str = last[0].split(" ")[0]
                                result["today_summary"] = {
                                    "date": today_str,
                                    "time": last[0],
                                    "main_net": round(_safe_float(last[1]) / 10000, 2),
                                    "small_net": round(_safe_float(last[2]) / 10000, 2),
                                    "medium_net": round(_safe_float(last[3]) / 10000, 2),
                                    "large_net": round(_safe_float(last[4]) / 10000, 2),
                                    "super_large_net": round(_safe_float(last[5]) / 10000, 2) if len(last) > 5 else 0.0,
                                }
            except Exception:
                pass

            browser.close()
    except Exception as e:
        result["error"] = f"playwright failed: {e}"
        logger.error(f"Playwright资金流向获取失败: {e}")

    return result


async def _fetch_eastmoney_daily(code: str, market: str) -> Dict[str, Any]:
    """
    东方财富历史日线资金流向 (push2his) — aiohttp 版本（主进程长连接可用）
    数据单位: 元 -> 转换为万元，返回最近60日日线数据
    """
    result = {
        "source": "eastmoney_daily",
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "daily": [],
        "available": False,
        "error": None
    }

    try:
        import aiohttp
        secid = f"1.{code}" if market.upper() == "SH" else f"0.{code}"
        url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
        params = {
            "lmt": "60", "klt": "101", "secid": secid,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/zjlx/"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        m = re.search(r'\{.*\}', text, re.DOTALL)
                        data = json.loads(m.group()) if m else {}
                    klines = data.get("data", {}).get("klines", []) if data else []
                    if klines:
                        result["daily"] = _parse_eastmoney_klines(klines)
                        result["available"] = True
                    else:
                        result["error"] = "API返回数据为空"
                else:
                    result["error"] = f"HTTP错误: {resp.status}"
    except asyncio.TimeoutError:
        result["error"] = "请求超时(15秒)"
    except Exception as e:
        result["error"] = f"请求失败: {str(e)}"
        logger.error(f"东方财富日线API获取失败: {e}")

    return result


async def _fetch_eastmoney_intraday(code: str, market: str) -> Dict[str, Any]:
    """
    东方财富盘中分钟级资金流向 (push2) — aiohttp 版本（主进程长连接可用）
    """
    result = {
        "source": "eastmoney_intraday",
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "today_summary": None,
        "available": False,
        "error": None
    }
    try:
        import aiohttp
        secid = f"1.{code}" if market.upper() == "SH" else f"0.{code}"
        url = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
        params = {
            "lmt": "0", "klt": "1", "secid": secid,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/zjlx/"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        m = re.search(r'\{.*\}', text, re.DOTALL)
                        data = json.loads(m.group()) if m else {}
                    klines = data.get("data", {}).get("klines", []) if data else []
                    if klines:
                        last = klines[-1].split(",")
                        if len(last) >= 5:
                            today_str = last[0].split(" ")[0]
                            result["today_summary"] = {
                                "date": today_str,
                                "time": last[0],
                                "main_net": round(_safe_float(last[1]) / 10000, 2),
                                "small_net": round(_safe_float(last[2]) / 10000, 2),
                                "medium_net": round(_safe_float(last[3]) / 10000, 2),
                                "large_net": round(_safe_float(last[4]) / 10000, 2),
                                "super_large_net": round(_safe_float(last[5]) / 10000, 2) if len(last) > 5 else 0.0,
                            }
                            result["available"] = True
                            result["minute_count"] = len(klines)
                    else:
                        result["error"] = "盘中数据为空（可能非交易时间）"
                else:
                    result["error"] = f"HTTP错误: {resp.status}"
    except asyncio.TimeoutError:
        result["error"] = "请求超时(10秒)"
    except Exception as e:
        result["error"] = f"请求失败: {str(e)}"
    return result


def _get_fund_flow_akshare(code: str, market: str, days: int) -> List[Dict]:
    """AkShare备用数据源"""
    try:
        import akshare as ak

        df = ak.stock_individual_fund_flow(stock=code, market=market.lower())

        if df is None or df.empty:
            return []

        daily = []
        for _, row in df.head(days).iterrows():
            main_net = _safe_float(row.get('主力净流入-净额', row.get('主力净流入净额', 0)))

            daily.append({
                "date": str(row.get('日期', '')),
                "main_net": round(main_net / 10000, 2),
                "main_net_pct": _safe_float(row.get('主力净流入-净占比', row.get('主力净流入净占比', 0))),
            })

        return sorted(daily, key=lambda x: x.get('date', ''), reverse=True)

    except Exception as e:
        logger.warning(f"AkShare资金流向获取失败: {e}")
        return []


def _get_north_bound_data(ts_code: str) -> Dict[str, Any]:
    """获取北向资金数据"""
    default = {
        "net_5d": 0.0, "net_10d": 0.0,
        "holding_ratio": 0.0, "holding_change": 0.0,
        "available": False
    }
    try:
        import akshare as ak
        code = ts_code.split('.')[0]

        df = ak.stock_hsgt_hold_stock_em(market="北向")
        if df is not None and not df.empty:
            row_data = df[df['代码'].astype(str) == code]
            if not row_data.empty:
                row = row_data.iloc[0]
                return {
                    "holding_ratio": _safe_float(row.get('占流通股比', 0)),
                    "holding_change": _safe_float(row.get('增减', 0)),
                    "available": True
                }
    except Exception as e:
        logger.warning(f"北向资金数据获取失败: {e}")
    return default


def _calculate_summary(daily: List[Dict], north_bound: Dict) -> Dict[str, Any]:
    """计算资金汇总指标（单位：万元 -> 亿元）"""
    if not daily:
        return {
            "main_net_3d": 0.0, "main_net_5d": 0.0, "main_net_10d": 0.0,
            "main_trend": "数据不足",
            "latest_day_net": 0.0,
            "latest_day_date": "-",
            "flow_momentum": 0.0
        }

    # main_net 单位是万元，转换为亿元
    main_3d = sum(d.get('main_net', 0) for d in daily[:3]) / 10000
    main_5d = sum(d.get('main_net', 0) for d in daily[:5]) / 10000
    main_10d = sum(d.get('main_net', 0) for d in daily[:10]) / 10000
    latest_net = daily[0].get('main_net', 0) / 10000  # 亿元
    latest_date = daily[0].get('date', '-')

    # 判断趋势
    if main_5d > 1:
        trend = "持续流入"
    elif main_5d < -1:
        if latest_net > 0:
            trend = "流出放缓"
        else:
            trend = "持续流出"
    elif abs(main_5d) <= 0.5:
        trend = "震荡平衡"
    elif main_5d > 0:
        trend = "震荡流入"
    else:
        trend = "震荡流出"

    # 流出动量（5日流出占10日比例）
    if main_10d != 0:
        flow_momentum = round(abs(main_5d) / abs(main_10d) * 100, 2)
    else:
        flow_momentum = 0

    return {
        "main_net_3d": round(main_3d, 2),
        "main_net_5d": round(main_5d, 2),
        "main_net_10d": round(main_10d, 2),
        "main_trend": trend,
        "latest_day_net": round(latest_net, 4),
        "latest_day_date": latest_date,
        "flow_momentum": flow_momentum,
        "consecutive_outflow_days": _count_consecutive(daily, negative=True),
        "consecutive_inflow_days": _count_consecutive(daily, negative=False)
    }


def _count_consecutive(daily: List[Dict], negative: bool = True) -> int:
    """计算连续流入/流出天数"""
    count = 0
    for d in daily:
        net = d.get('main_net', 0)
        if (negative and net < 0) or (not negative and net > 0):
            count += 1
        else:
            break
    return count


def _analyze_capital_behavior(daily: List[Dict], summary: Dict) -> Dict[str, Any]:
    """分析资金行为"""
    if not daily:
        return {"trend": "数据不足", "sentiment": "中性", "signals": []}

    signals = []
    score = 50

    main_5d = summary.get('main_net_5d', 0)
    main_10d = summary.get('main_net_10d', 0)
    latest = summary.get('latest_day_net', 0)
    latest_date = summary.get('latest_day_date', '-')

    # 5日资金流向
    if main_5d > 1:
        score += 15
        signals.append({"type": "positive", "message": f"5日主力净流入{main_5d:.2f}亿"})
    elif main_5d < -1:
        score -= 15
        signals.append({"type": "negative", "message": f"5日主力净流出{abs(main_5d):.2f}亿"})

    # 最新一日转向信号
    if latest > 0 and main_5d < 0:
        score += 10
        signals.append({"type": "positive", "message": f"{latest_date}资金转为净流入{latest:.4f}亿"})
    elif latest < 0 and main_5d > 0:
        score -= 10
        signals.append({"type": "negative", "message": f"{latest_date}资金转为净流出{abs(latest):.4f}亿"})

    # 流出动量判断
    momentum = summary.get('flow_momentum', 0)
    if main_5d < 0 and main_10d < 0:
        if momentum > 80:
            signals.append({"type": "warning", "message": "流出强度高，短期资金面偏弱"})
        elif momentum < 50:
            score += 5
            signals.append({"type": "info", "message": "流出强度减弱，关注资金转向信号"})

    # 连续流入/流出
    consecutive_in = summary.get('consecutive_inflow_days', 0)
    consecutive_out = summary.get('consecutive_outflow_days', 0)

    if consecutive_in >= 3:
        score += 10
        signals.append({"type": "positive", "message": f"连续{consecutive_in}日资金流入"})
    elif consecutive_out >= 3:
        score -= 5
        signals.append({"type": "negative", "message": f"连续{consecutive_out}日资金流出"})

    # 综合判断
    if score >= 70:
        sentiment, trend = "积极", "资金持续流入"
    elif score >= 55:
        sentiment, trend = "偏多", "资金小幅流入"
    elif score >= 45:
        sentiment, trend = "中性", "资金震荡"
    elif score >= 30:
        sentiment, trend = "偏空", "资金小幅流出"
    else:
        sentiment, trend = "谨慎", "资金持续流出"

    return {
        "trend": trend,
        "sentiment": sentiment,
        "sentiment_score": score,
        "signals": signals
    }


async def _get_realtime_data(code: str, market: str) -> Dict[str, Any]:
    """
    获取资金流向数据（多数据源 + 今日实时补充）

    策略:
    1. 东方财富历史日线API (push2his) — 获取最近60日日线
    2. 东方财富分钟级API (push2) — 获取今日盘中实时汇总
    3. 合并: 如果今日数据可用且日线最新日期不含今日，则将今日实时插入日线首位
    4. AkShare — 最终降级方案
    """

    # 并行请求日线和盘中数据
    daily_task = _fetch_eastmoney_daily(code, market)
    intraday_task = _fetch_eastmoney_intraday(code, market)

    daily_result, intraday_result = await asyncio.gather(daily_task, intraday_task)

    if daily_result.get("available"):
        daily_list = daily_result["daily"]
        today_str = datetime.now().strftime("%Y-%m-%d")

        # 处理今日盘中实时数据
        if intraday_result.get("available") and intraday_result.get("today_summary"):
            today_summary = intraday_result["today_summary"]
            today_date = today_summary["date"]
            latest_daily_date = daily_list[0]["date"] if daily_list else ""

            if today_date != latest_daily_date:
                # 日线没有今日数据，插入盘中汇总
                today_entry = {
                    "date": today_date,
                    "main_net": today_summary["main_net"],
                    "main_net_pct": 0.0,
                    "super_large_net": today_summary.get("super_large_net", 0.0),
                    "large_net": today_summary.get("large_net", 0.0),
                    "medium_net": today_summary.get("medium_net", 0.0),
                    "small_net": today_summary.get("small_net", 0.0),
                    "is_realtime": True,
                }
                daily_list.insert(0, today_entry)
                logger.info(f"合并今日盘中实时数据: {today_date}, main_net={today_summary['main_net']}万元")
            else:
                # 日线已有今日数据，用盘中数据覆盖（更实时）
                daily_list[0]["main_net"] = today_summary["main_net"]
                daily_list[0]["super_large_net"] = today_summary.get("super_large_net", daily_list[0].get("super_large_net", 0))
                daily_list[0]["large_net"] = today_summary.get("large_net", daily_list[0].get("large_net", 0))
                daily_list[0]["medium_net"] = today_summary.get("medium_net", daily_list[0].get("medium_net", 0))
                daily_list[0]["small_net"] = today_summary.get("small_net", daily_list[0].get("small_net", 0))
                daily_list[0]["is_realtime"] = True
                logger.info(f"盘中实时数据覆盖日线({today_date}), main_net={today_summary['main_net']}万元")

        # 判断数据质量
        latest_date = daily_list[0]["date"] if daily_list else ""
        if daily_list and daily_list[0].get("is_realtime"):
            data_quality = "实时"
        elif latest_date == today_str:
            data_quality = "当日"
        elif latest_date >= (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"):
            data_quality = "T+1"
        else:
            data_quality = "延迟"

        return {
            "source": "eastmoney",
            "data_quality": data_quality,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "daily": daily_list,
            "intraday_available": intraday_result.get("available", False),
            "intraday_minutes": intraday_result.get("minute_count", 0),
            "available": True,
        }

    logger.info(f"东方财富aiohttp均失败: daily={daily_result.get('error')}, 尝试Playwright兜底...")

    # Playwright 兜底（绕过东方财富对子进程新TCP连接的反爬）
    try:
        pw_result = _fetch_eastmoney_playwright(code, market)
        if pw_result.get("available") and pw_result.get("daily"):
            daily_list = pw_result["daily"]
            today_str = datetime.now().strftime("%Y-%m-%d")

            # 合并盘中数据
            if pw_result.get("today_summary"):
                today_summary = pw_result["today_summary"]
                today_date = today_summary["date"]
                latest_daily_date = daily_list[0]["date"] if daily_list else ""
                if today_date != latest_daily_date:
                    daily_list.insert(0, {
                        "date": today_date,
                        "main_net": today_summary["main_net"],
                        "main_net_pct": 0.0,
                        "super_large_net": today_summary.get("super_large_net", 0.0),
                        "large_net": today_summary.get("large_net", 0.0),
                        "medium_net": today_summary.get("medium_net", 0.0),
                        "small_net": today_summary.get("small_net", 0.0),
                        "is_realtime": True,
                    })
                else:
                    daily_list[0].update({
                        "main_net": today_summary["main_net"],
                        "super_large_net": today_summary.get("super_large_net", daily_list[0].get("super_large_net", 0)),
                        "is_realtime": True,
                    })

            latest_date = daily_list[0]["date"] if daily_list else ""
            if daily_list and daily_list[0].get("is_realtime"):
                data_quality = "实时"
            elif latest_date == today_str:
                data_quality = "当日"
            elif latest_date >= (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"):
                data_quality = "T+1"
            else:
                data_quality = "延迟"

            logger.info(f"Playwright兜底成功: {len(daily_list)}条数据, quality={data_quality}")
            return {
                "source": "eastmoney_playwright",
                "data_quality": data_quality,
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "daily": daily_list,
                "intraday_available": pw_result.get("today_summary") is not None,
                "available": True,
            }
    except Exception as e:
        logger.warning(f"Playwright兜底失败: {e}")

    logger.info("Playwright兜底也失败，降级至AkShare...")

    # AkShare 降级
    akshare_data = _get_fund_flow_akshare(code, market, 30)
    if akshare_data:
        latest_date = akshare_data[0].get("date", "") if akshare_data else ""
        today_str = datetime.now().strftime("%Y-%m-%d")

        if latest_date == today_str:
            data_quality = "实时"
        elif latest_date and latest_date >= (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"):
            data_quality = "延迟"
        else:
            data_quality = "严重滞后"

        return {
            "source": "akshare",
            "data_quality": data_quality,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "daily": akshare_data,
            "available": True
        }

    return {
        "source": "none",
        "data_quality": "不可用",
        "error": "所有数据源均不可用",
        "daily": [],
        "available": False
    }


async def main_async(params: Dict[str, Any]) -> Dict[str, Any]:
    """异步主函数"""
    ts_code = params.get("ts_code", "").strip()
    if not ts_code:
        return {"error": "缺少 ts_code 参数", "for_llm": {"error": "缺少 ts_code 参数"}}

    days = int(params.get("days", 30))
    code = ts_code.split('.')[0]
    market = ts_code.split('.')[1] if '.' in ts_code else "SZ"

    try:
        realtime_data = await _get_realtime_data(code, market)

        if not realtime_data.get("available"):
            return {
                "error": realtime_data.get("error", "资金数据获取失败"),
                "ts_code": ts_code,
                "for_llm": {"error": "资金数据不可用，请稍后重试"}
            }

        daily = realtime_data.get("daily", [])
        north_bound = _get_north_bound_data(ts_code)
        summary = _calculate_summary(daily, north_bound)
        analysis = _analyze_capital_behavior(daily, summary)

        return {
            "ts_code": ts_code,
            "data_source": realtime_data.get("source"),
            "data_quality": realtime_data.get("data_quality"),
            "data_update_time": realtime_data.get("update_time"),
            "title": f"实时资金流向分析 (近{len(daily)}日)",
            "summary": summary,
            "daily": daily[:days],
            "north_bound": north_bound,
            "analysis": analysis,
            "for_llm": {
                "ts_code": ts_code,
                "data_source": realtime_data.get("source"),
                "data_quality": realtime_data.get("data_quality"),
                "latest_day_date": summary.get("latest_day_date"),
                "main_net_3d_yi": summary.get("main_net_3d", 0),
                "main_net_5d_yi": summary.get("main_net_5d", 0),
                "main_net_10d_yi": summary.get("main_net_10d", 0),
                "latest_day_net_yi": summary.get("latest_day_net", 0),
                "main_trend": summary.get("main_trend"),
                "sentiment": analysis.get("sentiment"),
                "signals": [s["message"] for s in analysis.get("signals", [])]
            }
        }

    except Exception as e:
        logger.error(f"实时资金流向分析失败: {e}", exc_info=True)
        return {
            "error": f"分析失败: {str(e)}",
            "for_llm": {"error": f"分析失败: {str(e)}"}
        }


def main(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    同步入口 — 兼容 FastAPI 协程环境。
    asyncio.run() 在已有事件循环时会报错（如 FastAPI/uvicorn 环境），
    改用独立线程中运行新 event loop，避免嵌套 loop 问题。
    """
    import concurrent.futures

    def _run_in_thread():
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(main_async(params))
        finally:
            loop.close()

    try:
        # 检测是否已有运行中的事件循环
        loop = asyncio.get_running_loop()
        # 已有 loop（FastAPI 环境），在独立线程中运行
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_run_in_thread)
            return future.result(timeout=60)
    except RuntimeError:
        # 无运行中的 loop（命令行/测试环境），直接运行
        return _run_in_thread()


if __name__ == "__main__":
    import argparse

    # 优先读取 stdin JSON（skill_executor 通过 stdin 传参）
    p = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read().strip()
            if raw:
                p = json.loads(raw)
        except Exception:
            pass

    # 命令行参数作为补充（手动测试用）
    parser = argparse.ArgumentParser(description="实时资金流向分析")
    parser.add_argument("--ts_code", type=str, default="", help="股票代码，如 000988.SZ")
    parser.add_argument("--days", type=int, default=30, help="分析最近 N 天")
    args = parser.parse_args()
    if args.ts_code:
        p["ts_code"] = args.ts_code
    if args.days != 30 or "days" not in p:
        p.setdefault("days", args.days)

    result = main(p)
    print(json.dumps(result, ensure_ascii=False, indent=2))
