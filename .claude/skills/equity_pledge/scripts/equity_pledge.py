from typing import Dict, Any
from datetime import datetime

# ============================================================
# --- inlined from _shared/data_adapter.py ---
# ============================================================

"""
Data Adapter - 统一数据适配层
多数据源降级策略:
  1. akshare (东方财富/新浪) - Primary
  2. sina (新浪财经直接API) - Secondary
  3. adata - Tertiary

所有 Skill 通过此模块获取数据，不再直接调用 akshare。
"""
import logging
from typing import Dict, Any, Optional, List
from contextlib import contextmanager
import os
import re
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Sina Finance API Helpers (新浪财经直接接口)
# ------------------------------------------------------------------ #

def _sina_get_realtime_quote(code: str) -> Optional[Dict[str, Any]]:
    """
    通过新浪财经 API 获取单只股票实时行情

    返回: {name, price, open, high, low, close, volume, amount, pe, pb, market_cap, ...}
    """
    try:
        import httpx

        # 确定市场前缀
        if code.startswith('6') or code.startswith('9'):
            symbol = f"sh{code}"
        else:
            symbol = f"sz{code}"

        # 新浪实时行情接口
        url = f"https://hq.sinajs.cn/list={symbol}"
        headers = {
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        resp = httpx.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None

        # 解析响应: var hq_str_sh600519="贵州茅台,1855.00,1832.50,..."
        text = resp.text
        match = re.search(r'"([^"]*)"', text)
        if not match:
            return None

        data = match.group(1).split(',')
        if len(data) < 32:
            return None

        result = {
            "name": data[0],
            "open": _safe_float(data[1]),
            "pre_close": _safe_float(data[2]),
            "price": _safe_float(data[3]),
            "high": _safe_float(data[4]),
            "low": _safe_float(data[5]),
            "volume": _safe_float(data[8]) / 100,  # 股 -> 手
            "amount": _safe_float(data[9]),
            "bid1_vol": _safe_float(data[10]),
            "bid1_price": _safe_float(data[11]),
            "ask1_price": _safe_float(data[21]),
            "ask1_vol": _safe_float(data[20]),
            "date": data[30],
            "time": data[31],
        }

        # 计算涨跌幅
        if result["pre_close"] > 0:
            result["pct_chg"] = round((result["price"] - result["pre_close"]) / result["pre_close"] * 100, 2)
        else:
            result["pct_chg"] = 0

        logger.info(f"sina_get_realtime_quote succeeded: {code} price={result['price']}")
        return result

    except Exception as e:
        logger.warning(f"sina_get_realtime_quote failed for {code}: {e}")
        return None


def _sina_get_financial_indicator(code: str) -> Optional[Dict[str, Any]]:
    """
    通过新浪财经 API 获取股票估值指标 (PE/PB/总市值等)
    使用新浪股票页面的 JSON 接口
    """
    try:
        import httpx

        # 确定市场前缀
        if code.startswith('6') or code.startswith('9'):
            symbol = f"sh{code}"
        else:
            symbol = f"sz{code}"

        # 新浪股票基本面接口
        url = f"https://finance.sina.com.cn/realstock/company/{symbol}/jsvar.js"
        headers = {
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        resp = httpx.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None

        text = resp.text

        # 解析 JavaScript 变量
        result = {}

        # 提取市盈率
        pe_match = re.search(r'var\s+syl\s*=\s*"?([^";]+)"?', text)
        if pe_match:
            result["pe_ttm"] = _safe_float(pe_match.group(1))

        # 提取市净率
        pb_match = re.search(r'var\s+sjl\s*=\s*"?([^";]+)"?', text)
        if pb_match:
            result["pb"] = _safe_float(pb_match.group(1))

        # 提取总市值
        mv_match = re.search(r'var\s+totalcapital\s*=\s*"?([^";]+)"?', text)
        if mv_match:
            result["total_mv"] = _safe_float(mv_match.group(1)) * 10000  # 万 -> 元

        if result:
            logger.info(f"sina_get_financial_indicator succeeded: {code} PE={result.get('pe_ttm')}")
            return result

        return None

    except Exception as e:
        logger.warning(f"sina_get_financial_indicator failed for {code}: {e}")
        return None


def _sina_get_stock_list() -> Optional[pd.DataFrame]:
    """
    通过新浪财经获取A股股票列表
    """
    try:
        import httpx

        all_stocks = []

        # 新浪股票列表接口 (分页获取)
        for market in ['sh', 'sz']:
            page = 1
            while page <= 50:  # 最多50页
                url = f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={page}&num=80&sort=symbol&asc=1&node={market}_a&symbol=&_s_r_a=init"

                try:
                    resp = httpx.get(url, timeout=10)
                    if resp.status_code != 200:
                        break

                    # 解析 JSON
                    import json
                    data = json.loads(resp.text)

                    if not data:
                        break

                    for item in data:
                        all_stocks.append({
                            'code': item.get('symbol', ''),
                            'name': item.get('name', ''),
                        })

                    page += 1

                except Exception:
                    break

        if all_stocks:
            df = pd.DataFrame(all_stocks)
            logger.info(f"sina_get_stock_list succeeded: {len(df)} stocks")
            return df

        return None

    except Exception as e:
        logger.warning(f"sina_get_stock_list failed: {e}")
        return None


def _sina_get_all_realtime() -> Optional[pd.DataFrame]:
    """
    通过新浪财经批量获取A股实时行情
    """
    try:
        import httpx

        all_data = []

        # 新浪行情中心接口
        for market in ['sh_a', 'sz_a']:
            for page in range(1, 60):  # 每页80条，约60页覆盖所有A股
                url = f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={page}&num=80&sort=symbol&asc=1&node={market}&symbol=&_s_r_a=page"

                try:
                    resp = httpx.get(url, timeout=15)
                    if resp.status_code != 200:
                        break

                    import json
                    data = json.loads(resp.text)

                    if not data:
                        break

                    for item in data:
                        all_data.append({
                            'code': item.get('symbol', ''),
                            'name': item.get('name', ''),
                            'price': _safe_float(item.get('trade', 0)),
                            'pct_chg': _safe_float(item.get('changepercent', 0)),
                            'volume': _safe_float(item.get('volume', 0)) / 100,
                            'amount': _safe_float(item.get('amount', 0)),
                            'pe_ttm': _safe_float(item.get('per', 0)),
                            'pb': _safe_float(item.get('pb', 0)),
                            'total_mv': _safe_float(item.get('mktcap', 0)) * 10000,
                        })
                except Exception as e:
                    logger.debug(f"sina page {page} failed: {e}")
                    break

        if all_data:
            df = pd.DataFrame(all_data)
            logger.info(f"sina_get_all_realtime succeeded: {len(df)} stocks")
            return df

        return None

    except Exception as e:
        logger.warning(f"sina_get_all_realtime failed: {e}")
        return None

# ------------------------------------------------------------------ #
#  Proxy Helper
# ------------------------------------------------------------------ #

_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


@contextmanager
def no_proxy():
    """临时清除 HTTP 代理环境变量（akshare 访问国内 API 不需要代理）"""
    saved = {k: os.environ.pop(k) for k in _PROXY_ENV_KEYS if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


# ------------------------------------------------------------------ #
#  1. get_all_stocks_realtime
# ------------------------------------------------------------------ #

def get_all_stocks_realtime() -> Optional[pd.DataFrame]:
    """
    获取 A 股全量实时行情（含 PE/PB/总市值）

    标准列名: code, name, price, pct_chg, volume, amount, pe_ttm, pb, total_mv

    降级策略: akshare(东财) → sina(新浪) → adata
    """
    # Primary: akshare (东方财富)
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty:
            col_map = {
                '代码': 'code',
                '名称': 'name',
                '最新价': 'price',
                '涨跌幅': 'pct_chg',
                '成交量': 'volume',
                '成交额': 'amount',
                '市盈率-动态': 'pe_ttm',
                '市净率': 'pb',
                '总市值': 'total_mv',
            }
            result = df.rename(columns=col_map)
            logger.info(f"[akshare/东财] get_all_stocks_realtime succeeded: {len(result)} rows")
            return result
    except Exception as e:
        logger.warning(f"[akshare/东财] get_all_stocks_realtime failed: {e}, trying sina fallback")

    # Secondary: sina (新浪财经)
    try:
        df = _sina_get_all_realtime()
        if df is not None and not df.empty:
            logger.info(f"[sina/新浪] get_all_stocks_realtime succeeded: {len(df)} rows")
            return df
    except Exception as e:
        logger.warning(f"[sina/新浪] get_all_stocks_realtime failed: {e}, trying adata fallback")

    # Tertiary: adata
    try:
        import adata
        df = adata.stock.market.list_market_current()
        if df is not None and not df.empty:
            col_map = {
                'stock_code': 'code',
                'short_name': 'name',
                'price': 'price',
                'change_pct': 'pct_chg',
                'volume': 'volume',
                'amount': 'amount',
            }
            result = df.rename(columns=col_map)
            # adata 不返回 PE/PB/总市值，填充 0.0 (而非 NaN, 避免下游过滤失败)
            for col in ['pe_ttm', 'pb', 'total_mv']:
                if col not in result.columns:
                    result[col] = 0.0
            logger.info(f"[adata] get_all_stocks_realtime succeeded: {len(result)} rows")
            return result
    except Exception as e:
        logger.error(f"[adata] get_all_stocks_realtime also failed: {e}")

    return None


# ------------------------------------------------------------------ #
#  2. get_stock_info
# ------------------------------------------------------------------ #

def get_stock_info(code: str) -> Optional[Dict[str, Any]]:
    """
    获取个股基本信息（行业、上市日期、总市值、流通市值等）

    返回 dict: {name, industry, area, list_date, total_mv, circ_mv, ...}
    """
    # Primary: akshare
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_individual_info_em(symbol=code)
        if df is not None and not df.empty:
            info = dict(zip(df['item'], df['value']))
            logger.info(f"akshare get_stock_info succeeded: {code}")
            return info
    except Exception as e:
        logger.warning(f"akshare get_stock_info failed for {code}: {e}, trying adata fallback")

    # Fallback 1: akshare stock_zh_a_spot_em 全市场行情中查找
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty:
            code6 = code.zfill(6)
            match = df[df['代码'].astype(str) == code6]
            if not match.empty:
                r = match.iloc[0]
                info = {
                    '股票简称': r.get('名称', ''),
                    '名称': r.get('名称', ''),
                    '最新价': r.get('最新价', 0),
                    '涨跌幅': r.get('涨跌幅', 0),
                    '总市值': r.get('总市值', 0),
                    '流通市值': r.get('流通市值', 0),
                }
                logger.info(f"[akshare/spot_em] get_stock_info succeeded: {code}")
                return info
    except Exception as e:
        logger.warning(f"[akshare/spot_em] get_stock_info fallback failed: {e}")

    # Fallback 2: adata
    try:
        import adata
        # 获取基本信息
        df_all = adata.stock.info.all_code()
        if df_all is not None and not df_all.empty:
            row = df_all[df_all['stock_code'] == code]
            if not row.empty:
                r = row.iloc[0]
                info = {
                    '股票简称': r.get('short_name', ''),
                    '行业': r.get('industry', ''),
                    '上市时间': str(r.get('list_date', '')),
                }
                # 尝试获取核心指标补充
                try:
                    df_core = adata.stock.finance.get_core_index(stock_code=code)
                    if df_core is not None and not df_core.empty:
                        latest = df_core.iloc[0]
                        info['总市值'] = latest.get('total_mv', 0)
                        info['流通市值'] = latest.get('circ_mv', 0)
                except Exception:
                    pass
                logger.info(f"adata get_stock_info succeeded: {code}")
                return info
    except Exception as e:
        logger.error(f"adata get_stock_info also failed for {code}: {e}")

    return None


# ------------------------------------------------------------------ #
#  3. get_stock_history
# ------------------------------------------------------------------ #

def get_stock_history(
    code: str,
    period: str = "daily",
    start_date: str = "",
    end_date: str = "",
    adjust: str = "qfq",
) -> Optional[pd.DataFrame]:
    """
    获取个股历史 K 线数据

    标准列名: trade_date, open, close, high, low, volume, amount, pct_chg, change_amt, turnover
    """
    # Primary: akshare
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_zh_a_hist(
                symbol=code, period=period,
                start_date=start_date, end_date=end_date,
                adjust=adjust,
            )
        if df is not None and not df.empty:
            col_map = {
                '日期': 'trade_date',
                '开盘': 'open',
                '收盘': 'close',
                '最高': 'high',
                '最低': 'low',
                '成交量': 'volume',
                '成交额': 'amount',
                '涨跌幅': 'pct_chg',
                '涨跌额': 'change_amt',
                '换手率': 'turnover',
            }
            result = df.rename(columns=col_map)
            logger.info(f"akshare get_stock_history succeeded: {code}, {len(result)} rows")
            return result
    except Exception as e:
        logger.warning(f"akshare/东财 get_stock_history failed for {code}: {e}, trying sina fallback")

    # Fallback 1: akshare 新浪数据源（stock_zh_a_daily）
    if period == "daily":
        try:
            import akshare as ak
            # 新浪数据源需要 sh/sz 前缀
            if code.startswith("6") or code.startswith("9"):
                ak_symbol = f"sh{code}"
            else:
                ak_symbol = f"sz{code}"
            with no_proxy():
                df = ak.stock_zh_a_daily(symbol=ak_symbol, adjust=adjust)
            if df is not None and not df.empty:
                # 按日期过滤
                if 'date' in df.columns:
                    df['date'] = pd.to_datetime(df['date'])
                    if start_date:
                        df = df[df['date'] >= pd.to_datetime(start_date)]
                    if end_date:
                        df = df[df['date'] <= pd.to_datetime(end_date)]
                col_map = {
                    'date': 'trade_date',
                    'open': 'open',
                    'close': 'close',
                    'high': 'high',
                    'low': 'low',
                    'volume': 'volume',
                    'amount': 'amount',
                    'turnover': 'turnover',
                }
                result = df.rename(columns=col_map)
                # 删除不需要的列
                if 'outstanding_share' in result.columns:
                    result = result.drop(columns=['outstanding_share'])
                # 补充缺失列
                if 'pct_chg' not in result.columns:
                    result['pct_chg'] = result['close'].pct_change() * 100
                if 'change_amt' not in result.columns:
                    result['change_amt'] = result['close'].diff()
                logger.info(f"akshare/新浪 get_stock_history succeeded: {code}, {len(result)} rows")
                return result
        except Exception as e:
            logger.warning(f"akshare/新浪 get_stock_history failed for {code}: {e}, trying adata fallback")

    # Fallback 2: adata
    try:
        import adata
        k_type_map = {"daily": 1, "weekly": 2, "monthly": 3}
        k_type = k_type_map.get(period, 1)
        df = adata.stock.market.get_market(
            stock_code=code,
            start_date=start_date if start_date else None,
            end_date=end_date if end_date else None,
            k_type=k_type,
        )
        if df is not None and not df.empty:
            col_map = {
                'trade_date': 'trade_date',
                'open': 'open',
                'close': 'close',
                'high': 'high',
                'low': 'low',
                'volume': 'volume',
                'amount': 'amount',
                'change_pct': 'pct_chg',
                'change': 'change_amt',
                'turnover_ratio': 'turnover',
            }
            result = df.rename(columns=col_map)
            logger.info(f"adata get_stock_history succeeded: {code}, {len(result)} rows")
            return result
    except Exception as e:
        logger.error(f"adata get_stock_history also failed for {code}: {e}")

    return None


# ------------------------------------------------------------------ #
#  4. get_all_stock_codes
# ------------------------------------------------------------------ #

def get_all_stock_codes() -> Optional[pd.DataFrame]:
    """
    获取 A 股所有股票代码列表

    标准列名: code, name
    """
    # Primary: akshare
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_info_a_code_name()
        if df is not None and not df.empty:
            logger.info(f"akshare get_all_stock_codes succeeded: {len(df)} rows")
            return df  # 已有 code, name 列
    except Exception as e:
        logger.warning(f"akshare get_all_stock_codes failed: {e}, trying adata fallback")

    # Fallback: adata
    try:
        import adata
        df = adata.stock.info.all_code()
        if df is not None and not df.empty:
            result = df.rename(columns={'stock_code': 'code', 'short_name': 'name'})
            logger.info(f"adata get_all_stock_codes succeeded: {len(result)} rows")
            return result
    except Exception as e:
        logger.error(f"adata get_all_stock_codes also failed: {e}")

    return None


# ------------------------------------------------------------------ #
#  5. get_financial_abstract
# ------------------------------------------------------------------ #

def get_financial_abstract(code: str) -> Optional[pd.DataFrame]:
    """获取财务摘要数据"""
    # Primary: akshare
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_financial_abstract(symbol=code)
        if df is not None and not df.empty:
            logger.info(f"akshare get_financial_abstract succeeded: {code}")
            return df
    except Exception as e:
        logger.warning(f"akshare get_financial_abstract failed for {code}: {e}, trying adata fallback")

    # Fallback: adata
    try:
        import adata
        df = adata.stock.finance.get_core_index(stock_code=code)
        if df is not None and not df.empty:
            # 转换为与 akshare 兼容的格式
            # adata get_core_index 返回的是横向表，需要转置为与 akshare 类似格式
            result = _convert_adata_finance_to_abstract(df)
            if result is not None:
                logger.info(f"adata get_financial_abstract succeeded: {code}")
                return result
    except Exception as e:
        logger.error(f"adata get_financial_abstract also failed for {code}: {e}")

    return None


def _convert_adata_finance_to_abstract(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """将 adata finance core_index 转换为类似 akshare financial_abstract 的格式"""
    try:
        # adata core_index 字段映射
        col_map = {
            'report_date': '截止日期',
            'basic_eps': '基本每股收益',
            'net_asset_ps': '每股净资产',
            'roe': '净资产收益率(ROE)',
            'total_revenue': '营业总收入',
            'net_profit': '归母净利润',
            'gross_profit_margin': '销售毛利率',
            'net_profit_margin': '销售净利率',
            'debt_asset_ratio': '资产负债率',
        }
        result = df.rename(columns=col_map)
        # 构造与 akshare 相似的 '指标' + 日期列格式
        # 简化处理：直接返回重命名后的 df，让 financial_report.py 的解析逻辑兼容
        if '截止日期' not in result.columns and 'report_date' in df.columns:
            result['截止日期'] = df['report_date']
        return result
    except Exception as e:
        logger.warning(f"Failed to convert adata finance data: {e}")
        return None


# ------------------------------------------------------------------ #
#  6. get_financial_indicators
# ------------------------------------------------------------------ #

def get_financial_indicators(code: str) -> Optional[pd.DataFrame]:
    """获取财务分析指标（周转率、现金流等）"""
    # Primary: akshare
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_financial_analysis_indicator(symbol=code)
        if df is not None and not df.empty:
            logger.info(f"akshare get_financial_indicators succeeded: {code}")
            return df
    except Exception as e:
        logger.warning(f"akshare get_financial_indicators failed for {code}: {e}, trying adata fallback")

    # Fallback: adata - 复用 get_core_index 子集
    try:
        import adata
        df = adata.stock.finance.get_core_index(stock_code=code)
        if df is not None and not df.empty:
            logger.info(f"adata get_financial_indicators succeeded (via core_index): {code}")
            return _convert_adata_finance_to_abstract(df)
    except Exception as e:
        logger.error(f"adata get_financial_indicators also failed for {code}: {e}")

    return None


# ------------------------------------------------------------------ #
#  7. get_fund_flow
# ------------------------------------------------------------------ #

def get_fund_flow(code: str, market: str = "sh", days: int = 20) -> Optional[pd.DataFrame]:
    """
    获取个股资金流向

    标准列名: date, close, pct_chg, main_net_inflow, super_large_net, large_net, medium_net, small_net
    """
    # Primary: akshare
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_individual_fund_flow(stock=code, market=market)
        if df is not None and not df.empty:
            logger.info(f"akshare get_fund_flow succeeded: {code}, {len(df)} rows")
            return df.tail(days)
    except Exception as e:
        logger.warning(f"akshare get_fund_flow failed for {code}: {e}, trying adata fallback")

    # Fallback: adata
    try:
        import adata
        df = adata.stock.market.get_market_fund_flow(stock_code=code)
        if df is not None and not df.empty:
            col_map = {
                'trade_date': '日期',
                'main_net_inflow': '主力净流入-净额',
                'max_net_inflow': '超大单净流入-净额',
                'lg_net_inflow': '大单净流入-净额',
                'mid_net_inflow': '中单净流入-净额',
                'sm_net_inflow': '小单净流入-净额',
            }
            result = df.rename(columns=col_map)
            logger.info(f"adata get_fund_flow succeeded: {code}, {len(result)} rows")
            return result.tail(days)
    except Exception as e:
        logger.error(f"adata get_fund_flow also failed for {code}: {e}")

    return None


# ------------------------------------------------------------------ #
#  8. get_north_bound_holdings
# ------------------------------------------------------------------ #

def get_north_bound_holdings() -> Optional[pd.DataFrame]:
    """获取北向资金持股数据"""
    # Primary: akshare
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_hsgt_hold_stock_em(market="北向", indicator="今日排行")
        if df is not None and not df.empty:
            logger.info(f"akshare get_north_bound_holdings succeeded: {len(df)} rows")
            return df
    except Exception as e:
        logger.warning(f"akshare get_north_bound_holdings failed: {e}, trying adata fallback")

    # Fallback: adata
    try:
        import adata
        df = adata.sentiment.north.north_flow_current()
        if df is not None and not df.empty:
            logger.info(f"adata get_north_bound_holdings succeeded: {len(df)} rows")
            return df
    except Exception as e:
        logger.error(f"adata get_north_bound_holdings also failed: {e}")

    return None


# ------------------------------------------------------------------ #
#  9. get_margin_detail
# ------------------------------------------------------------------ #

def get_margin_detail(market: str = "sh", date: str = "") -> Optional[pd.DataFrame]:
    """获取融资融券明细"""
    # Primary: akshare
    try:
        import akshare as ak
        with no_proxy():
            if market == "sh":
                df = ak.stock_margin_detail_sse(date=date)
            else:
                df = ak.stock_margin_detail_szse(date=date)
        if df is not None and not df.empty:
            logger.info(f"akshare get_margin_detail succeeded: market={market}")
            return df
    except Exception as e:
        logger.warning(f"akshare get_margin_detail failed: {e}, trying adata fallback")

    # Fallback: adata
    try:
        import adata
        df = adata.sentiment.securities_margin()
        if df is not None and not df.empty:
            logger.info(f"adata get_margin_detail succeeded")
            return df
    except Exception as e:
        logger.error(f"adata get_margin_detail also failed: {e}")

    return None


# ------------------------------------------------------------------ #
#  10. get_stock_daily_sina
# ------------------------------------------------------------------ #

def get_stock_daily_sina(
    code: str,
    start_date: str = "",
    end_date: str = "",
    adjust: str = "qfq",
) -> Optional[pd.DataFrame]:
    """
    获取个股日 K 线（新浪数据源，用于 prediction/training）

    返回 DataFrame 带 date index, 列: open, high, low, close, volume, amount, ...
    """
    # Primary: akshare (stock_zh_a_daily, 新浪源)
    try:
        import akshare as ak
        # 转换代码格式: "600519" → "sh600519"
        if code.startswith("6") or code.startswith("9"):
            ak_symbol = f"sh{code}"
        else:
            ak_symbol = f"sz{code}"

        with no_proxy():
            df = ak.stock_zh_a_daily(
                symbol=ak_symbol,
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
        if df is not None and not df.empty:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            # 补充缺失列
            if "amplitude" not in df.columns:
                df["amplitude"] = ((df["high"] - df["low"]) / df["close"].shift(1) * 100).fillna(0)
            if "change_pct" not in df.columns:
                df["change_pct"] = df["close"].pct_change() * 100
            if "change_amt" not in df.columns:
                df["change_amt"] = df["close"].diff()
            logger.info(f"akshare get_stock_daily_sina succeeded: {code}, {len(df)} rows")
            return df
    except Exception as e:
        logger.warning(f"akshare get_stock_daily_sina failed for {code}: {e}, trying adata fallback")

    # Fallback: adata → 复用 get_stock_history
    try:
        df = get_stock_history(code, period="daily", start_date=start_date, end_date=end_date, adjust=adjust)
        if df is not None and not df.empty:
            # 转换为与 akshare stock_zh_a_daily 相同的格式 (date index)
            if 'trade_date' in df.columns:
                df['date'] = pd.to_datetime(df['trade_date'])
                df = df.set_index('date').sort_index()
            # 补充缺失列
            if "amplitude" not in df.columns:
                df["amplitude"] = ((df["high"] - df["low"]) / df["close"].shift(1) * 100).fillna(0)
            if "change_pct" not in df.columns:
                df["change_pct"] = df["close"].pct_change() * 100
            if "change_amt" not in df.columns:
                df["change_amt"] = df["close"].diff()
            if "turnover" not in df.columns:
                df["turnover"] = 0.0
            logger.info(f"adata get_stock_daily_sina fallback succeeded: {code}, {len(df)} rows")
            return df
    except Exception as e:
        logger.error(f"adata fallback for get_stock_daily_sina also failed for {code}: {e}")

    return None


# ------------------------------------------------------------------ #
#  PE/PB 计算降级 (仅针对特定股票)
# ------------------------------------------------------------------ #

def get_stock_pe_pb(code: str) -> Dict[str, float]:
    """
    获取单只股票的 PE/PB/总市值

    降级策略:
      1. akshare(东财) - 全量行情
      2. sina(新浪) - 新浪财经直接接口
      3. adata + sina price - EPS/BPS 计算
    """
    pe_ttm = 0.0
    pb = 0.0
    total_mv = 0.0

    # Primary: akshare spot (东方财富)
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty:
            row = df[df['代码'] == code]
            if not row.empty:
                r = row.iloc[0]
                pe_ttm = _safe_float(r.get('市盈率-动态', 0))
                pb = _safe_float(r.get('市净率', 0))
                total_mv = _safe_float(r.get('总市值', 0))
                if pe_ttm != 0 or pb != 0:
                    logger.info(f"[akshare/东财] get_stock_pe_pb succeeded: {code} PE={pe_ttm} PB={pb}")
                    return {"pe_ttm": pe_ttm, "pb": pb, "total_mv": total_mv}
    except Exception as e:
        logger.warning(f"[akshare/东财] get_stock_pe_pb failed for {code}: {e}")

    # Secondary: sina (新浪财经直接接口)
    try:
        sina_result = _sina_get_financial_indicator(code)
        if sina_result:
            pe_ttm = sina_result.get('pe_ttm', 0)
            pb = sina_result.get('pb', 0)
            total_mv = sina_result.get('total_mv', 0)
            if pe_ttm != 0 or pb != 0:
                logger.info(f"[sina/新浪] get_stock_pe_pb succeeded: {code} PE={pe_ttm} PB={pb}")
                return {"pe_ttm": pe_ttm, "pb": pb, "total_mv": total_mv}
    except Exception as e:
        logger.warning(f"[sina/新浪] get_stock_pe_pb failed for {code}: {e}")

    # Tertiary: adata EPS/BPS + sina 实时价格计算 PE/PB
    try:
        price = 0.0

        # 1) 尝试 sina 获取实时价格
        sina_quote = _sina_get_realtime_quote(code)
        if sina_quote:
            price = sina_quote.get('price', 0)

        # 2) adata 获取 EPS/BPS
        import adata
        df_core = adata.stock.finance.get_core_index(stock_code=code)
        if df_core is not None and not df_core.empty:
            latest = df_core.iloc[0]
            basic_eps = _safe_float(latest.get('basic_eps', 0))
            net_asset_ps = _safe_float(latest.get('net_asset_ps', 0))

            if basic_eps != 0 and price > 0:
                pe_ttm = round(price / basic_eps, 2)
            if net_asset_ps != 0 and price > 0:
                pb = round(price / net_asset_ps, 2)

            logger.info(f"[adata+sina] get_stock_pe_pb computed: {code} PE={pe_ttm} PB={pb} (price={price}, eps={basic_eps}, bps={net_asset_ps})")
    except Exception as e:
        logger.error(f"[adata+sina] get_stock_pe_pb also failed for {code}: {e}")

    return {"pe_ttm": pe_ttm, "pb": pb, "total_mv": total_mv}


# ------------------------------------------------------------------ #
#  Helpers
# ------------------------------------------------------------------ #

def _safe_float(val) -> float:
    """安全转换浮点数"""
    if val is None or val == '-' or val == '':
        return 0.0
    try:
        import math
        result = float(val)
        if math.isnan(result) or math.isinf(result):
            return 0.0
        return result
    except (ValueError, TypeError):
        return 0.0


def _standardize_columns(df: pd.DataFrame, col_map: Dict[str, str]) -> pd.DataFrame:
    """
    统一列名映射
    col_map: {"原始列名": "标准列名"}
    """
    existing_cols = {k: v for k, v in col_map.items() if k in df.columns}
    return df.rename(columns=existing_cols)


def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    清理 DataFrame 中的 NaN/inf 值，确保 JSON 序列化安全
    - 数值列中的 NaN/inf 替换为 None
    - 字符串列中的 NaN 替换为空字符串
    """
    import numpy as np
    for col in df.columns:
        if df[col].dtype in ('float64', 'float32', 'float16'):
            df[col] = df[col].replace([np.inf, -np.inf], np.nan)
            df[col] = df[col].where(df[col].notna(), None)
        elif df[col].dtype == 'object':
            df[col] = df[col].fillna('')
    return df


# ------------------------------------------------------------------ #
#  P1 Adapter Methods
# ------------------------------------------------------------------ #

def get_exchange_summary(exchange: str = "all") -> Optional[pd.DataFrame]:
    """
    获取交易所市场总貌数据
    exchange: 'sse' | 'szse' | 'all'
    降级策略: akshare(stock_sse_summary/stock_szse_summary) → stock_sse_deal_daily
    """
    results = []

    # --- 上交所 ---
    if exchange in ("sse", "all"):
        try:
            import akshare as ak
            with no_proxy():
                df = ak.stock_sse_summary()
            if df is not None and not df.empty:
                logger.info(f"[akshare/东财] get_exchange_summary SSE succeeded: {len(df)} rows")
                df["exchange"] = "sse"
                results.append(df)
        except Exception as e:
            logger.warning(f"[akshare/东财] get_exchange_summary SSE failed: {e}")
            # Fallback: stock_sse_deal_daily
            try:
                with no_proxy():
                    df = ak.stock_sse_deal_daily()
                if df is not None and not df.empty:
                    df["exchange"] = "sse"
                    results.append(df)
                    logger.info(f"[akshare/东财] get_exchange_summary SSE fallback succeeded")
            except Exception as e2:
                logger.warning(f"[akshare/东财] get_exchange_summary SSE fallback failed: {e2}")

    # --- 深交所 ---
    if exchange in ("szse", "all"):
        try:
            import akshare as ak
            with no_proxy():
                df = ak.stock_szse_summary()
            if df is not None and not df.empty:
                logger.info(f"[akshare/东财] get_exchange_summary SZSE succeeded: {len(df)} rows")
                df["exchange"] = "szse"
                results.append(df)
        except Exception as e:
            logger.warning(f"[akshare/东财] get_exchange_summary SZSE failed: {e}")

    if results:
        return pd.concat(results, ignore_index=True)
    logger.error(f"[data_adapter] get_exchange_summary all sources failed: {exchange}")
    return None


def get_bid_ask(code: str) -> Optional[pd.DataFrame]:
    """
    获取个股五档盘口行情
    降级策略: akshare(stock_bid_ask_em) → sina hq 五档数据
    """
    # Primary: AKShare 东财
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_bid_ask_em(symbol=code)
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_bid_ask succeeded: {code}")
            return df
    except Exception as e:
        logger.warning(f"[akshare/东财] get_bid_ask failed: {code}: {e}")

    # Fallback: sina hq 五档数据
    try:
        quote = _sina_get_realtime_quote(code)
        if quote:
            rows = []
            for i in range(1, 6):
                buy_p = _safe_float(quote.get(f'buy{i}_price', 0))
                buy_v = _safe_float(quote.get(f'buy{i}_volume', 0))
                sell_p = _safe_float(quote.get(f'sell{i}_price', 0))
                sell_v = _safe_float(quote.get(f'sell{i}_volume', 0))
                if buy_p > 0 or sell_p > 0:
                    rows.append({"level": f"买{i}", "price": buy_p, "volume": buy_v})
                    rows.append({"level": f"卖{i}", "price": sell_p, "volume": sell_v})
            if rows:
                logger.info(f"[sina] get_bid_ask fallback succeeded: {code}")
                return pd.DataFrame(rows)
    except Exception as e:
        logger.warning(f"[sina] get_bid_ask fallback failed: {code}: {e}")

    logger.error(f"[data_adapter] get_bid_ask all sources failed: {code}")
    return None


def get_minute_kline(code: str, period: str = "5", **kwargs) -> Optional[pd.DataFrame]:
    """
    获取分钟K线数据
    period: '1', '5', '15', '30', '60'
    降级策略: akshare(stock_zh_a_hist_min_em) → stock_zh_a_minute
    """
    col_map = {
        '时间': 'time', '开盘': 'open', '收盘': 'close',
        '最高': 'high', '最低': 'low', '成交量': 'volume',
        '成交额': 'amount', '涨跌幅': 'change_pct',
    }
    # Primary: stock_zh_a_hist_min_em
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_zh_a_hist_min_em(
                symbol=code,
                period=period,
                start_date=kwargs.get('start_date', ''),
                end_date=kwargs.get('end_date', ''),
            )
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_minute_kline succeeded: {code} period={period}, rows={len(df)}")
            return _standardize_columns(df, col_map)
    except Exception as e:
        logger.warning(f"[akshare/东财] get_minute_kline failed: {code}: {e}")

    # Fallback: stock_zh_a_minute
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_zh_a_minute(symbol=f"sh{code}" if code.startswith('6') else f"sz{code}", period=period)
        if df is not None and not df.empty:
            logger.info(f"[akshare/新浪] get_minute_kline fallback succeeded: {code}")
            fb_map = {'day': 'time', 'open': 'open', 'close': 'close', 'high': 'high', 'low': 'low', 'volume': 'volume'}
            return _standardize_columns(df, fb_map)
    except Exception as e:
        logger.warning(f"[akshare/新浪] get_minute_kline fallback failed: {code}: {e}")

    logger.error(f"[data_adapter] get_minute_kline all sources failed: {code}")
    return None


def get_limit_board(date: str = "", board_type: str = "涨停") -> Optional[pd.DataFrame]:
    """
    获取涨跌停池数据
    board_type: '涨停' | '跌停' | '强势' | '次新' | '炸板' | '昨日涨停'
    降级策略: akshare(stock_zt_pool_em等) → 同花顺涨停数据
    """
    if not date:
        date = datetime.now().strftime('%Y%m%d')

    api_map = {
        '涨停': 'stock_zt_pool_em',
        '昨日涨停': 'stock_zt_pool_previous_em',
        '跌停': 'stock_zt_pool_dtgc_em',
        '强势': 'stock_zt_pool_strong_em',
        '次新': 'stock_zt_pool_sub_new_em',
        '炸板': 'stock_zt_pool_zbgc_em',
    }

    func_name = api_map.get(board_type, 'stock_zt_pool_em')

    try:
        import akshare as ak
        func = getattr(ak, func_name, None)
        if func is None:
            logger.warning(f"[akshare] get_limit_board: API {func_name} not found")
            return None
        with no_proxy():
            df = func(date=date)
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_limit_board succeeded: {board_type} {date}, rows={len(df)}")
            return df
    except Exception as e:
        logger.warning(f"[akshare/东财] get_limit_board failed: {board_type} {date}: {e}")

    logger.error(f"[data_adapter] get_limit_board all sources failed: {board_type} {date}")
    return None


def get_dragon_tiger(date: str = "", code: str = "") -> Optional[pd.DataFrame]:
    """
    获取龙虎榜详情
    降级策略: akshare(stock_lhb_detail_em) → 同花顺龙虎榜
    """
    if not date:
        date = datetime.now().strftime('%Y%m%d')

    try:
        import akshare as ak
        with no_proxy():
            if code:
                df = ak.stock_lhb_stock_statistic_em(symbol="近一月")
                if df is not None and not df.empty:
                    df = df[df['代码'].astype(str).str.contains(code)]
            else:
                df = ak.stock_lhb_detail_em(
                    start_date=date,
                    end_date=date,
                )
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_dragon_tiger succeeded: {date}, rows={len(df)}")
            return df
    except Exception as e:
        logger.warning(f"[akshare/东财] get_dragon_tiger failed: {date}: {e}")

    logger.error(f"[data_adapter] get_dragon_tiger all sources failed: {date}")
    return None


def get_performance_forecast(date: str = "", report_type: str = "预告") -> Optional[pd.DataFrame]:
    """
    获取业绩预告/快报/报表/披露时间
    report_type: '预告' | '快报' | '报表' | '披露时间'
    date: 报告期（季度末日期如 20240930），如不指定则自动推算最近报告期
    降级策略: akshare(stock_yjyg_em等) → 同花顺业绩数据
    """
    if not date:
        # 自动推算最近报告期（季度末: 0331, 0630, 0930, 1231）
        now = datetime.now()
        quarter_ends = [
            (now.year, 3, 31), (now.year, 6, 30),
            (now.year, 9, 30), (now.year, 12, 31),
            (now.year - 1, 12, 31), (now.year - 1, 9, 30),
        ]
        for y, m, d in quarter_ends:
            qdate = datetime(y, m, d)
            if qdate < now:
                date = qdate.strftime('%Y%m%d')
                break
        if not date:
            date = f"{now.year - 1}1231"

    api_map = {
        '预告': 'stock_yjyg_em',
        '快报': 'stock_yjkb_em',
        '报表': 'stock_yjbb_em',
    }
    func_name = api_map.get(report_type, 'stock_yjyg_em')

    try:
        import akshare as ak
        func = getattr(ak, func_name, None)
        if func is None:
            logger.warning(f"[akshare] get_performance_forecast: API {func_name} not found")
            return None
        with no_proxy():
            df = func(date=date)
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_performance_forecast succeeded: {report_type} {date}, rows={len(df)}")
            return _clean_dataframe(df)
    except Exception as e:
        logger.warning(f"[akshare/东财] get_performance_forecast failed: {report_type} {date}: {e}")

    logger.error(f"[data_adapter] get_performance_forecast all sources failed: {report_type} {date}")
    return None


def get_top_shareholders(code: str, date: str = "") -> Optional[pd.DataFrame]:
    """
    获取十大股东数据
    降级策略: akshare(stock_gdfx_top_10_em) → 同花顺股东数据
    """
    # 转换代码格式: 600519 → sh600519
    prefix = "sh" if code.startswith('6') else "sz"
    ak_symbol = f"{prefix}{code}"
    try:
        import akshare as ak
        with no_proxy():
            kwargs = {"symbol": ak_symbol}
            if date:
                kwargs["date"] = date
            df = ak.stock_gdfx_top_10_em(**kwargs)
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_top_shareholders succeeded: {code}, rows={len(df)}")
            return df
    except Exception as e:
        logger.warning(f"[akshare/东财] get_top_shareholders failed: {code}: {e}")

    logger.error(f"[data_adapter] get_top_shareholders all sources failed: {code}")
    return None


def get_shareholder_count(code: str) -> Optional[pd.DataFrame]:
    """
    获取股东户数变化数据
    降级策略: akshare(stock_zh_a_gdhs_detail_em) → 同花顺股东户数
    """
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_zh_a_gdhs_detail_em(symbol=code)
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_shareholder_count succeeded: {code}, rows={len(df)}")
            return df
    except Exception as e:
        logger.warning(f"[akshare/东财] get_shareholder_count failed: {code}: {e}")

    logger.error(f"[data_adapter] get_shareholder_count all sources failed: {code}")
    return None


def get_peer_comparison(code: str, compare_type: str = "成长性") -> Optional[pd.DataFrame]:
    """
    获取同行比较数据
    compare_type: '成长性' | '估值' | '杜邦分析' | '公司规模'
    降级策略: akshare(stock_zh_*_comparison_em) → 手动计算
    """
    api_map = {
        '成长性': 'stock_zh_growth_comparison_em',
        '估值': 'stock_zh_valuation_comparison_em',
        '杜邦分析': 'stock_zh_dupont_comparison_em',
        '公司规模': 'stock_zh_scale_comparison_em',
    }
    func_name = api_map.get(compare_type, 'stock_zh_growth_comparison_em')

    # 转换代码格式: 600519 → SH600519
    prefix = "SH" if code.startswith('6') else "SZ"
    ak_symbol = f"{prefix}{code}"

    try:
        import akshare as ak
        func = getattr(ak, func_name, None)
        if func is None:
            logger.warning(f"[akshare] get_peer_comparison: API {func_name} not found")
            return None
        with no_proxy():
            df = func(symbol=ak_symbol)
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_peer_comparison succeeded: {code} {compare_type}, rows={len(df)}")
            return df
    except Exception as e:
        logger.warning(f"[akshare/东财] get_peer_comparison failed: {code} {compare_type}: {e}")

    logger.error(f"[data_adapter] get_peer_comparison all sources failed: {code} {compare_type}")
    return None


def get_balance_sheet(code: str) -> Optional[pd.DataFrame]:
    """
    获取资产负债表
    降级策略: akshare(stock_balance_sheet_by_report_em) → adata
    """
    # 转换代码格式: 600519 → SH600519
    prefix = "SH" if code.startswith('6') else "SZ"
    ak_symbol = f"{prefix}{code}"
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_balance_sheet_by_report_em(symbol=ak_symbol)
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_balance_sheet succeeded: {code}, rows={len(df)}")
            return df
    except Exception as e:
        logger.warning(f"[akshare/东财] get_balance_sheet failed: {code}: {e}")

    # Fallback: adata
    try:
        import adata
        df = adata.stock.finance.get_balance_sheet(stock_code=code)
        if df is not None and not df.empty:
            logger.info(f"[adata] get_balance_sheet fallback succeeded: {code}")
            return df
    except Exception as e:
        logger.warning(f"[adata] get_balance_sheet fallback failed: {code}: {e}")

    logger.error(f"[data_adapter] get_balance_sheet all sources failed: {code}")
    return None


def get_income_statement(code: str) -> Optional[pd.DataFrame]:
    """
    获取利润表
    降级策略: akshare(stock_profit_sheet_by_report_em) → adata
    """
    # 转换代码格式: 600519 → SH600519
    prefix = "SH" if code.startswith('6') else "SZ"
    ak_symbol = f"{prefix}{code}"
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_profit_sheet_by_report_em(symbol=ak_symbol)
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_income_statement succeeded: {code}, rows={len(df)}")
            return df
    except Exception as e:
        logger.warning(f"[akshare/东财] get_income_statement failed: {code}: {e}")

    # Fallback: adata
    try:
        import adata
        df = adata.stock.finance.get_income_statement(stock_code=code)
        if df is not None and not df.empty:
            logger.info(f"[adata] get_income_statement fallback succeeded: {code}")
            return df
    except Exception as e:
        logger.warning(f"[adata] get_income_statement fallback failed: {code}: {e}")

    logger.error(f"[data_adapter] get_income_statement all sources failed: {code}")
    return None


def get_cashflow_statement(code: str) -> Optional[pd.DataFrame]:
    """
    获取现金流量表
    降级策略: akshare(stock_cash_flow_sheet_by_report_em) → adata
    """
    # 转换代码格式: 600519 → SH600519
    prefix = "SH" if code.startswith('6') else "SZ"
    ak_symbol = f"{prefix}{code}"
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_cash_flow_sheet_by_report_em(symbol=ak_symbol)
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_cashflow_statement succeeded: {code}, rows={len(df)}")
            return df
    except Exception as e:
        logger.warning(f"[akshare/东财] get_cashflow_statement failed: {code}: {e}")

    # Fallback: adata
    try:
        import adata
        df = adata.stock.finance.get_cashflow_statement(stock_code=code)
        if df is not None and not df.empty:
            logger.info(f"[adata] get_cashflow_statement fallback succeeded: {code}")
            return df
    except Exception as e:
        logger.warning(f"[adata] get_cashflow_statement fallback failed: {code}: {e}")

    logger.error(f"[data_adapter] get_cashflow_statement all sources failed: {code}")
    return None


def get_key_financial_index(code: str) -> Optional[pd.DataFrame]:
    """
    获取关键财务指标
    降级策略: akshare(stock_financial_abstract_ths) → adata
    """
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_financial_abstract_ths(symbol=code)
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_key_financial_index succeeded: {code}, rows={len(df)}")
            return df
    except Exception as e:
        logger.warning(f"[akshare/东财] get_key_financial_index failed: {code}: {e}")

    # Fallback: adata
    try:
        import adata
        df = adata.stock.finance.get_indicator(stock_code=code)
        if df is not None and not df.empty:
            logger.info(f"[adata] get_key_financial_index fallback succeeded: {code}")
            return df
    except Exception as e:
        logger.warning(f"[adata] get_key_financial_index fallback failed: {code}: {e}")

    logger.error(f"[data_adapter] get_key_financial_index all sources failed: {code}")
    return None


# ------------------------------------------------------------------ #
#  P2 Adapter Methods
# ------------------------------------------------------------------ #

def get_intraday_tick(code: str, date: str = "") -> Optional[pd.DataFrame]:
    """
    获取日内逐笔成交数据
    降级策略: akshare(stock_intraday_em) → stock_intraday_sina
    """
    col_map = {
        '时间': 'time', '价格': 'price', '手数': 'volume',
        '方向': 'direction', '成交额': 'amount',
    }
    # Primary: stock_intraday_em
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_intraday_em(symbol=code)
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_intraday_tick succeeded: {code}, rows={len(df)}")
            return _standardize_columns(df, col_map)
    except Exception as e:
        logger.warning(f"[akshare/东财] get_intraday_tick failed: {code}: {e}")

    # Fallback: stock_intraday_sina
    try:
        import akshare as ak
        prefix = "sh" if code.startswith('6') else "sz"
        with no_proxy():
            df = ak.stock_intraday_sina(symbol=f"{prefix}{code}", date=date if date else datetime.now().strftime('%Y%m%d'))
        if df is not None and not df.empty:
            logger.info(f"[akshare/新浪] get_intraday_tick fallback succeeded: {code}")
            return _standardize_columns(df, col_map)
    except Exception as e:
        logger.warning(f"[akshare/新浪] get_intraday_tick fallback failed: {code}: {e}")

    logger.error(f"[data_adapter] get_intraday_tick all sources failed: {code}")
    return None


def get_block_trade(date: str = "", detail_type: str = "market") -> Optional[pd.DataFrame]:
    """
    获取大宗交易数据
    detail_type: 'market' | 'detail' | 'active'
    降级策略: akshare(stock_dzjy_*) → 交易所官网
    """
    from datetime import timedelta

    # 使用正确的API: stock_dzjy_mrtj(每日统计) / stock_dzjy_mrmx(每日明细)
    api_map = {
        'market': 'stock_dzjy_mrtj',
        'detail': 'stock_dzjy_mrmx',
        'active': 'stock_dzjy_hygtj',
    }
    func_name = api_map.get(detail_type, 'stock_dzjy_mrtj')

    # 对于 active 类型，不需要日期参数
    if detail_type == 'active':
        try:
            import akshare as ak
            func = getattr(ak, func_name, None)
            if func is None:
                return None
            with no_proxy():
                df = func(symbol="A股")
            if df is not None and not df.empty:
                logger.info(f"[akshare/东财] get_block_trade succeeded: active, rows={len(df)}")
                return _clean_dataframe(df)
        except Exception as e:
            logger.warning(f"[akshare/东财] get_block_trade active failed: {e}")
        return None

    # 尝试最近几个交易日
    dates_to_try = []
    if date:
        dates_to_try = [date]
    else:
        today = datetime.now()
        for i in range(7):
            d = today - timedelta(days=i)
            if d.weekday() < 5:
                dates_to_try.append(d.strftime('%Y%m%d'))

    for try_date in dates_to_try:
        try:
            import akshare as ak
            func = getattr(ak, func_name, None)
            if func is None:
                return None
            with no_proxy():
                df = func(start_date=try_date, end_date=try_date)
            if df is not None and not df.empty:
                logger.info(f"[akshare/东财] get_block_trade succeeded: {detail_type} {try_date}, rows={len(df)}")
                return _clean_dataframe(df)
        except Exception as e:
            logger.warning(f"[akshare/东财] get_block_trade failed: {detail_type} {try_date}: {e}")
            continue

    logger.error(f"[data_adapter] get_block_trade all sources failed: {detail_type}")
    return None


def get_northbound_holding(code: str = "") -> Optional[pd.DataFrame]:
    """
    获取北向资金个股持股数据
    降级策略: akshare(stock_hsgt_individual_em) → adata
    """
    try:
        import akshare as ak
        with no_proxy():
            if code:
                df = ak.stock_hsgt_individual_em(symbol=code)
            else:
                df = ak.stock_hsgt_hold_stock_em(market="沪股通")
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_northbound_holding succeeded: {code or 'all'}, rows={len(df)}")
            return df
    except Exception as e:
        logger.warning(f"[akshare/东财] get_northbound_holding failed: {code}: {e}")

    # Fallback: adata
    try:
        import adata
        df = adata.stock.market.get_north_holding(stock_code=code if code else None)
        if df is not None and not df.empty:
            logger.info(f"[adata] get_northbound_holding fallback succeeded: {code or 'all'}")
            return df
    except Exception as e:
        logger.warning(f"[adata] get_northbound_holding fallback failed: {code}: {e}")

    logger.error(f"[data_adapter] get_northbound_holding all sources failed: {code}")
    return None


def get_northbound_flow() -> Optional[pd.DataFrame]:
    """
    获取北向资金流向数据
    降级策略: akshare(stock_hsgt_hist_em) → adata
    """
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_hsgt_hist_em(symbol="沪股通")
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_northbound_flow succeeded: rows={len(df)}")
            return df
    except Exception as e:
        logger.warning(f"[akshare/东财] get_northbound_flow failed: {e}")

    # Fallback: adata
    try:
        import adata
        df = adata.stock.market.get_north_flow()
        if df is not None and not df.empty:
            logger.info(f"[adata] get_northbound_flow fallback succeeded")
            return df
    except Exception as e:
        logger.warning(f"[adata] get_northbound_flow fallback failed: {e}")

    logger.error(f"[data_adapter] get_northbound_flow all sources failed")
    return None


def get_margin_market(exchange: str = "sse", date: str = "") -> Optional[pd.DataFrame]:
    """
    获取融资融券市场数据
    exchange: 'sse' | 'szse'
    降级策略: akshare(stock_margin_sse/szse) → adata
    """
    from datetime import timedelta

    # 如果没指定日期，尝试最近几个交易日
    dates_to_try = []
    if date:
        dates_to_try = [date]
    else:
        today = datetime.now()
        for i in range(7):
            d = today - timedelta(days=i)
            if d.weekday() < 5:  # 工作日
                dates_to_try.append(d.strftime('%Y%m%d'))

    for try_date in dates_to_try:
        try:
            import akshare as ak
            with no_proxy():
                if exchange == "sse":
                    df = ak.stock_margin_sse(start_date=try_date, end_date=try_date)
                else:
                    df = ak.stock_margin_szse(date=try_date)
            if df is not None and not df.empty:
                logger.info(f"[akshare/东财] get_margin_market succeeded: {exchange} {try_date}, rows={len(df)}")
                return _clean_dataframe(df)
        except Exception as e:
            logger.warning(f"[akshare/东财] get_margin_market failed: {exchange} {try_date}: {e}")
            continue

    # Fallback: adata
    try:
        import adata
        df = adata.stock.market.get_margin(market=exchange, trade_date=date)
        if df is not None and not df.empty:
            logger.info(f"[adata] get_margin_market fallback succeeded: {exchange}")
            return df
    except Exception as e:
        logger.warning(f"[adata] get_margin_market fallback failed: {exchange}: {e}")

    logger.error(f"[data_adapter] get_margin_market all sources failed: {exchange} {date}")
    return None


def get_equity_pledge(code: str = "") -> Optional[pd.DataFrame]:
    """
    获取股权质押数据
    降级策略: akshare(stock_gpzy_pledge_ratio_em) → stock_gpzy_industry_data_em
    """
    # Primary: stock_gpzy_pledge_ratio_em (快速，~3s，2300行)
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_gpzy_pledge_ratio_em()
        if df is not None and not df.empty:
            if code:
                for col in df.columns:
                    if '代码' in str(col) or 'code' in str(col).lower():
                        df = df[df[col].astype(str).str.contains(code)]
                        break
            if not df.empty:
                logger.info(f"[akshare/东财] get_equity_pledge succeeded: {code or 'all'}, rows={len(df)}")
                return _clean_dataframe(df)
    except Exception as e:
        logger.warning(f"[akshare/东财] get_equity_pledge failed: {code}: {e}")

    # Fallback: 行业质押数据
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_gpzy_industry_data_em()
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_equity_pledge fallback succeeded: industry data, rows={len(df)}")
            return _clean_dataframe(df)
    except Exception as e:
        logger.warning(f"[akshare/东财] get_equity_pledge fallback failed: {e}")

    logger.error(f"[data_adapter] get_equity_pledge all sources failed: {code}")
    return None


def get_restricted_shares(code_or_date: str = "") -> Optional[pd.DataFrame]:
    """
    获取限售解禁数据
    code_or_date: 股票代码或日期(YYYYMMDD)
    降级策略: akshare(stock_restricted_release_detail_em) → stock_restricted_release_queue_em
    """
    from datetime import timedelta
    try:
        import akshare as ak
        # 使用 stock_restricted_release_detail_em(start_date, end_date)
        if code_or_date and len(code_or_date) == 8 and code_or_date.isdigit():
            start_date = code_or_date
            end_date = code_or_date
        else:
            # 默认查未来3个月
            today = datetime.now()
            start_date = today.strftime('%Y%m%d')
            end_date = (today + timedelta(days=90)).strftime('%Y%m%d')
        with no_proxy():
            df = ak.stock_restricted_release_detail_em(start_date=start_date, end_date=end_date)
        if df is not None and not df.empty:
            if code_or_date and len(code_or_date) == 6 and code_or_date.isdigit():
                # 按股票代码过滤
                for col in df.columns:
                    if '代码' in str(col) or 'code' in str(col).lower():
                        filtered = df[df[col].astype(str).str.contains(code_or_date)]
                        if not filtered.empty:
                            df = filtered
                        break
            logger.info(f"[akshare/东财] get_restricted_shares succeeded: {code_or_date or 'all'}, rows={len(df)}")
            return df
    except Exception as e:
        logger.warning(f"[akshare/东财] get_restricted_shares failed: {code_or_date}: {e}")

    # Fallback: stock_restricted_release_queue_em
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_restricted_release_queue_em()
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_restricted_shares fallback succeeded: rows={len(df)}")
            return df
    except Exception as e:
        logger.warning(f"[akshare/东财] get_restricted_shares fallback failed: {e}")

    logger.error(f"[data_adapter] get_restricted_shares all sources failed: {code_or_date}")
    return None


def get_institutional_visits(code: str = "") -> Optional[pd.DataFrame]:
    """
    获取机构调研数据
    降级策略: akshare(stock_jgdy_detail_em) → stock_jgdy_tj_em
    注意: stock_jgdy_detail_em 使用 date 参数(非 symbol)
    """
    from datetime import timedelta
    # Primary: 尝试最近30天的调研数据
    try:
        import akshare as ak
        today = datetime.now()
        for days_back in [0, 7, 14, 30]:
            try:
                target_date = (today - timedelta(days=days_back)).strftime('%Y%m%d')
                with no_proxy():
                    df = ak.stock_jgdy_detail_em(date=target_date)
                if df is not None and not df.empty:
                    if code:
                        for col in df.columns:
                            if '代码' in str(col) or 'code' in str(col).lower():
                                filtered = df[df[col].astype(str).str.contains(code)]
                                if not filtered.empty:
                                    df = filtered
                                break
                    if not df.empty:
                        logger.info(f"[akshare/东财] get_institutional_visits succeeded: {code or 'all'}, date={target_date}, rows={len(df)}")
                        return _clean_dataframe(df)
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"[akshare/东财] get_institutional_visits failed: {code}: {e}")

    # Fallback: 统计汇总
    try:
        import akshare as ak
        today = datetime.now()
        for days_back in [0, 7, 14, 30]:
            try:
                target_date = (today - timedelta(days=days_back)).strftime('%Y%m%d')
                with no_proxy():
                    df = ak.stock_jgdy_tj_em(date=target_date)
                if df is not None and not df.empty:
                    if code:
                        for col in df.columns:
                            if '代码' in str(col) or 'code' in str(col).lower():
                                filtered = df[df[col].astype(str).str.contains(code)]
                                if not filtered.empty:
                                    df = filtered
                                break
                    if not df.empty:
                        logger.info(f"[akshare/东财] get_institutional_visits fallback succeeded: date={target_date}, rows={len(df)}")
                        return _clean_dataframe(df)
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"[akshare/东财] get_institutional_visits fallback failed: {e}")

    logger.error(f"[data_adapter] get_institutional_visits all sources failed: {code}")
    return None


def get_analyst_ratings(code: str = "") -> Optional[pd.DataFrame]:
    """
    获取分析师评级数据
    降级策略: akshare(stock_comment_detail_zhpj_lspf_em) → stock_analyst_rank_em
    """
    try:
        import akshare as ak
        if code:
            with no_proxy():
                df = ak.stock_comment_detail_zhpj_lspf_em(symbol=code)
            if df is not None and not df.empty:
                logger.info(f"[akshare/东财] get_analyst_ratings succeeded: {code}, rows={len(df)}")
                return df
        else:
            with no_proxy():
                df = ak.stock_analyst_rank_em()
            if df is not None and not df.empty:
                logger.info(f"[akshare/东财] get_analyst_ratings succeeded: all, rows={len(df)}")
                return df
    except Exception as e:
        logger.warning(f"[akshare/东财] get_analyst_ratings failed: {code}: {e}")

    logger.error(f"[data_adapter] get_analyst_ratings all sources failed: {code}")
    return None


# ------------------------------------------------------------------ #
#  Enhancement Adapter Methods
# ------------------------------------------------------------------ #

def get_dividend_distribution(code: str) -> Optional[pd.DataFrame]:
    """
    获取分红配送详情数据
    降级策略: akshare(stock_fhps_detail_em) → 同花顺
    """
    try:
        import akshare as ak
        with no_proxy():
            df = ak.stock_fhps_detail_em(symbol=code)
        if df is not None and not df.empty:
            logger.info(f"[akshare/东财] get_dividend_distribution succeeded: {code}, rows={len(df)}")
            return df
    except Exception as e:
        logger.warning(f"[akshare/东财] get_dividend_distribution failed: {code}: {e}")

    logger.error(f"[data_adapter] get_dividend_distribution all sources failed: {code}")
    return None

# ============================================================
# --- end inlined from _shared/data_adapter.py ---
# ============================================================

import logging

logger = logging.getLogger(__name__)



# ── 兼容层：SkillResult / SkillStatus（老架构接口，保持向后兼容）──
class _SkillStatus:
    SUCCESS = "success"
    ERROR = "error"
    PARTIAL = "partial"

class _SkillResult(dict):
    """轻量兼容类：SkillResult(status=..., data=..., error=...) 直接作为 dict 使用"""
    def __init__(self, status=None, data=None, error=None, **kwargs):
        d = {}
        if status is not None:
            d["status"] = status
        if data is not None:
            if isinstance(data, dict):
                d.update(data)
            else:
                d["data"] = data
        if error is not None:
            d["error"] = error
        d.update(kwargs)
        super().__init__(d)

SkillResult = _SkillResult
SkillStatus = _SkillStatus
# ────────────────────────────────────────────────────────────────────────────

class EquityPledgeSkill:

    @property
    def name(self) -> str:
        return "equity_pledge"

    @property
    def description(self) -> str:
        return "股权质押比例和明细数据"

    @property
    def category(self) -> str:
        return "data_collection"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "ts_code": {"type": "string", "optional": True, "description": "股票代码(不填则查全市场)"}
        }

    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "items": "质押数据列表",
            "columns": "列定义数组",
            "summary": "质押汇总",
            "analysis": "质押风险分析"
        }

    async def execute(self, context: dict) -> dict:
        start_time = datetime.now()
        params = context or {}
        ts_code = context.get("ts_code", "") or params.get("ts_code", "")
        code = ts_code.split('.')[0] if '.' in ts_code else ts_code

        try:
            df = get_equity_pledge(code)
            if df is None or df.empty:
                return SkillResult(
                    status=SkillStatus.ERROR,
                    error="无法获取股权质押数据",
                    execution_time_ms=int((datetime.now() - start_time).total_seconds() * 1000)
                )

            items = df.head(20).to_dict('records')
            columns = [{"key": col, "label": col} for col in df.columns]

            summary = {"total_records": len(df)}
            if ts_code:
                summary["ts_code"] = ts_code
            title = f"{ts_code} 股权质押" if ts_code else "全市场股权质押"
            analysis = f"股权质押数据获取成功，共{len(df)}条记录。"

            elapsed = int((datetime.now() - start_time).total_seconds() * 1000)
            return SkillResult(
                status=SkillStatus.SUCCESS,
                data={
                    "ts_code": ts_code,
                    "title": title,
                    "items": items,
                    "columns": columns,
                    "summary": summary,
                    "analysis": analysis,
                    "data_source": "akshare/equity_mortgage_em"
                },
                message="成功获取股权质押数据",
                execution_time_ms=elapsed
            )
        except Exception as e:
            logger.error(f"EquityPledgeSkill execute error: {e}")
            return SkillResult(
                status=SkillStatus.ERROR, error=str(e),
                execution_time_ms=int((datetime.now() - start_time).total_seconds() * 1000)
            )


def _main():
    """直接执行入口: python3 script.py --param1 value1
    也支持 JSON stdin: echo '{"param1": "v1"}' | python3 script.py
    """
    import argparse
    import asyncio
    import json
    import sys

    params = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read().strip()
            if raw:
                params = json.loads(raw)
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Run EquityPledgeSkill directly")
    parser.add_argument("--ts-code", type=str, dest="ts_code")
    args = parser.parse_args()
    for k, v in vars(args).items():
        if v is not None:
            params[k] = v

    async def run():
        skill = EquityPledgeSkill()
        result = await skill.execute(params)
        out = result if isinstance(result, dict) else {"data": str(result)}
        print(json.dumps(out, ensure_ascii=False, default=str, indent=2))

    asyncio.run(run())


if __name__ == "__main__":
    _main()
