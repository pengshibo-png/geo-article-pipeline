"""探测当前配置的 LLM 端点：模型名、输出上限、是否 reasoning。

只读探测（一次极短调用 + 一次试探性 max_tokens），用于确定"输出被截断"该把
max_tokens 调到多少 —— 盲调大如果超过模型上限会直接 400。

用法：.venv\\Scripts\\python.exe tools\\probe_llm_limits.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "data" / "model_config.json"
PRESET = {"deepseek": "https://api.deepseek.com/v1",
          "doubao": "https://ark.cn-beijing.volces.com/api/v3",
          "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
          "openai": "https://api.openai.com/v1"}


def post(base: str, key: str, body: dict, timeout: float = 90) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"_raw": raw[:300]}


def main() -> int:
    cfg = json.loads(CFG.read_text(encoding="utf-8")).get("llm") or {}
    provider = str(cfg.get("provider") or "").strip()
    model = str(cfg.get("model") or "").strip()
    key = str(cfg.get("key") or "").strip()
    base = str(cfg.get("base_url") or "").strip() or PRESET.get(provider, "")
    print(f"provider={provider}  model={model}  base={base}")
    if not base or not key:
        print("配置不完整，退出")
        return 1

    # 1) 极短调用：确认模型存在、看是否返回 reasoning_content
    st, d = post(base, key, {"model": model, "messages": [{"role": "user", "content": "回复 ok"}],
                             "max_tokens": 16})
    print(f"\n[1] 短调用 HTTP {st}")
    if st != 200:
        print("   ", json.dumps(d, ensure_ascii=False)[:400])
        return 1
    ch = (d.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    print("   finish_reason:", ch.get("finish_reason"))
    print("   content:", str(msg.get("content"))[:60])
    print("   有 reasoning_content:", "reasoning_content" in msg)
    print("   usage:", json.dumps(d.get("usage") or {}, ensure_ascii=False))

    # 2) 试探 max_tokens 上限：给一个远超常规的值，看是否 400
    for mt in (8192, 16384, 32768):
        st, d = post(base, key, {"model": model,
                                 "messages": [{"role": "user", "content": "回复 ok"}],
                                 "max_tokens": mt})
        if st == 200:
            print(f"[2] max_tokens={mt:<6} 接受 ✓")
        else:
            err = json.dumps(d, ensure_ascii=False)[:200]
            print(f"[2] max_tokens={mt:<6} 拒绝 ✗  {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
