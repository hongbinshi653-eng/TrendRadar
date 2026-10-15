#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
经济日历拉取脚本（数据源：ForexFactory 社区 JSON，免费且免 Key）

用途：
    拉取指定日期范围内的重大经济事件（如 FOMC、CPI、非农、GDP），
    按国家与影响级别过滤后，输出：
      1) 结构化 JSON：供下游程序 / TrendRadar 集成
      2) 中文纯文本 / 企业微信 markdown：可直接推送

依赖：
    仅使用 Python 标准库，需 Python 3.9+（zoneinfo）。

用法：
    python econ_calendar.py                          # 今天（高/中影响）
    python econ_calendar.py --days 7                 # 未来 7 天
    python econ_calendar.py --impact high            # 只看高影响
    python econ_calendar.py --countries USD CNY      # 只看美国和中国的数据
    python econ_calendar.py --out-md out.md          # 输出适配企业微信的 markdown
    python econ_calendar.py --push-wework            # 推送到企业微信（读 WEWORK_WEBHOOK_URL）
                                                     # WEWORK_MSG_TYPE=markdown 时自动用 markdown 版式

注意：
    该数据源不提供「实际值」，只有预期值与前值。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from wework import push_wework

# ForexFactory 社区 JSON（免费、免 Key）：本周 / 下周
FF_WEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_NEXTWEEK_URL = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

# 默认关注范围（可用 --countries 覆盖）；ForexFactory 用货币代码
DEFAULT_COUNTRIES = ["USD", "EUR", "CNY", "JPY", "GBP", "AUD", "CAD", "CHF", "NZD"]

IMPACT_LABEL = {"high": "高影响", "medium": "中影响", "low": "低影响"}
IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2}

# ForexFactory 用货币代码标识国家 / 地区
COUNTRY_LABEL = {
    "USD": "美国", "EUR": "欧元区", "CNY": "中国", "JPY": "日本",
    "GBP": "英国", "AUD": "澳大利亚", "NZD": "新西兰", "CAD": "加拿大",
    "CHF": "瑞士", "ALL": "全球",
}


def http_get_json(url: str, timeout: int):
    """发起 GET 请求并解析 JSON，带基础错误提示。"""
    req = urllib.request.Request(url, headers={"User-Agent": "econ-calendar/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise SystemExit("[错误] 请求过于频繁（HTTP 429），请稍后重试。")
        raise SystemExit(f"[错误] 请求失败 HTTP {e.code}。")
    except urllib.error.URLError as e:
        raise SystemExit(f"[错误] 网络请求失败：{e.reason}")

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise SystemExit(f"[错误] 返回内容不是合法 JSON（前 120 字符）：{body[:120]}")


def fmt_value(value) -> str:
    """格式化数值：空值显示 -。"""
    if value is None or value == "":
        return "-"
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def parse_ff_event(raw: dict):
    """把 ForexFactory 单条事件转换为统一结构；时间转为北京时间。

    FF 字段：title / country(货币代码) / date(带时区偏移的 ISO) / impact(High/Medium/Low/Holiday)
             / forecast / previous（不含 actual）。
    """
    impact = (raw.get("impact") or "").strip().lower()
    if impact == "holiday":  # FF 把休市日也算一种影响级别，归入低影响
        impact = "low"
    if impact not in IMPACT_LABEL:
        impact = "unknown"

    day = ""
    date_text = raw.get("date") or ""
    try:
        dt_local = datetime.fromisoformat(date_text).astimezone(LOCAL_TZ)
        time_str = dt_local.strftime("%H:%M")
        day = dt_local.date().isoformat()
        sort_key = dt_local
    except ValueError:
        time_str = "--:--"
        sort_key = datetime.min.replace(tzinfo=LOCAL_TZ)

    currency = (raw.get("country") or "").strip().upper()
    return {
        "time": date_text,
        "day": day,
        "time_str": time_str,
        "sort_key": sort_key,
        "country": currency,
        "country_label": COUNTRY_LABEL.get(currency, currency or "-"),
        "event": (raw.get("title") or "").strip(),
        "impact": impact,
        "estimate": fmt_value(raw.get("forecast")),
        "prev": fmt_value(raw.get("previous")),
        "actual": "-",
    }


def fetch_forexfactory(start: date, end: date, timeout: int) -> list:
    """抓取 ForexFactory 社区 JSON（本周 + 下周），返回区间内的事件。

    注意：该源不提供「实际值」；「下周」文件可能不存在（404），此时自动跳过。
    """
    events = []
    ok = 0
    for url in (FF_WEEK_URL, FF_NEXTWEEK_URL):
        try:
            items = http_get_json(url, timeout)
        except SystemExit as e:
            print(f"[警告] 跳过数据源 {url}：{e}")
            continue
        ok += 1
        for item in items:
            ev = parse_ff_event(item)
            if ev["day"] and start.isoformat() <= ev["day"] <= end.isoformat():
                events.append(ev)
    if ok == 0:
        raise SystemExit("[错误] ForexFactory 数据源均不可用，请稍后重试。")
    return events


def filter_events(events: list, impacts: list, countries: list) -> list:
    """按影响级别与国家过滤，并按时间、影响排序。"""
    impacts = {i.lower() for i in impacts}
    countries = {c.upper() for c in countries}
    kept = [
        e for e in events
        if e["impact"] in impacts
        and (not countries or e["country"] in countries)
    ]
    kept.sort(key=lambda e: (e["sort_key"], IMPACT_ORDER.get(e["impact"], 9)))
    return kept


def render_text(events: list, start: date, end: date) -> str:
    """渲染成可直接推送的中文纯文本。"""
    span = start.isoformat() if start == end else f"{start.isoformat()} ~ {end.isoformat()}"
    counts = {k: sum(1 for e in events if e["impact"] == k) for k in ("high", "medium", "low")}

    lines = [f"经济日历 · {span}（北京时间）"]
    lines.append(f"高影响 {counts['high']} · 中影响 {counts['medium']} · 共 {len(events)} 条")
    lines.append("")

    for impact in ("high", "medium", "low"):
        group = [e for e in events if e["impact"] == impact]
        if not group:
            continue
        lines.append(f"【{IMPACT_LABEL[impact]}】")
        for e in group:
            lines.append(f"{e['time_str']}  {e['country_label']}  {e['event']}")
            values = (e["estimate"], e["prev"], e["actual"])
            if any(v != "-" for v in values):
                lines.append(
                    f"        预期 {e['estimate']} | 前值 {e['prev']} | 实际 {e['actual']}"
                )
        lines.append("")

    if not events:
        lines.append("（该时段暂无匹配的经济事件）")

    return "\n".join(lines).rstrip() + "\n"


def render_markdown(events: list, start: date, end: date) -> str:
    """渲染成适配企业微信 markdown 的文本。

    企业微信 markdown 不支持缩进、列表、表格，仅支持标题/加粗/引用/行内代码/字体颜色，
    因此这里用「标题 + 字体颜色分区 + 引用块」实现层级，不依赖前导空格。
    """
    span = start.isoformat() if start == end else f"{start.isoformat()} ~ {end.isoformat()}"
    counts = {k: sum(1 for e in events if e["impact"] == k) for k in ("high", "medium", "low")}

    lines = [f"# 经济日历 · {span}", ""]
    lines.append(
        f"**北京时间** ｜ 高影响 {counts['high']} · 中影响 {counts['medium']} · 共 {len(events)} 条"
    )
    lines.append("")

    if not events:
        lines.append("（该时段暂无匹配的经济事件）")
        return "\n".join(lines).rstrip() + "\n"

    # 高影响用橙红(warning)突出，中/低影响用灰色(comment)弱化
    color = {"high": "warning", "medium": "comment", "low": "comment"}
    for impact in ("high", "medium", "low"):
        group = [e for e in events if e["impact"] == impact]
        if not group:
            continue
        lines.append(f"<font color=\"{color[impact]}\">{IMPACT_LABEL[impact]}</font>")
        lines.append("")
        for e in group:
            lines.append(f"> {e['time_str']} {e['country_label']} **{e['event']}**")
            values = (e["estimate"], e["prev"], e["actual"])
            if any(v != "-" for v in values):
                lines.append(f"> 预期 {e['estimate']} ｜ 前值 {e['prev']} ｜ 实际 {e['actual']}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_json(events: list, start: date, end: date) -> dict:
    """构造结构化 JSON 输出。"""
    return {
        "generated_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
        "source": "forexfactory",
        "timezone": "Asia/Shanghai",
        "from": start.isoformat(),
        "to": end.isoformat(),
        "count": len(events),
        "events": [
            {k: v for k, v in e.items() if k != "sort_key"} for e in events
        ],
    }


def write_file(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[已写入] {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="拉取并格式化重大经济事件日历")
    parser.add_argument("--days", type=int, default=1, help="拉取天数，默认 1（今天）")
    parser.add_argument("--start", help="起始日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--impact", nargs="+", default=["high", "medium"],
                        choices=["high", "medium", "low"], help="影响级别，默认 high medium")
    parser.add_argument("--countries", nargs="+", default=None,
                        help=f"关注的货币代码（默认 {' '.join(DEFAULT_COUNTRIES)}）")
    parser.add_argument("--timeout", type=int, default=20, help="请求超时秒数，默认 20")
    parser.add_argument("--out-json", help="JSON 输出文件路径")
    parser.add_argument("--out-text", help="纯文本输出文件路径")
    parser.add_argument("--out-md", help="企业微信 markdown 输出文件路径")
    parser.add_argument("--push-wework", action="store_true",
                        help="推送到企业微信 Webhook（默认读环境变量 WEWORK_WEBHOOK_URL）")
    parser.add_argument("--webhook-url", help="企业微信 Webhook 地址（默认读环境变量 WEWORK_WEBHOOK_URL）")
    parser.add_argument("--msg-type",
                        help="消息类型：text(个人微信) | markdown(群机器人)，默认读 WEWORK_MSG_TYPE 或 text")
    args = parser.parse_args()

    start = date.fromisoformat(args.start) if args.start else datetime.now(LOCAL_TZ).date()
    end = start + timedelta(days=max(args.days, 1) - 1)

    events = filter_events(fetch_forexfactory(start, end, args.timeout),
                           args.impact, args.countries or DEFAULT_COUNTRIES)

    json_text = json.dumps(build_json(events, start, end), ensure_ascii=False, indent=2)
    text_text = render_text(events, start, end)
    md_text = render_markdown(events, start, end)

    for path, content in ((args.out_json, json_text),
                          (args.out_text, text_text),
                          (args.out_md, md_text)):
        if path:
            write_file(path, content)

    if args.push_wework:
        webhook_url = args.webhook_url or os.environ.get("WEWORK_WEBHOOK_URL")
        if not webhook_url:
            raise SystemExit(
                "[错误] 未提供企业微信 Webhook。请设置环境变量 WEWORK_WEBHOOK_URL，"
                "或使用 --webhook-url 传入。"
            )
        msg_type = args.msg_type or os.environ.get("WEWORK_MSG_TYPE") or "text"
        content = md_text if msg_type.lower() == "markdown" else text_text
        push_wework(webhook_url, content, msg_type, args.timeout)
        print("[已推送] 企业微信")
    elif not (args.out_json or args.out_text or args.out_md):
        print(json_text)
        print("\n" + text_text, end="")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
