# -*- coding: utf-8 -*-
"""
定向抓取雪球博主主页（Playwright sync_api 版本）
功能说明：
1. 使用 launch_persistent_context 接管本地 Chrome 用户数据（免登录）
2. 循环访问 urls 中的博主主页
3. 每个主页抓取最新 15~20 条帖子：博主昵称、发帖时间、正文内容
4. 页面内滚动 2~3 次，每次滚动后随机休眠 2~4 秒（强制反爬）
5. 抓完一个博主后，访问下一个前随机休眠 10~15 秒（强制反爬）
6. 控制台打印抓取进度
7. 合并所有博主数据并保存为 today_xueqiu.json
"""

import json
import random
import time
from datetime import date
from pathlib import Path
import re
from typing import Dict, List, TypedDict

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# =========================
# 你需要修改的配置项
# =========================

# 1) 本地 Chrome 用户数据目录配置文件（仅一行路径）
CHROME_DATA_FILE = Path(__file__).resolve().parent / "chrom_data"

# 2) 博主主页链接列表文件（每行一个链接，支持 # 注释）
BLOGGER_LIST_FILE = Path(__file__).resolve().parent / "blogger_list"

# 每个博主抓取的帖子数量（建议 15~20）
TARGET_POST_COUNT = 20


def _day_suffix(day: int) -> str:
    if 11 <= day % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _date_folder(d: date | None = None) -> str:
    d = d or date.today()
    mon = d.strftime("%b").lower()
    return f"{mon}_{d.day}{_day_suffix(d.day)}"


def _output_day_dir(base_dir: Path | None = None, d: date | None = None) -> Path:
    root = (base_dir or Path(__file__).resolve().parent.parent) / "output_info" / _date_folder(d)
    root.mkdir(parents=True, exist_ok=True)
    return root


def load_user_data_dir(config_file: Path) -> str:
    """
    从文件读取 Chrome 用户数据目录。
    """
    if not config_file.exists():
        raise FileNotFoundError(f"未找到配置文件：{config_file}")

    raw = config_file.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError(f"{config_file} 为空，请填写你的 Chrome User Data 路径。")

    # 允许用户误把“赋值语句”写进配置文件里：如 user_data_dir = r"..."
    # 优先从引号中提取第一段内容
    value = raw
    for quote in ('"', "'"):
        if quote in raw:
            first = raw.split(quote, 1)[1]
            if quote in first:
                value = first.split(quote, 1)[0]
            break

    value = value.strip()
    if not value:
        raise ValueError(f"{config_file} 内容无效，请仅填写路径或写成 user_data_dir = r\"...\"。")

    return value


class BloggerItem(TypedDict, total=False):
    name: str
    link: str


def load_bloggers(list_file: Path) -> List[BloggerItem]:
    """
    从文件读取要抓取的博主列表（推荐 JSON: [{name, link}, ...]）。
    兼容两种格式：
    1) JSON 数组：每项包含 name/link（name 可选）
    2) 纯文本：从文本中正则提取所有 URL（支持每行一个、同一行多个、# 注释）
    """
    if not list_file.exists():
        raise FileNotFoundError(f"未找到链接文件：{list_file}")

    text = list_file.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"{list_file} 为空，请填写博主列表。")

    # 优先尝试 JSON
    if text.lstrip().startswith("["):
        try:
            arr = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"{list_file} JSON 解析失败：{e.msg}（行 {e.lineno} 列 {e.colno}）。"
                "请检查是否缺少逗号或引号。"
            ) from e

        if not isinstance(arr, list) or not arr:
            raise ValueError(f"{list_file} 必须是非空 JSON 数组。")

        out: List[BloggerItem] = []
        for idx, item in enumerate(arr, start=1):
            if isinstance(item, str):
                link = item.strip()
                if link:
                    out.append({"link": link})
                continue

            if not isinstance(item, dict):
                raise ValueError(f"{list_file} 第 {idx} 项必须是对象或字符串链接。")

            link = str(item.get("link", "")).strip()
            name = str(item.get("name", "")).strip()
            if not link:
                raise ValueError(f"{list_file} 第 {idx} 项缺少 link。")
            out.append({"name": name, "link": link})

        return out

    # 纯文本模式：去掉注释后提取 URL
    cleaned_lines = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            cleaned_lines.append(line)
    cleaned_text = "\n".join(cleaned_lines)

    candidates = re.findall(r"https?://[^\s,;]+", cleaned_text, flags=re.IGNORECASE)
    if not candidates:
        raise ValueError(f"{list_file} 中没有有效链接，请至少填写一个主页 URL。")

    out: List[BloggerItem] = []
    seen = set()
    for u in candidates:
        u = u.strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append({"link": u})
    return out


def random_sleep(min_sec: int, max_sec: int, reason: str = "") -> None:
    """
    随机休眠，满足反爬策略。
    :param min_sec: 最小秒数
    :param max_sec: 最大秒数
    :param reason: 控制台提示用途
    """
    sec = random.uniform(min_sec, max_sec)
    if reason:
        print(f"[休眠] {reason}，暂停 {sec:.2f} 秒...")
    time.sleep(sec)


def extract_posts_from_page(page) -> Dict:
    """
    从当前博主主页提取昵称 + 最新帖子列表。
    这里使用 JS 在浏览器上下文中做多选择器兼容提取，尽量减少因页面结构微调导致的失效。
    """
    data = page.evaluate(
        """
        () => {
            // ---------- 工具函数 ----------
            const cleanText = (t) => (t || "")
                .replace(/\\s+/g, " ")
                .replace(/\\u200b/g, "")
                .trim();

            const pickFirstText = (selectors) => {
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const txt = cleanText(el.textContent);
                        if (txt) return txt;
                    }
                }
                return "";
            };

            const pickTextFromNode = (node, selectors) => {
                for (const sel of selectors) {
                    const el = node.querySelector(sel);
                    if (el) {
                        const txt = cleanText(el.textContent);
                        if (txt) return txt;
                    }
                }
                return "";
            };

            const firstBySelectorsIn = (node, selectors) => {
                for (const sel of selectors) {
                    const el = node.querySelector(sel);
                    if (el) return el;
                }
                return null;
            };

            const isLikelyTimeText = (t) => {
                const s = cleanText(t);
                if (!s) return false;
                // 排除常见 UI 文案/计数
                const bad = [
                    "转发", "评论", "赞", "收起", "展开", "查看对话", "更多",
                    "置顶", "推荐", "关注", "私信"
                ];
                if (bad.some(k => s.includes(k))) return false;

                // 时间常见形态（尽量宽松，但要能覆盖“xx分钟前/今天/昨天/04-27/2026-04-27 15:30”等）
                const patterns = [
                    /^\\d{4}-\\d{1,2}-\\d{1,2}(\\s+\\d{1,2}:\\d{2})?$/,
                    /^\\d{1,2}-\\d{1,2}(\\s+\\d{1,2}:\\d{2})?$/,
                    /^\\d{1,2}:\\d{2}$/,
                    /^(刚刚|\\d+\\s*秒前|\\d+\\s*分钟前|\\d+\\s*小时前|\\d+\\s*天前)$/,
                    /^(今天|昨天|前天)(\\s+\\d{1,2}:\\d{2})?$/
                ];
                return patterns.some(r => r.test(s));
            };

            const pickTimeText = (node) => {
                // 优先：time[datetime]
                const timeEl = node.querySelector("time");
                if (timeEl) {
                    const dt = timeEl.getAttribute("datetime");
                    if (dt) {
                        const dtShort = cleanText(dt.replace("T", " ").replace("Z", ""));
                        if (isLikelyTimeText(dtShort)) return dtShort;
                    }
                    const t = cleanText(timeEl.textContent);
                    if (isLikelyTimeText(t)) return t;
                }

                // 次优：发布链接（雪球很多帖子时间在 /S/ 链接上，title 里是完整时间）
                const sLink = node.querySelector("a[href*='/S/']");
                if (sLink) {
                    const title = cleanText(sLink.getAttribute("title") || "");
                    if (isLikelyTimeText(title)) return title;
                    const t = cleanText(sLink.textContent);
                    if (isLikelyTimeText(t)) return t;
                }

                // 兜底：查找疑似 time 的 class，但要过格式校验
                const candidates = node.querySelectorAll(
                    "a[class*='time'], span[class*='time'], div[class*='time'], [datetime]"
                );
                for (const el of candidates) {
                    const dt = cleanText(el.getAttribute?.("datetime") || "");
                    if (dt) {
                        const dtShort = cleanText(dt.replace("T", " ").replace("Z", ""));
                        if (isLikelyTimeText(dtShort)) return dtShort;
                    }
                    const title = cleanText(el.getAttribute?.("title") || "");
                    if (isLikelyTimeText(title)) return title;
                    const t = cleanText(el.textContent);
                    if (isLikelyTimeText(t)) return t;
                }

                return "";
            };

            const findQuoteBlock = (node) => {
                // 尽量用“结构/语义”定位转发/引用原帖区块（雪球常见为背景色引用框）
                const candidates = [
                    "blockquote",
                    "div[class*='quote']",
                    "div[class*='quoted']",
                    "div[class*='repost']",
                    "div[class*='retweet']",
                    "div[class*='forward']",
                    "div[class*='origin']",
                    "div[class*='source']",
                    "div[class*='card'] div[class*='sub']",
                    "div[class*='reference']",
                    "section[class*='quote']",
                ];
                for (const sel of candidates) {
                    const el = node.querySelector(sel);
                    if (el && cleanText(el.textContent).length >= 6) return el;
                }
                return null;
            };

            const pickOwnTextOutsideQuote = (node, quoteEl) => {
                // 先找“更像正文”的容器，并确保它不在 quote 内部
                const contentSelectors = [
                    "div[class*='content']",
                    "div[class*='text']",
                    "div[class*='desc']",
                    "section",
                    "p",
                ];
                for (const sel of contentSelectors) {
                    const list = node.querySelectorAll(sel);
                    for (const el of list) {
                        if (!el) continue;
                        if (quoteEl && quoteEl.contains(el)) continue;
                        const txt = cleanText(el.textContent);
                        if (txt && txt.length >= 2) return txt;
                    }
                }

                // 兜底：用整条文本减去 quote 文本（可能不完美，但比完全丢原帖强）
                const full = cleanText(node.textContent);
                if (!quoteEl) return full;
                const q = cleanText(quoteEl.textContent);
                if (!q) return full;
                if (full.includes(q)) {
                    return cleanText(full.replace(q, " "));
                }
                return full;
            };

            const extractQuotedPost = (quoteEl) => {
                if (!quoteEl) return { author: "", content: "" };

                // 原作者昵称：优先找指向用户页的链接或 name 样式
                const authorEl = firstBySelectorsIn(quoteEl, [
                    "a[href^='https://xueqiu.com/u/']",
                    "a[href^='/u/']",
                    "a[href*='/u/']",
                    "a[class*='user']",
                    "span[class*='user']",
                    "span[class*='name']",
                    "div[class*='name']",
                ]);
                const author = authorEl ? cleanText(authorEl.textContent) : "";

                // 原帖正文：优先在 quote 内找正文容器
                const quotedContent = pickTextFromNode(quoteEl, [
                    "div[class*='content']",
                    "div[class*='text']",
                    "div[class*='desc']",
                    "p",
                    "section",
                    "article",
                    "div",
                ]);

                return { author, content: quotedContent };
            };

            // ---------- 提取博主昵称 ----------
            // 多套候选选择器，兼容雪球页面可能的不同结构
            let nickname = pickFirstText([
                "h1",
                ".user-name",
                ".profile__name",
                ".profile-name",
                "[class*='user'] h1",
                "[class*='profile'] h1",
                "[class*='name']"
            ]);

            // 如果昵称仍为空，退化为页面标题前半段
            if (!nickname) {
                const title = cleanText(document.title || "");
                nickname = title.split("-")[0]?.trim() || title || "未知博主";
            }

            // ---------- 提取帖子节点 ----------
            // 常见卡片容器的候选选择器（取并集）
            const cardSelectors = [
                "article",
                "div.timeline__item",
                "li.timeline__item",
                "div[class*='status-item']",
                "div[class*='feed-item']",
                "div[class*='tweet']",
                "div[class*='timeline-item']"
            ];

            const nodeSet = new Set();
            for (const sel of cardSelectors) {
                document.querySelectorAll(sel).forEach(n => nodeSet.add(n));
            }

            const allNodes = Array.from(nodeSet);

            // ---------- 从每个卡片中抽取时间和正文 ----------
            const posts = [];
            const seen = new Set();

            for (const node of allNodes) {
                const timeText = pickTimeText(node);

                const contentText = pickTextFromNode(node, [
                    "div[class*='content']",
                    "div[class*='text']",
                    "div[class*='desc']",
                    "p",
                    "section",
                    "article",
                    "div"
                ]);

                // 基本过滤：正文太短通常不是有效帖子
                if (!contentText || contentText.length < 8) continue;

                // ---------- 转发/引用贴兼容处理 ----------
                // 先提取博主自己的发言，再判断是否包含引用原帖区块
                const quoteEl = findQuoteBlock(node);
                const ownText = pickOwnTextOutsideQuote(node, quoteEl);

                let finalContent = ownText;
                if (quoteEl) {
                    const q = extractQuotedPost(quoteEl);
                    const quotedText = cleanText(q.content);
                    const authorText = cleanText(q.author);
                    if (quotedText) {
                        finalContent = `博主发言：${ownText || ""} || 转发原帖：[@${authorText || "原作者"}：${quotedText}]`;
                    }
                }

                // 有些卡片可能抓不到时间，允许为空但优先保留有时间的
                const key = `${timeText}__${finalContent}`;
                if (seen.has(key)) continue;
                seen.add(key);

                posts.push({
                    post_time: timeText || "",
                    content: finalContent || contentText
                });
            }

            return {
                nickname,
                posts
            };
        }
        """
    )

    return data


def crawl_xueqiu_homepages() -> List[Dict]:
    """
    主爬虫函数：循环访问 urls，抓取并汇总数据。
    """
    all_results: List[Dict] = []

    user_data_dir = load_user_data_dir(CHROME_DATA_FILE)
    bloggers = load_bloggers(BLOGGER_LIST_FILE)
    print(f"[配置] CHROME_DATA_FILE = {CHROME_DATA_FILE}")
    print(f"[配置] BLOGGER_LIST_FILE = {BLOGGER_LIST_FILE}")
    print(f"[配置] user_data_dir = {user_data_dir}")
    print(f"[配置] urls_count = {len(bloggers)}")
    print(f"[配置] first_url = {bloggers[0].get('link', '') if bloggers else ''}")

    # 确保目录存在（不强制创建，避免误配置）
    if not Path(user_data_dir).exists():
        raise FileNotFoundError(f"user_data_dir 不存在：{user_data_dir}")

    with sync_playwright() as p:
        # 关键点：使用 persistent context 复用本地 Chrome 登录态
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            channel="chrome",        # 指定使用本机 Chrome
            headless=False,          # 首次建议 False，便于观察和排错；稳定后可改 True
            viewport={"width": 1400, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        )

        try:
            total = len(bloggers)

            for idx, b in enumerate(bloggers, start=1):
                url = (b.get("link") or "").strip()
                expected_name = (b.get("name") or "").strip()
                page = context.new_page()

                print(f"开始访问第 {idx}/{total} 个主页：{url}")

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)

                    # 等页面再稳定一点（有些内容异步加载）
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except PlaywrightTimeoutError:
                        # 即使 networkidle 超时，也继续执行，避免卡死
                        pass

                    # 页面内随机滚动 2~3 次，每次后强制休眠 5~10 秒
                    scroll_times = random.randint(2, 3)
                    for s in range(scroll_times):
                        page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.85));")
                        random_sleep(5, 10, f"第 {idx}/{total} 个博主滚动 {s+1}/{scroll_times}")

                    # 提取昵称和帖子
                    parsed = extract_posts_from_page(page)
                    nickname = (parsed.get("nickname") or "").strip() or f"未知博主_{idx}"
                    display_name = expected_name or nickname

                    print(f"正在抓取 {display_name}... {idx}/{total}")

                    posts = parsed.get("posts", [])

                    # 仅保留最新 15~20 条：
                    # - 如果 >= 20，取前 20
                    # - 如果 15~19，全部保留
                    # - 如果 < 15，按实际数量保留（页面可能帖子较少或反爬导致）
                    if len(posts) >= TARGET_POST_COUNT:
                        posts = posts[:TARGET_POST_COUNT]
                    elif len(posts) > 20:
                        posts = posts[:20]

                    # 统一写入结构
                    for p_item in posts:
                        all_results.append(
                            {
                                "nickname": nickname,
                                "blogger_name": expected_name,
                                "post_time": p_item.get("post_time", ""),
                                "content": p_item.get("content", ""),
                                "source_url": url,
                            }
                        )

                    print(f"{display_name} 抓取完成，共提取 {len(posts)} 条帖子。")

                except Exception as e:
                    print(f"[异常] 抓取失败（{url}）：{e}")

                finally:
                    try:
                        if not page.is_closed():
                            page.close()
                    except Exception as e:
                        # 个别情况下页面/上下文被提前关闭，避免因此中断整个批量任务
                        print(f"[警告] 关闭页面失败，将继续下一位博主：{e}")

                # 抓完一个博主，在访问下一个前强制随机休眠 10~15 秒（最后一个可不休眠）
                if idx < total:
                    random_sleep(10, 15, f"准备抓取下一个博主（{idx+1}/{total}）")

        finally:
            try:
                context.close()
            except Exception as e:
                print(f"[警告] 关闭浏览器上下文失败：{e}")

    return all_results


def save_to_json(data: List[Dict], output_file: str | Path = "today_xueqiu.json") -> Path:
    """
    保存抓取结果到 JSON 文件。
    """
    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"已保存 {len(data)} 条数据到 {out_path}")
    return out_path


def save_grouped_daily_output(data: List[Dict], base_dir: Path | None = None) -> Path:
    """
    按抓取日期输出到 output_info/<date>/xueqiu_posts.txt，并按博主分组，博主之间用分隔符隔开。
    """
    out_root = _output_day_dir(base_dir=base_dir)

    # 按博主分组（保留原有顺序：先出现的博主先写）
    grouped: Dict[str, List[Dict]] = {}
    order: List[str] = []
    for item in data:
        group_name = (item.get("blogger_name") or "").strip() or (item.get("nickname") or "未知博主").strip() or "未知博主"
        if group_name not in grouped:
            grouped[group_name] = []
            order.append(group_name)
        grouped[group_name].append(item)

    out_file = out_root / "xueqiu_posts.txt"
    with out_file.open("w", encoding="utf-8") as f:
        for i, nick in enumerate(order, start=1):
            f.write(f"==================== {i}. {nick} ====================\n")
            for p in grouped[nick]:
                t = (p.get("post_time") or "").strip()
                c = (p.get("content") or "").strip()
                src = (p.get("source_url") or "").strip()
                if t:
                    f.write(f"[时间] {t}\n")
                if src:
                    f.write(f"[主页] {src}\n")
                f.write(f"{c}\n")
                f.write("\n")

            # 博主之间隔开
            f.write("\n\n")

    print(f"已保存分组文本到 {out_file}")
    return out_file


if __name__ == "__main__":
    """
    运行入口：
    1) pip install playwright
    2) playwright install
    3) 在 chrom_data 填写 Chrome User Data 路径（仅 1 行）
    4) 在 blogger_list 按行填写博主主页链接
    5) python xxx.py
    """
    result = crawl_xueqiu_homepages()
    out_root = _output_day_dir()
    save_to_json(result, out_root / "today_xueqiu.json")
    save_grouped_daily_output(result, base_dir=Path(__file__).resolve().parent.parent)