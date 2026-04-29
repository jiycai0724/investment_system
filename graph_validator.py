import json
import os
import re
import sys
import time
import random
from datetime import datetime

import akshare as ak
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ================= 工具函数 =================

def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def parse_stock_entry(entry: str):
    """
    解析 focus_stocks 条目，支持以下格式：
      - "天齐锂业(002466)"   → name="天齐锂业", code="002466"
      - "联想集团(00992)"    → name="联想集团",  code="00992"
      - "天齐锂业"           → name="天齐锂业",  code=None
    返回 (name, code)，code 为 None 表示未内嵌代码。
    """
    m = re.match(r"^(.+?)\(([A-Za-z0-9\.]+)\)\s*$", entry.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return entry.strip(), None


def is_a_share(code: str) -> bool:
    """判断是否为 A 股 6 位数字代码。"""
    return bool(code and re.fullmatch(r"\d{6}", code))


def load_focus_stocks(report_dir="daily_report", date_str=None):
    """
    读取当天的 focus_stocks JSON，返回 [(name, code), ...] 列表。
    code 可能为 None（JSON 中只有名称无代码）。
    """
    date_str = date_str or today_str()
    path = os.path.join(report_dir, f"{date_str}_focus_stocks.json")
    if not os.path.exists(path):
        print(f"[ERROR] 找不到 {path}，请先运行 analyze_and_push.py 生成报告。")
        return [], path
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("focus_stocks", [])
    stocks = [parse_stock_entry(s) for s in raw if isinstance(s, str)]
    print(f"[INFO] 读取到 {len(stocks)} 只关注个股：{[f'{n}({c})' if c else n for n, c in stocks]}")
    return stocks, path


# ================= MACD 计算核心 =================

def compute_macd(series: pd.Series, fast=12, slow=26, signal=9):
    """
    标准 MACD 计算。
    返回 DataFrame，包含列：DIF、DEA、MACD（柱状值 = (DIF-DEA)*2）。
    """
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    macd_bar = (dif - dea) * 2
    return pd.DataFrame({"DIF": dif, "DEA": dea, "MACD": macd_bar})


def _sina_prefix(code: str) -> str:
    """新浪接口需要 'sh'/'sz'/'bj' 前缀。"""
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


_SINA_PERIOD = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}


def _fetch_sina(stock_code, period):
    """通过新浪财经接口拉取 K 线（stock_zh_a_daily 仅支持日线）。"""
    if period != "daily":
        raise ValueError("新浪接口仅支持日线")
    symbol = _sina_prefix(stock_code)
    df = ak.stock_zh_a_daily(symbol=symbol, adjust="qfq")
    if df is None or df.empty:
        raise ValueError("返回数据为空")
    # 统一列名为东方财富格式
    df = df.rename(columns={"date": "日期", "open": "开盘", "high": "最高",
                             "low": "最低", "close": "收盘", "volume": "成交量"})
    df["日期"] = df["日期"].astype(str)
    return df


def _fetch_em(stock_code, period):
    """通过东方财富接口拉取 K 线。"""
    df = ak.stock_zh_a_hist(symbol=stock_code, period=period, adjust="qfq")
    if df is None or df.empty:
        raise ValueError("返回数据为空")
    return df


def _fetch_with_retry(stock_code, period, max_retries=3, base_delay=2.0):
    """
    优先走新浪财经（日线），周线/月线走东方财富。
    每次请求前随机等待 5~8 秒，失败后指数退避重试。
    """
    fetchers = []
    if period == "daily":
        # 日线：先试新浪，再试东方财富
        fetchers = [("新浪", lambda: _fetch_sina(stock_code, period)),
                    ("东财", lambda: _fetch_em(stock_code, period))]
    else:
        # 周线/月线：只有东方财富支持
        fetchers = [("东财", lambda: _fetch_em(stock_code, period))]

    last_err = None
    for source_name, fetcher in fetchers:
        for attempt in range(1, max_retries + 1):
            try:
                time.sleep(random.uniform(5.0, 8.0))
                return fetcher()
            except Exception as e:
                last_err = e
                if attempt < max_retries:
                    backoff = base_delay * (2 ** attempt) + random.uniform(0, 1)
                    print(f"    [RETRY {attempt}/{max_retries}][{source_name}] {period} 失败，{backoff:.1f}s 后重试：{e}")
                    time.sleep(backoff)
                else:
                    print(f"    [FAIL][{source_name}] {period} 全部重试失败：{e}")

    raise RuntimeError(f"所有数据源均请求失败：{last_err}")


def analyze_macd_for_period(stock_code, period="daily"):
    """获取指定周期 K 线并计算 MACD，返回 (近3根明细df, 状态描述文本)。"""
    period_map = {"daily": "日线", "weekly": "周线", "monthly": "月线"}
    label = period_map.get(period, period)

    try:
        df = _fetch_with_retry(stock_code, period)
        if df is None or df.empty or len(df) < 35:
            return None, f"{label}：数据不足，无法计算"

        macd_df = compute_macd(df["收盘"])
        df = df.join(macd_df)

        last3 = df.tail(3)[["日期", "收盘", "DIF", "DEA", "MACD"]].copy()
        latest = df.iloc[-1]
        prev = df.iloc[-2]

        dif_now, dea_now, macd_now = latest["DIF"], latest["DEA"], latest["MACD"]
        dif_prev, dea_prev = prev["DIF"], prev["DEA"]

        just_golden = (dif_now > dea_now) and (dif_prev <= dea_prev)
        just_dead   = (dif_now < dea_now) and (dif_prev >= dea_prev)
        above_zero  = dif_now > 0 and dea_now > 0

        if just_golden:
            cross_status = "🔥 刚刚金叉"
        elif just_dead:
            cross_status = "❄️ 刚刚死叉"
        elif dif_now > dea_now:
            cross_status = "✅ 多头排列（DIF>DEA）"
        else:
            cross_status = "🔻 空头排列（DIF<DEA）"

        zone = "零轴上方" if above_zero else "零轴下方"
        summary = (
            f"{label}：{cross_status}，位于{zone}\n"
            f"    DIF={dif_now:+.4f}  DEA={dea_now:+.4f}  MACD柱={macd_now:+.4f}"
        )
        return last3, summary

    except Exception as e:
        return None, f"{label}：计算出错 ({e})"


def analyze_stock(name, code):
    """对单只 A 股做日/周/月三周期 MACD 分析，返回格式化文本块。"""
    lines = [f"\n{'='*50}", f"【{name}】({code})", f"{'='*50}"]

    daily_detail,  daily_summary  = analyze_macd_for_period(code, "daily")
    weekly_detail, weekly_summary = analyze_macd_for_period(code, "weekly")
    monthly_detail,monthly_summary= analyze_macd_for_period(code, "monthly")

    lines += [daily_summary, weekly_summary, monthly_summary]

    if daily_detail is not None:
        lines.append("\n  近 3 根日线数据：")
        for _, row in daily_detail.iterrows():
            lines.append(
                f"    {row['日期']}  收盘={row['收盘']:.2f}"
                f"  DIF={row['DIF']:+.4f}  DEA={row['DEA']:+.4f}  MACD柱={row['MACD']:+.4f}"
            )

    is_daily_bull   = any(k in daily_summary   for k in ("多头", "金叉"))
    is_weekly_bull  = any(k in weekly_summary  for k in ("多头", "金叉"))
    is_monthly_bull = any(k in monthly_summary for k in ("多头", "金叉"))
    bull_count = sum([is_daily_bull, is_weekly_bull, is_monthly_bull])

    if bull_count == 3:
        rating = "⭐⭐⭐ 三周期共振多头，强势"
    elif bull_count == 2:
        rating = "⭐⭐ 中长线偏多，短线观察"
    elif bull_count == 1:
        rating = "⭐ 仅短线偏多，谨慎参与"
    else:
        rating = "❌ 全周期空头，暂时回避"

    lines.append(f"\n  综合评级：{rating}")
    return "\n".join(lines)


# ================= 主流程 =================

def run_macd_validation(report_dir="daily_report"):
    print("=" * 55)
    print("  MACD 量化验证引擎启动")
    print("=" * 55)

    date_str = today_str()
    stocks, src_path = load_focus_stocks(report_dir, date_str)
    if not stocks:
        return

    results = []
    for name, code in stocks:
        if not code:
            results.append(f"\n【{name}】: focus_stocks 中未包含股票代码，已跳过。")
            print(f"  [SKIP] {name} 无代码")
            continue

        if not is_a_share(code):
            results.append(
                f"\n{'='*50}\n【{name}】({code})\n{'='*50}\n"
                f"  非 A 股代码（港股/其他），当前版本暂不支持 MACD 计算。"
            )
            print(f"  [SKIP] {name}({code}) 非 A 股，跳过")
            continue

        print(f"  正在分析 {name}({code}) 三周期 MACD...")
        results.append(analyze_stock(name, code))

    header = (
        f"MACD 量化验证报告\n"
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"来源：{src_path}\n"
        f"{'=' * 55}\n"
    )
    report_text = header + "\n".join(results)

    out_file = os.path.join(report_dir, f"{date_str}_macd.txt")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(report_text)

    print(f"\n[OK] MACD 验证报告已保存：{out_file}")
    print("\n" + report_text)


if __name__ == "__main__":
    run_macd_validation()
