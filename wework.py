#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
企业微信推送的公共实现（被 econ_calendar.py 与 flash_news.py 共用）。

msg_type：
    markdown —— 企业微信群机器人
    text     —— 个人微信（企业微信应用）
推送失败会抛出 SystemExit，在 GitHub Actions 里即表现为任务失败，便于及时发现。
"""

import json
import urllib.error
import urllib.request


def push_wework(webhook_url: str, content: str, msg_type: str, timeout: int) -> None:
    """把文本推送到企业微信 Webhook。"""
    key = "markdown" if (msg_type or "text").lower() == "markdown" else "text"
    payload = {"msgtype": key, key: {"content": content}}
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"[错误] 推送失败 HTTP {e.code}。")
    except urllib.error.URLError as e:
        raise SystemExit(f"[错误] 推送网络失败：{e.reason}")
    if result.get("errcode", 0) != 0:
        raise SystemExit(f"[错误] 企业微信返回异常：{result}")
