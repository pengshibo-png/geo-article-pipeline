"""知识卡片生成器：系统生图渠道出背景 + HTML 精确排版 + Playwright 截图。

为什么不是"纯生图"：文生图模型渲染中文长句会糊字/错字，知识卡片最不能错的就是
文字。所以拆成两段 —— 背景交给 `services/imagegen`（真实渠道，科技风抽象底），
文案交给 HTML/CSS（100% 准确、可改可迭代），最后用 Playwright 按 9:16 截图。

用法（源码态，cwd = 项目根）：
    .venv/Scripts/python.exe tools/make_card.py                      # 出整套系列
    .venv/Scripts/python.exe tools/make_card.py --only 2             # 只出第 2 张
    .venv/Scripts/python.exe tools/make_card.py --bg-file data/local/cards/bg-last.png
输出默认落在 `data/local/cards/series/`（不进抖音内容包库，避免影响发布库存/账本）。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

W, H = 1080, 1920          # 9:16 成品尺寸
SCALE = 1.5                # 输出 1620x2880，抖音压缩后仍清晰


# ── 卡面文案（改这里就能出别的卡）──────────────────────────────────────────
# hue：背景光的色相偏移，0 保持青绿主调；小范围偏移让系列有节奏但不成两套风格
CARDS = [
    {
        "eyebrow": "GEO · AI CONTENT ENGINE",
        "tag": "写稿闭环",
        "title": ["AI 写稿", "四段闭环"],
        "subtitle": "从选题到终稿，每一步都有检查点，断了能接着跑",
        "flow": ["选题", "初稿", "审稿", "定稿"],
        "points": [
            ("01", "选题库自动补货", "按行业与城市生成标题，库存不足自动补，不用人工盯着加"),
            ("02", "初稿落地可续跑", "多 Key 池轮转，单篇失败自动换 Key 重试，稿子落盘不丢"),
            ("03", "审稿按契约判分", "冻结契约逐项校验，直接判「可发」还是「建议重写」分流"),
            ("04", "定稿只改有问题处", "按审稿意见改写，产出终稿，不会把合格段落一起重写"),
        ],
        "stats": [("4", "写稿环节"), ("多Key", "自动轮转"), ("续跑", "断点续传")],
        "hue": -14,
    },
    {
        "eyebrow": "GEO · AI CONTENT ENGINE",
        "tag": "多平台分发",
        "title": ["20 个平台", "一次点击"],
        "subtitle": "云分发 + 本地浏览器双通道，账号能发哪个就走哪条",
        "flow": ["绑定账号", "组稿", "投递", "回查"],
        "points": [
            ("01", "双通道并行", "绑了云账号走蚁小二，没绑的走本机浏览器，互不阻塞"),
            ("02", "一稿多投", "一篇文章可同时投全部勾选渠道，也能按篇智能分配轮换"),
            ("03", "真实结果回查", "提交后自动查任务集，只有拿到平台链接才算发成功"),
            ("04", "失败分型处置", "限流冻结、登录失效标红、额度耗尽顺延，不对症不硬试"),
        ],
        "stats": [("20+", "分发平台"), ("2", "条通道"), ("0", "编造链接")],
        "hue": 18,
    },
    {
        "eyebrow": "GEO · AI CONTENT ENGINE",
        "tag": "抖音图文",
        "title": ["抖音图文", "无人值守"],
        "subtitle": "AI 写文案、AI 出配图，到点自己发，账本自己记",
        "flow": ["文案", "配图", "定位配乐", "定时发出"],
        "points": [
            ("01", "文案带话题", "标题控制在平台上限内，正文成段，标签自动带，查重避重复"),
            ("02", "多图成一组", "每包 4~6 张竖版图，按文案规划画面，图不到位会被标出来"),
            ("03", "定位与配乐", "按城市匹配地点，账号之间不撞；配乐随机取，查不到就不加"),
            ("04", "定时或循环", "每包可单独定时，也能按间隔/每日档位无人值守连发"),
        ],
        "stats": [("4~6", "图 / 包"), ("定时", "按包设置"), ("80", "库存上限")],
        "hue": -34,
    },
    {
        "eyebrow": "GEO · AI CONTENT ENGINE",
        "tag": "效果复盘",
        "title": ["发出去", "还要看得见"],
        "subtitle": "账号、额度、被引用率，全在一个看板里，不靠感觉",
        "flow": ["采样", "判定", "归集", "复盘"],
        "points": [
            ("01", "账号矩阵", "云账号一键同步，每日额度原子扣减，重复发布自动拦截"),
            ("02", "发布账本", "每条内容发到哪个号、成没成、为什么失败，逐条可查"),
            ("03", "四引擎采样", "多模型分别提问，看内容在 AI 回答里有没有被引用"),
            ("04", "趋势可复盘", "引用份额、城市分布、时间曲线，效果看得见也说得清"),
        ],
        "stats": [("4", "AI 引擎"), ("逐条", "可追溯"), ("日更", "采样监测")],
        "hue": 32,
    },
]

BG_PROMPT = (
    "抽象科技感背景，深空蓝黑底色，青绿色荧光光带在画面右下角缓慢流动，"
    "细密的数据网格线与粒子微光，玻璃质感光斑，极简未来主义，暗部留白充足，"
    "高对比但整体偏暗，画面中没有任何文字、字母、数字或图标，无人物，纯背景素材"
)


def gen_background(size: str = "1440x2560") -> bytes:
    """调系统生图渠道出背景图，返回 PNG 字节。失败时退回纯 CSS 底（返回 b""）。"""
    from app.platform import net
    from app.services import imagegen

    provider = imagegen.current_provider()
    print(f"[i] 生图渠道：{provider}")
    try:
        ref = imagegen.generate(BG_PROMPT, size=size, provider=provider)
    except Exception as exc:                                  # noqa: BLE001
        print(f"[!] 背景生图失败，改用 CSS 底色：{str(exc)[:160]}")
        return b""
    if ref.startswith("data:"):
        return base64.b64decode(ref.split(",", 1)[1])
    if ref.startswith(("http://", "https://")):
        try:
            with net.urlopen(ref, timeout=120) as resp:
                return resp.read()
        except Exception as exc:                              # noqa: BLE001
            print(f"[!] 背景下载失败，改用 CSS 底色：{str(exc)[:160]}")
    return b""


def build_html(card: dict, bg: bytes, idx: int, total: int) -> str:
    bg_css = (f"background-image:url(data:image/png;base64,"
              f"{base64.b64encode(bg).decode()});" if bg else "")
    pts = "".join(
        f"""<section class="item">
              <div class="num">{no}</div>
              <div class="body"><h3>{name}</h3><p>{desc}</p></div>
            </section>""" for no, name, desc in card["points"])
    flow = ""
    if card.get("flow"):
        steps = "".join(f"<span>{s}</span>" for s in card["flow"])
        flow = f'<div class="flow">{steps}</div>'
    # 中文值要缩字号：等宽数字 56px 时两个汉字会顶得跟数字一样大，比例失衡
    def _fs(v: str) -> int:
        return 44 if any("\u4e00" <= ch <= "\u9fff" for ch in v) else 56

    stats = "".join(
        f'<div class="stat"><b style="font-size:{_fs(v)}px">{v}</b><span>{k}</span></div>'
        for v, k in card["stats"])
    title = "".join(f"<span>{t}</span>" for t in card["title"])
    tag = f"{idx:02d} / {total:02d} · {card['tag']}"
    hue = int(card.get("hue") or 0)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  html,body{{width:{W}px;height:{H}px}}
  body{{
    position:relative;overflow:hidden;
    font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",sans-serif;
    color:#E8F4FF;background-color:#04070E;
  }}
  /* 背景独立成层：可单独色相偏移，不影响文字层 */
  .bgimg{{position:absolute;inset:0;z-index:0;{bg_css}
    background-size:cover;background-position:center;
    filter:hue-rotate({hue}deg) saturate(1.05);}}
  /* 霓虹光晕：把生图背景的氛围提上来（局部发光，不压文字） */
  body::before{{content:'';position:absolute;inset:-12%;pointer-events:none;
    background:
      radial-gradient(36% 24% at 80% 10%, rgba(34,211,238,.34), transparent 70%),
      radial-gradient(32% 20% at 12% 86%, rgba(124,92,255,.30), transparent 72%),
      radial-gradient(26% 14% at 88% 94%, rgba(255,61,174,.20), transparent 74%);
    filter:blur(10px);mix-blend-mode:screen;opacity:.9;z-index:1}}
  /* 背景压暗：只压文字区，保留顶部/边缘的光效可见 */
  .veil{{position:absolute;inset:0;z-index:1;
    background:
      radial-gradient(96% 34% at 50% 32%, rgba(4,7,14,.72) 0%, rgba(4,7,14,.30) 78%),
      linear-gradient(180deg, rgba(4,7,14,.16) 0%, rgba(4,7,14,.52) 38%,
                      rgba(3,6,12,.86) 100%);}}
  /* 细网格 */
  .grid{{position:absolute;inset:0;opacity:.16;z-index:2;
    background-image:
      linear-gradient(rgba(34,211,238,.20) 1px, transparent 1px),
      linear-gradient(90deg, rgba(34,211,238,.20) 1px, transparent 1px);
    background-size:60px 60px;
    mask-image:radial-gradient(90% 70% at 50% 30%, #000 12%, transparent 78%);
    -webkit-mask-image:radial-gradient(90% 70% at 50% 30%, #000 12%, transparent 78%);}}
  .edge{{position:absolute;left:0;right:0;height:2px;z-index:3;
    background:linear-gradient(90deg,transparent,#22D3EE 20%,#34F5C5 50%,#7C5CFF 78%,#FF3DAE 88%,transparent);
    opacity:.75}}
  .edge.top{{top:0}} .edge.bot{{bottom:0;opacity:.45}}

  .wrap{{position:relative;z-index:4;height:100%;padding:96px 78px 72px;
    display:flex;flex-direction:column}}

  .head{{display:flex;justify-content:space-between;align-items:center;
    font-size:22px;letter-spacing:.28em;color:#5FE3D0;font-weight:600}}
  .head .tag{{letter-spacing:.12em;color:#9FB6CC;border:1px solid rgba(95,227,208,.32);
    padding:8px 18px;border-radius:999px;font-size:18px;
    background:rgba(34,211,238,.08);white-space:nowrap}}

  h1{{margin-top:58px;font-size:102px;line-height:1.12;font-weight:800;
    letter-spacing:.01em;text-shadow:0 0 42px rgba(34,211,238,.28)}}
  h1 span{{display:block}}
  h1 span:last-child{{background:linear-gradient(96deg,#34F5C5 0%,#22D3EE 52%,#8B7CFF 100%);
    -webkit-background-clip:text;background-clip:text;color:transparent}}
  .sub{{margin-top:28px;font-size:30px;line-height:1.62;color:#93A9BF;font-weight:400}}

  /* 横向流程条（仅部分卡启用） */
  .flow{{margin-top:40px;display:flex;align-items:center;gap:0;
    border-radius:16px;padding:22px 18px;
    background:linear-gradient(180deg,rgba(7,14,26,.80),rgba(7,14,26,.55));
    border:1px solid rgba(34,211,238,.18)}}
  .flow span{{flex:1;text-align:center;font-size:26px;font-weight:700;color:#BFE9F5;
    position:relative}}
  .flow span:not(:last-child)::after{{content:'›';position:absolute;right:-6px;
    color:#34F5C5;font-size:34px;line-height:1;top:50%;transform:translateY(-52%);
    text-shadow:0 0 14px rgba(52,245,197,.7)}}

  .list{{margin-top:46px;display:flex;flex-direction:column;gap:24px}}
  .item{{position:relative;display:flex;gap:24px;padding:30px 28px 30px 34px;
    border-radius:18px;overflow:hidden;
    background:linear-gradient(180deg,rgba(7,14,26,.82),rgba(7,14,26,.58));
    border:1px solid rgba(34,211,238,.20);
    box-shadow:inset 0 1px 0 rgba(255,255,255,.07), 0 14px 38px rgba(0,0,0,.46);
    backdrop-filter:blur(6px)}}
  .item::before{{content:'';position:absolute;left:0;top:16px;bottom:16px;width:3px;
    border-radius:3px;background:linear-gradient(180deg,#34F5C5,#22D3EE 55%,#7C5CFF);
    box-shadow:0 0 16px rgba(52,245,197,.55)}}
  .num{{flex:0 0 74px;font-family:Consolas,"Bahnschrift",monospace;font-size:34px;
    font-weight:700;color:#34F5C5;opacity:.92;line-height:1.2;
    text-shadow:0 0 22px rgba(52,245,197,.45)}}
  .body h3{{font-size:36px;font-weight:700;letter-spacing:.01em}}
  .body p{{margin-top:10px;font-size:24px;line-height:1.55;color:#8FA6BD}}

  .spacer{{flex:1}}
  .stats{{display:flex;gap:18px;margin-top:30px;padding-top:32px;
    border-top:1px solid rgba(34,211,238,.20)}}
  .stat{{flex:1;text-align:center}}
  .stat b{{display:block;font-family:"Bahnschrift",Consolas,monospace;font-size:56px;
    font-weight:700;color:#E8F4FF;letter-spacing:.02em;
    text-shadow:0 0 30px rgba(34,211,238,.35)}}
  .stat span{{display:block;margin-top:6px;font-size:21px;color:#7C93AA;letter-spacing:.12em}}
  .foot{{margin-top:28px;font-size:19px;color:#5C7290;text-align:center;letter-spacing:.06em}}
</style></head><body>
  <div class="bgimg"></div>
  <div class="veil"></div><div class="grid"></div>
  <div class="edge top"></div><div class="edge bot"></div>
  <div class="wrap">
    <div class="head"><div>{card['eyebrow']}</div><div class="tag">{tag}</div></div>
    <h1>{title}</h1>
    <div class="sub">{card['subtitle']}</div>
    {flow}
    <div class="list">{pts}</div>
    <div class="spacer"></div>
    <div class="stats">{stats}</div>
    <div class="foot">本图由系统内置生图渠道生成 · 9:16 竖版知识卡片</div>
  </div>
</body></html>"""


def render(html: str, out: Path) -> None:
    """headless 浏览器按 9:16 截图（复用系统发布链路的启动策略：自带内核→Edge→Chrome）。"""
    from playwright.sync_api import sync_playwright

    out.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as pw:
        for kwargs in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
            try:
                browser = pw.chromium.launch(headless=True, args=["--no-sandbox"], **kwargs)
            except Exception as exc:                          # noqa: BLE001
                errors.append(f"{kwargs or 'chromium'}: {str(exc).splitlines()[0][:100]}")
                continue
            page = browser.new_page(viewport={"width": W, "height": H},
                                    device_scale_factor=SCALE)
            page.set_content(html, wait_until="load")
            page.wait_for_timeout(600)
            page.screenshot(path=str(out), type="png")
            browser.close()
            print(f"[OK] {out.name}  ({W}x{H} @{SCALE}x)")
            return
    raise SystemExit("浏览器内核不可用：" + "；".join(errors))


def main() -> int:
    # Windows 控制台默认 GBK：中文/符号说明会 UnicodeEncodeError（图已存盘也会报错退出）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老 Python 无 reconfigure
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(ROOT / "data/local/cards/series"))
    ap.add_argument("--only", type=int, default=0, help="只出第 N 张（1 起）")
    ap.add_argument("--bg-size", default="1440x2560")
    ap.add_argument("--no-bg", action="store_true", help="跳过生图，只用 CSS 底")
    ap.add_argument("--bg-file", default="", help="复用已生成的背景图，省一次生图调用")
    args = ap.parse_args()

    from app import create_app

    app = create_app()
    out_dir = Path(args.out_dir)
    with app.app_context():
        if args.bg_file:
            p = Path(args.bg_file)
            bg = p.read_bytes() if p.exists() else b""
            print(f"[i] 复用背景：{p}（{len(bg)//1024} KB）")
        else:
            bg = b"" if args.no_bg else gen_background(args.bg_size)
        if bg:
            cached = ROOT / "data/local/cards" / "bg-last.png"
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(bg)
        picked = ([CARDS[args.only - 1]] if 1 <= args.only <= len(CARDS) else CARDS)
        made = []
        for i, card in enumerate(CARDS, start=1):
            if card not in picked:
                continue
            out = out_dir / f"card-{i:02d}-{card['tag']}.png"
            render(build_html(card, bg, i, len(CARDS)), out)
            made.append(str(out))
    print(json.dumps({"ok": True, "count": len(made), "files": made},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
