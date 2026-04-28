import json
import os
import sys
from datetime import datetime
from openai import OpenAI

# 尽量确保 Windows 控制台输出为 UTF-8，避免中文乱码/编码异常
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ================= 配置区 =================
# 1. 模型选择：使用千问最强推理与思考模型
# qwen-max 是综合能力最强的旗舰模型；如果想体验纯粹的深度思考模型，可换成 qwq-32b-preview
MODEL_NAME = "qwen-max" 
# =========================================

def load_json_data(file_path):
    """辅助函数：安全读取 JSON 文件"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[WARN] 找不到 {file_path} 文件，将被忽略。")
        return None
    except json.JSONDecodeError:
        print(f"[WARN] {file_path} JSON 格式错误。")
        return None

def load_tongyi_api_key(keys_path="api_keys.local.json"):
    keys = load_json_data(keys_path) or {}
    api_key = (keys.get("tongyi") or {}).get("api_key")
    if not api_key or not isinstance(api_key, str):
        raise RuntimeError(f"未在 {keys_path} 中找到 tongyi.api_key")
    return api_key.strip()

def _today_tag(dt=None):
    dt = dt or datetime.now()
    return dt.strftime("%b_%dth").lower()

def load_today_market_inputs(output_root="output_info", today_tag=None):
    tag = today_tag or _today_tag()

    candidates = {
        "xueqiu": [
            os.path.join(output_root, tag, "today_xueqiu.json"),
            os.path.join(output_root, tag, "xueqiu.json"),
            os.path.join(output_root, f"{tag}_xueqiu.json"),
        ],
        "news": [
            os.path.join(output_root, tag, "news.json"),
            os.path.join(output_root, tag, "today_news.json"),
            os.path.join(output_root, "news", f"{tag}_news.json"),
        ],
        "money_flow": [
            os.path.join(output_root, tag, "money_flow.json"),
            os.path.join(output_root, tag, "today_money_flow.json"),
            os.path.join(output_root, "money_flow", f"{tag}_money.json"),
            os.path.join(output_root, "money_flow", f"{tag}_money_flow.json"),
        ],
    }

    def _first_existing(paths):
        for p in paths:
            if os.path.exists(p):
                return p
        return paths[0]

    xueqiu_path = _first_existing(candidates["xueqiu"])
    news_path = _first_existing(candidates["news"])
    money_flow_path = _first_existing(candidates["money_flow"])

    return (
        load_json_data(xueqiu_path),
        load_json_data(news_path),
        load_json_data(money_flow_path),
        {"tag": tag, "xueqiu_path": xueqiu_path, "news_path": news_path, "money_flow_path": money_flow_path},
    )

def _truncate_text(s, max_len):
    if s is None:
        return None
    if not isinstance(s, str):
        return s
    s = s.strip()
    return s if len(s) <= max_len else (s[: max(0, max_len - 1)] + "…")

def _compact_xueqiu(xueqiu_data, max_items=80, max_content_len=220):
    if not isinstance(xueqiu_data, list):
        return xueqiu_data
    compact = []
    for item in xueqiu_data[:max_items]:
        if not isinstance(item, dict):
            continue
        compact.append(
            {
                "nickname": item.get("nickname") or item.get("blogger_name"),
                "post_time": item.get("post_time"),
                "content": _truncate_text(item.get("content"), max_content_len),
                "source_url": item.get("source_url"),
            }
        )
    return compact

def _compact_news(news_data, max_items=60, max_summary_len=260):
    if not isinstance(news_data, dict):
        return news_data
    compact = {}
    for k, v in news_data.items():
        if isinstance(v, list):
            items = []
            for it in v[:max_items]:
                if not isinstance(it, dict):
                    continue
                items.append(
                    {
                        "tag": it.get("tag"),
                        "summary": _truncate_text(it.get("summary"), max_summary_len),
                        "url": it.get("url"),
                    }
                )
            compact[k] = items
        else:
            compact[k] = v
    return compact

def _compact_money_flow(money_flow_data, max_items=30):
    if not isinstance(money_flow_data, dict):
        return money_flow_data
    keep_keys = [
        "今日最吸金_行业板块Top10",
        "今日最流出_行业板块Top10",
        "今日最吸金_概念板块Top10",
        "今日最流出_概念板块Top10",
        "今日个股_主力净流入Top50",
        "今日个股_主力净流出Top50",
        "机构龙虎榜_净买入Top20",
        "机构龙虎榜_净卖出Top20",
    ]
    compact = {}
    for k in keep_keys:
        if k not in money_flow_data:
            continue
        v = money_flow_data.get(k)
        if isinstance(v, list):
            compact[k] = v[:max_items]
        else:
            compact[k] = v
    if not compact:
        for k, v in money_flow_data.items():
            if isinstance(v, list):
                compact[k] = v[:max_items]
            else:
                compact[k] = v
    return compact

def build_model_payload(xueqiu_data, news_data, money_flow_data, max_chars=28000):
    """
    DashScope(兼容 OpenAI) 对 messages 的输入长度有限制。
    这里做裁剪/压缩，保证 user 内容稳定小于阈值。
    """
    budgets = [
        (80, 60, 30, 220, 260),
        (50, 40, 20, 180, 220),
        (30, 25, 15, 140, 180),
        (20, 15, 10, 120, 160),
    ]

    last_str = None
    last_payload = None
    for x_items, n_items, m_items, x_len, n_len in budgets:
        payload = {
            "主观情绪面(雪球大V发言)": _compact_xueqiu(xueqiu_data, max_items=x_items, max_content_len=x_len)
            if xueqiu_data
            else "无数据",
            "客观消息面(财经快讯)": _compact_news(news_data, max_items=n_items, max_summary_len=n_len) if news_data else "无数据",
            "真实资金面(主力与机构流向)": _compact_money_flow(money_flow_data, max_items=m_items)
            if money_flow_data
            else "无数据",
        }
        s = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        last_str = s
        last_payload = payload
        if len(s) <= max_chars:
            return payload, s

    # 兜底：直接截断字符串（宁可少信息也别报 400）
    if last_str and len(last_str) > max_chars:
        return last_payload, last_str[: max_chars - 1] + "…"
    return last_payload, last_str or "{}"

def analyze_market_data():
    print("开始读取市场三维数据...")
    
    # 1. 读取三个维度的数据
    xueqiu_data, news_data, money_flow_data, meta = load_today_market_inputs()  # 情绪面/消息面/资金面
    print(f"使用数据日期标签：{meta['tag']}")
    print(f"  - 情绪面：{meta['xueqiu_path']}")
    print(f"  - 消息面：{meta['news_path']}")
    print(f"  - 资金面：{meta['money_flow_path']}")

    if not any([xueqiu_data, news_data, money_flow_data]):
        print("[ERROR] 所有数据文件均不存在，请先运行抓取脚本。")
        return None

    # 将数据裁剪为紧凑 JSON，避免触发千问输入长度上限
    _, data_str = build_model_payload(xueqiu_data, news_data, money_flow_data)
    print(f"发送给模型的 payload 大小：{len(data_str)} 字符")

    print(f"正在调用千问模型 [{MODEL_NAME}] 进行三维共振分析，可能需要 1-2 分钟...")
    
    # 初始化 OpenAI 客户端（连接阿里云 DashScope 接口）
    qwen_api_key = load_tongyi_api_key()
    client = OpenAI(
        api_key=qwen_api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1" 
    )

    # 核心：量化基金经理级别的 System Prompt
    system_prompt = """
    你是一位掌管百亿游资与量化基金的顶级策略分析师，拥有极强的逻辑推理能力。
    我将为你提供今日A股市场的“三维切片数据”：
    1.【消息面】：财联社/东方财富的客观快讯。
    2.【情绪面】：雪球核心大V的实盘讨论与观点分歧。
    3.【资金面】：今日主力资金净流入及机构龙虎榜数据。

    请对这三个维度进行严密的“交叉验证（Cross-Validation）”，输出一份排版精美、极具实战指导意义的《明日A股三维共振推演内参》。
    报告必须包含以下四个核心模块：
    模块一：《雪球大V今日市场研判报告》，包含：
    1.【宏观与情绪定调】：总结总体情绪及担忧/期待的宏观因素；
    2.【具体标的异动】：提取集中讨论的个股/板块及对应态度；
    3.【核心认知分歧】：指出专家间的明显分歧点；
    
    模块二：【最强主线（三维共振研判）】
    (思考逻辑：哪些板块同时满足“出政策利好 + 大V热烈讨论 + 主力资金真实大额流入”？)
    请列出今日确定性最高的 5 个主线板块，并详细说明它的催化剂是什么，资金介入程度如何。

    模块二：【致命背离（杀猪盘与踏空预警）】
    (思考逻辑：寻找数据之间的矛盾点！)
    - 危险信号：有没有大V在疯狂吹捧、新闻猛发利好，但资金面（主力流向）却在悄悄大幅净流出的板块？(警惕诱多出货)
    - 潜伏信号：有没有新闻完全没提、大V都在看空或无视，但机构龙虎榜或主力资金却在偷偷大额买入的标的？(底部分歧建仓)

    模块三：🎯 【核心标的资金拆解】
    请提取大V们讨论的具体股票名称，并去【资金面】数据中查验：这些股票今天的主力资金到底是净流入还是净流出？如果有龙虎榜机构席位，请重点标出。给出“真金白银验证后”的重点关注个股名单。

    模块四：📈 【明日操盘剧本】
    用三句简短锐利的话，提炼明早竞价和开盘阶段最需要观察的阵眼（变量）以及整体仓位建议。
    """

    try:
        # 调用大模型
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"请深度分析以下今日市场数据：\n{data_str}"}
            ],
            temperature=0.4 # 调低温度，让模型的推理更严谨、更客观（0.4适合逻辑分析）
        )
        
        report = response.choices[0].message.content
        
        # 保存报告到本地
        with open('report.txt', 'w', encoding='utf-8') as f:
            f.write(report)
        print("\nAI 深度研判完成，已保存为 report.txt。")
        return report

    except Exception as e:
        print(f"[ERROR] 调用通义千问 API 时发生错误：{e}")
        return None

if __name__ == "__main__":
    print("="*50)
    print("启动通义千问【三维共振】深度推演系统")
    print("="*50)
    
    # 1. 思考与分析
    analyze_market_data()