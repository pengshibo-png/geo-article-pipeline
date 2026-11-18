"""HTTP 出站统一入口：**只认显式环境变量代理，忽略 Windows 注册表里的系统代理。**

## 为什么要这样（2026-10-17 本机实测）

系统代理指向本地 VPN：`127.0.0.1:7890`（iKuuuVPNCore）。Python 的 `urllib`
默认会读注册表里的系统代理（`getproxies()`），于是所有 LLM / 生图请求都被导去
那个代理，而它对火山方舟端点**直接断 TLS**：

    <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol>

同一时刻 `curl` 直连 0.2 秒就拿到 401 —— 端点没问题，是代理链路的问题。
用户可见症状就是"有时候生成文章失败 / 选题生成失败"，VPN 一抖动全线挂掉。

桌面应用的出站 API 调用不该被一台机器上的翻墙代理左右。需要代理的环境显式设置
`HTTPS_PROXY` / `HTTP_PROXY` 即可，本模块照常使用。
"""
from __future__ import annotations

import os
import urllib.request


def _env_proxies() -> dict:
    """把 `*_proxy` 环境变量映射成 urllib 认识的 scheme 键。

    注意：`ProxyHandler` 是按 `http` / `https` 这两个键查表的，直接塞
    `https_proxy` 这种键**不会生效** —— 这是 urllib 的坑。
    """
    https = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    http = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    proxies: dict[str, str] = {}
    if http:
        proxies["http"] = http
    if https:
        proxies["https"] = https
    elif http:
        proxies["https"] = http        # 常见约定：只给了 http 代理就都走它
    return proxies


_opener = None


def opener():
    global _opener
    if _opener is None:
        # 显式传 proxies（哪怕是空 dict）= 明确的"不走任何代理"，
        # 不会回落到 getproxies() 去读注册表。
        _opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(_env_proxies()))
    return _opener


def urlopen(req, timeout: float = 60.0):
    """替代 `urllib.request.urlopen`：语义一致，但代理行为受本模块控制。"""
    return opener().open(req, timeout=timeout)


__all__ = ["opener", "urlopen"]
