import akshare as ak
import pandas as pd
import json
import time
import sys
import os
from datetime import date

# Windows 控制台常见为 GBK，遇到 emoji/部分字符会触发 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def _day_suffix(day: int) -> str:
    if 11 <= day % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _dated_basename(kind: str, d: date | None = None) -> str:
    d = d or date.today()
    mon = d.strftime("%b").lower()
    return f"{mon}_{d.day}{_day_suffix(d.day)}_{kind}"


def _date_folder(d: date | None = None) -> str:
    d = d or date.today()
    mon = d.strftime("%b").lower()
    return f"{mon}_{d.day}{_day_suffix(d.day)}"


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def fetch_news_data():
    """
    获取消息面数据：尽量抓取“快讯/资讯”
    """
    print("正在抓取【消息面】数据 (财联社、东方财富)...")
    news_data = {}
    
    # 1. 获取“财联社快讯”替代源：财新要闻
    try:
        df_cx = ak.stock_news_main_cx()
        cx_list = df_cx.head(30).to_dict(orient="records")
        news_data["财新最新快讯(替代财联社)"] = cx_list
        print("   财新数据获取成功(替代财联社)")
    except Exception as e:
        print(f"   财新数据获取失败(替代财联社): {e}")

    # 2. 获取东方财富资讯
    try:
        df_em = ak.stock_news_em()
        em_list = df_em[["发布时间", "新闻标题", "新闻内容", "文章来源", "新闻链接"]].head(30).to_dict(orient="records")
        news_data["东方财富最新资讯"] = em_list
        print("   东方财富数据获取成功")
    except Exception as e:
        print(f"   东方财富数据获取失败: {e}")

    # 保存为 JSON 文件
    out_dir = os.path.join("output_info", _date_folder())
    _ensure_dir(out_dir)
    out_path = os.path.join(out_dir, "news_data.json")
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(news_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"消息面数据已保存至: {out_path}\n")


def fetch_fund_flow_data():
    """
    获取资金面数据：行业板块资金、个股主力资金、机构龙虎榜买卖
    """
    print("正在抓取【资金面】数据 (主力资金、龙虎榜)...")
    fund_data = {}

    # 1. 行业板块资金流向排名 (今日最吸金的行业)
    try:
        df_sector = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
        # 默认按净流入额从高到低排序，提取前 10 名
        # AkShare 返回的列较多，我们全部保留转为字典发给AI即可
        sector_top10 = df_sector.head(10).to_dict(orient='records')
        fund_data['今日最吸金_行业板块Top10'] = sector_top10
        print("   行业板块资金获取成功")
    except Exception as e:
        print(f"   行业板块资金获取失败: {e}")

    # 2. 个股主力资金流向排名 (今日主力狂买的个股)
    try:
        df_stock = ak.stock_individual_fund_flow_rank(indicator="今日")
        # 提取主力净流入排名前 20 的个股
        stock_top20 = df_stock.head(20).to_dict(orient='records')
        fund_data['今日主力狂买_个股Top20'] = stock_top20
        print("   个股主力资金获取成功")
    except Exception as e:
        print(f"   个股主力资金获取失败: {e}")

    # 3. 机构龙虎榜每日统计 (机构真金白银买入席位)
    try:
        # 获取近期的机构席位买卖明细（包含个股名称、机构买入净额等）
        df_lhb = ak.stock_lhb_jgmmtj_em() 
        # 提取前 15 条
        lhb_top15 = df_lhb.head(15).to_dict(orient='records')
        fund_data['近期机构龙虎榜_净买入情况'] = lhb_top15
        print("   机构龙虎榜数据获取成功")
    except Exception as e:
        print(f"   机构龙虎榜数据获取失败: {e}")

    # 保存为 JSON 文件
    out_dir = os.path.join("output_info", _date_folder())
    _ensure_dir(out_dir)
    out_path = os.path.join(out_dir, "money_flow.json")
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(fund_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"资金面数据已保存至: {out_path}\n")


if __name__ == "__main__":
    print("="*40)
    print("市场全维数据抓取脚本启动")
    print("="*40)
    
    fetch_news_data()
    # 稍微休眠 2 秒，防止接口请求过于频繁
    time.sleep(2) 
    fetch_fund_flow_data()
    
    print("所有数据抓取完毕！你可以将 json 提交给大模型了。")