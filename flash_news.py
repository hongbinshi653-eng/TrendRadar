#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全球财经快讯（两级推送）

    · 重要新闻（事件级别 >= IMPORTANT_LEVEL）：立即推送一条「重要财经快讯」
    · 中低新闻：仅 DIGEST_SOURCES（中文财经源）进入待汇总池，每 12 小时汇总成一条「财经摘要」；
      其余来源的中低条目直接丢弃（英文 RSS 只走「重要」实时推）

重要性判定 = 事件类型分级（主）+ 多源共振（辅），说明见下方常量区。

两条消息都走企业微信 Webhook；同一轮里若没有重要新闻，则不打扰。

状态文件（默认 .flash_state.json，GitHub Actions 用 actions/cache 持久化）保存：
    sent          已推送的条目（去重用）
    pending       待汇总的中低条目池
    next_digest_at  下一次汇总时间

用法：
    python flash_news.py --dry-run                 # 预览两类消息，不发送、不写状态
    python flash_news.py --push-wework             # 抓取并按两级策略推送
    python flash_news.py --push-wework --digest-now # 强制立刻汇总一次（测试用）
    python flash_news.py --reset                   # 清空状态
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from wework import push_wework

NEWSNOW_API = "https://newsnow.busiyi.world/api/s?id={id}&latest"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
STATE_KEEP_DAYS = 7
MAX_BYTES = 3800  # 企业微信 markdown 单条上限 4096 字节，留出余量

# 实时财经源（NewsNow 平台，均已实测可用）
NEWSNOW_SOURCES = [
    ("jin10", "金十数据"),
    ("wallstreetcn-hot", "华尔街见闻"),
    ("cls-hot", "财联社"),
    ("gelonghui", "格隆汇"),
    ("xueqiu", "雪球"),
    ("fastbull", "FastBull"),
    ("mktnews", "MKTNews"),
    ("zaobao", "联合早报"),
]

# 国际财经 RSS 源（均已实测可用）
RSS_SOURCES = [
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("CNBC World", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("WSJ Markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("Investing.com", "https://www.investing.com/rss/news.rss"),
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; flash-news/1.0)"}

# ---------------------------------------------------------------------------
# 重要性判定 = A 事件类型分级（主） + B 多源共振（辅）
#
#   A. 事件类型分级：标题命中「高影响事件」记 2 级，命中「中影响事件」记 1 级，都未命中记 0 级。
#      只取命中的最高级、不做累加 —— 从根上避免长标题堆分，以及“欧央行”重复命中“央行”。
#      英文词按词边界匹配，避免 oil 命中 boiling、war 命中 warning。
#   B. 多源共振：同一事件被 >= 2 个来源报道时，级别 +1（封顶 2 级）。
#      依据：跨源覆盖度比“来源是否权威”更客观。
#
#   级别 >= IMPORTANT_LEVEL 判为「重要」立即推送，其余进入汇总池。
#   来源不参与定级，只用于“同一事件选谁做代表”，见 SOURCE_PRIORITY。
# ---------------------------------------------------------------------------
IMPORTANT_LEVEL = 2

# 高影响事件类型（级别 2）：货币政策 / 通胀就业 / 贸易地缘 / 系统性风险
EVENT_HIGH = [
    "美联储", "fomc", "federal reserve", "鲍威尔", "powell",
    "利率决议", "加息", "降息", "rate hike", "rate cut", "欧央行", "ecb",
    "日本央行", "boj", "英国央行", "boe", "降准", "mlf",
    "cpi", "pce", "通胀", "inflation", "非农", "nonfarm", "nfp",
    "失业率", "unemployment", "gdp",
    "关税", "tariff", "制裁", "sanction", "出口管制", "export control",
    "战争", "war", "冲突升级", "停火",
    "熔断", "circuit breaker", "主权违约", "债务上限", "债务危机",
    "银行倒闭", "暴跌",
]

# 中影响事件类型（级别 1）：景气数据 / 大宗商品 / 市场与信用
EVENT_MEDIUM = [
    "央行", "central bank", "lpr", "pmi", "零售销售", "retail sales",
    "贸易帐", "trade balance", "工业产出", "industrial production",
    "财报", "earnings", "评级", "rating", "破产", "债务", "停摆", "shutdown",
    "油价", "原油", "crude", "oil", "黄金", "gold", "汇率", "人民币",
    "美元", "美股", "国债", "yield", "冲突",
]

# 同一事件的标题相似度阈值（3-gram Jaccard）；过短标题只做完全相同判定
SIM_THRESHOLD = 0.35
MIN_SIM_LEN = 6

# 来源仅用于“同一事件挑代表”与“是否进汇总池”，不参与重要性定级
SOURCE_PRIORITY = {
    "金十数据": 5, "华尔街见闻": 5, "财联社": 5, "格隆汇": 4,
    "MKTNews": 3, "FastBull": 3, "联合早报": 3,
    "BBC Business": 2, "CNBC World": 2, "MarketWatch": 2, "WSJ Markets": 2,
    "雪球": 1, "Investing.com": 1,
}

# 进入「中低汇总池」的来源（其余来源的中低条目直接丢弃，避免汇总过载）
DIGEST_SOURCES = {"金十数据", "华尔街见闻", "财联社", "格隆汇"}


def norm_title(text: str) -> str:
    """规范化标题：去掉标点/空格并转小写，用于近似去重。"""
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]", "", text).lower()


def _compile_patterns(words: list) -> list:
    """英文词加词边界（避免 oil 命中 boiling），中文词直接子串匹配。"""
    pats = []
    for w in words:
        if w.isascii():
            pats.append(re.compile(r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?![a-z0-9])"))
        else:
            pats.append(re.compile(re.escape(w)))
    return pats


EVENT_HIGH_PAT = _compile_patterns(EVENT_HIGH)
EVENT_MEDIUM_PAT = _compile_patterns(EVENT_MEDIUM)


def event_level(title: str) -> int:
    """A. 事件类型分级：高影响 2 / 中影响 1 / 其他 0。命中取最高级，不累加。"""
    text = title.lower()
    if any(p.search(text) for p in EVENT_HIGH_PAT):
        return 2
    if any(p.search(text) for p in EVENT_MEDIUM_PAT):
        return 1
    return 0


def shingles(title: str, n: int = 3) -> tuple:
    """返回（规范化标题, 3-gram 集合），用于近似比对是否同一事件。"""
    t = norm_title(title)
    if len(t) < n:
        return t, {t} if t else set()
    return t, {t[i:i + n] for i in range(len(t) - n + 1)}


def cluster(items: list) -> list:
    """B. 把同一事件的多源报道聚成一簇（标题 3-gram 相似度 >= 阈值）。"""
    clusters = []
    for it in items:
        text, grams = shingles(it["title"])
        for c in clusters:
            if min(len(text), c["len"]) < MIN_SIM_LEN:
                same = grams == c["grams"]
            else:
                union = grams | c["grams"]
                same = bool(union) and len(grams & c["grams"]) / len(union) >= SIM_THRESHOLD
            if same:
                c["items"].append(it)
                c["sources"].add(it["source"])
                break
        else:
            clusters.append({"len": len(text), "grams": grams,
                             "sources": {it["source"]}, "items": [it]})
    return clusters


# ----------------------------------------------------------------------------
# 抓取
# ----------------------------------------------------------------------------

def fetch_text(url: str, timeout: int):
    """GET 返回文本；失败返回 None（单源失败不影响整体）。"""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        print(f"[警告] 抓取失败 {url}：{e}")
        return None
    return decode_body(raw)


def decode_body(raw: bytes) -> str:
    """按 XML 声明里的编码解码，兼容 GB2312 等中文源。"""
    enc = "utf-8"
    m = re.search(rb'encoding="([\w-]+)"', raw[:200], re.I)
    if m:
        enc = m.group(1).decode("ascii", "ignore")
    try:
        return raw.decode(enc)
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


def make_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def fetch_newsnow(source_id: str, name: str, timeout: int) -> list:
    body = fetch_text(NEWSNOW_API.format(id=source_id), timeout)
    if not body:
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print(f"[警告] {name} 返回非 JSON，已跳过。")
        return []
    out = []
    for item in data.get("items", []):
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        if not title:
            continue
        out.append({"key": make_key(url or f"{name}:{title}"),
                    "title": title, "url": url, "source": name})
    return out


def fetch_rss(name: str, url: str, timeout: int) -> list:
    body = fetch_text(url, timeout)
    if not body:
        return []
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        print(f"[警告] {name} XML 解析失败，已跳过。")
        return []
    out = []
    for node in root.iter():
        if node.tag.split("}")[-1] not in ("item", "entry"):
            continue
        title, link = "", ""
        for child in node:
            tag = child.tag.split("}")[-1]
            if tag == "title" and not title:
                title = (child.text or "").strip()
            elif tag == "link" and not link:
                link = (child.get("href") or child.text or "").strip()
        if not title:
            continue
        out.append({"key": make_key(link or f"{name}:{title}"),
                    "title": title, "url": link, "source": name})
    return out


def collect(timeout: int) -> list:
    items = []
    for sid, name in NEWSNOW_SOURCES:
        items.extend(fetch_newsnow(sid, name, timeout))
    for name, url in RSS_SOURCES:
        items.extend(fetch_rss(name, url, timeout))
    return items


# ----------------------------------------------------------------------------
# 渲染与推送
# ----------------------------------------------------------------------------

def render_message(title: str, items: list) -> str:
    """渲染企业微信 markdown，超出字节上限时自动截断。"""
    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    lines = [f"# {title}", "", f"**{now}（北京时间）** ｜ {len(items)} 条", ""]
    used = len("\n".join(lines).encode("utf-8"))
    shown = 0
    for it in items:
        text = f"[{it['title']}]({it['url']})" if it.get("url") else it["title"]
        line = f"> {text} · {it['source']}"
        size = len((line + "\n").encode("utf-8"))
        if used + size > MAX_BYTES:
            lines.append(f"> …另有 {len(items) - shown} 条")
            break
        lines.append(line)
        used += size
        shown += 1
    return "\n".join(lines).rstrip() + "\n"


# ----------------------------------------------------------------------------
# 状态
# ----------------------------------------------------------------------------

def empty_state() -> dict:
    return {"sent": {}, "pending": {}, "next_digest_at": None}


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return empty_state()
    if "sent" not in raw:  # 兼容旧格式（扁平 dict 视为已发送）
        return {"sent": raw, "pending": {}, "next_digest_at": None}
    raw.setdefault("pending", {})
    raw.setdefault("next_digest_at", None)
    return raw


def save_state(path: str, state: dict) -> None:
    cutoff = (datetime.now(LOCAL_TZ) - timedelta(days=STATE_KEEP_DAYS)).isoformat()
    state["sent"] = {k: v for k, v in state["sent"].items() if v >= cutoff}
    state["pending"] = {k: v for k, v in state["pending"].items() if v["ts"] >= cutoff}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="全球财经快讯（重要实时推 + 中低 12h 汇总）")
    parser.add_argument("--state", default=".flash_state.json", help="状态文件路径")
    parser.add_argument("--max-items", type=int, default=12, help="重要消息每次最多条数，默认 12")
    parser.add_argument("--digest-hours", type=int, default=12, help="中低新闻汇总周期（小时），默认 12")
    parser.add_argument("--digest-max", type=int, default=30, help="汇总单条最多条数，默认 30")
    parser.add_argument("--timeout", type=int, default=20, help="单源请求超时秒数，默认 20")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不推送、不写状态")
    parser.add_argument("--digest-now", action="store_true", help="强制立即汇总一次")
    parser.add_argument("--reset", action="store_true", help="清空状态文件后退出")
    parser.add_argument("--push-wework", action="store_true",
                        help="推送到企业微信（默认读环境变量 WEWORK_WEBHOOK_URL）")
    parser.add_argument("--webhook-url", help="企业微信 Webhook（默认读 WEWORK_WEBHOOK_URL）")
    parser.add_argument("--msg-type", help="markdown（默认）| text，默认读 WEWORK_MSG_TYPE")
    args = parser.parse_args()

    if args.reset:
        save_state(args.state, empty_state())
        print(f"[已清空] {args.state}")
        return

    now = datetime.now(LOCAL_TZ)
    interval = timedelta(hours=max(args.digest_hours, 1))
    state = load_state(args.state)

    # 1. 抓取 + 过滤已发送/待汇总 + A 事件类型分级
    known = set(state["sent"]) | set(state["pending"])
    new_items = []
    for it in collect(args.timeout):
        if it["key"] in known:
            continue
        it["level"] = event_level(it["title"])
        new_items.append(it)

    # 2. B 多源共振：同一事件跨源报道则提升一级，且只保留一个代表条目
    important, normal = [], []
    for c in cluster(new_items):
        level = max(it["level"] for it in c["items"])
        if len(c["sources"]) >= 2 and level < IMPORTANT_LEVEL:
            level += 1
        rep = max(c["items"], key=lambda x: SOURCE_PRIORITY.get(x["source"], 0))
        rep["score"] = level
        if level >= IMPORTANT_LEVEL:
            important.append(rep)
        elif rep["source"] in DIGEST_SOURCES:
            normal.append(rep)

    important.sort(key=lambda x: (-x["score"], -SOURCE_PRIORITY.get(x["source"], 0)))
    important = important[: max(args.max_items, 1)]

    # 3. 判断是否到汇总时间
    due = args.digest_now
    if not due:
        if state["next_digest_at"] is None:
            state["next_digest_at"] = (now + interval).isoformat(timespec="seconds")
        else:
            try:
                due = now >= datetime.fromisoformat(state["next_digest_at"])
            except ValueError:
                due = True

    digest_items = []
    if due:
        digest_items = sorted(state["pending"].values(), key=lambda x: -x["score"])[: max(args.digest_max, 1)]
        state["pending"] = {}
        state["next_digest_at"] = (now + interval).isoformat(timespec="seconds")

    # 4. 预览或推送
    important_msg = render_message("重要财经快讯", important) if important else ""
    digest_msg = render_message(f"财经摘要（近 {args.digest_hours} 小时·中低）", digest_items) if digest_items else ""

    if args.dry_run:
        print(important_msg or "[无重要新闻]")
        print(digest_msg or "[无汇总内容]")
        print(f"[预览] 重要 {len(important)} 条 / 汇总 {len(digest_items)} 条（未推送、未写状态）")
        return

    if args.push_wework:
        webhook_url = args.webhook_url or os.environ.get("WEWORK_WEBHOOK_URL")
        if not webhook_url:
            raise SystemExit("[错误] 未提供企业微信 Webhook（环境变量 WEWORK_WEBHOOK_URL 或 --webhook-url）。")
        msg_type = args.msg_type or os.environ.get("WEWORK_MSG_TYPE") or "markdown"
        if important_msg:
            push_wework(webhook_url, important_msg, msg_type, args.timeout)
            print(f"[已推送] 重要财经快讯（{len(important)} 条）")
        if digest_msg:
            push_wework(webhook_url, digest_msg, msg_type, args.timeout)
            print(f"[已推送] 财经摘要（{len(digest_items)} 条）")
        if not important_msg and not digest_msg:
            print("[无新增] 本次不推送")

    # 5. 更新状态
    stamp = now.isoformat(timespec="seconds")
    for it in important:
        state["sent"][it["key"]] = stamp
    for it in normal:
        state["pending"][it["key"]] = {
            "title": it["title"], "url": it.get("url", ""),
            "source": it["source"], "score": it["score"], "ts": stamp,
        }
    save_state(args.state, state)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
