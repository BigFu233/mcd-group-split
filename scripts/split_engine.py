#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拼单凑券 + AA 分摊引擎（本项目的核心算法）

能力：
1. 券可用性校验：生效日期 / 星期几 / 可用时段 / 渠道（到店 or 外送）/ 剩余名额
2. 凑券优化：把券分配给「最省钱且合规」的那一份餐品，一张券只吃一份
3. 三种分摊模式：proportional 按消费占比（默认）/ initiator 优惠全归发起人 / equal 平均分
4. 尾差处理：四舍五入后多退少补，差额落在发起人身上，保证「每人实付之和 == 整单实付」
5. 预算校验 + 加购建议 + 分享版清单

用法：
    python scripts/split_engine.py examples/order_demo.json
    python scripts/split_engine.py examples/order_demo.json --check   # 只看券可用性
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

D = lambda x: Decimal(str(x))  # noqa: E731
CENT = Decimal("0.01")
WEEK_CN = {1: "周日", 2: "周一", 3: "周二", 4: "周三", 5: "周四", 6: "周五", 7: "周六"}
MODE_CN = {"proportional": "按消费占比分摊（公平模式）",
           "initiator": "优惠全归发起者",
           "equal": "优惠平均分摊"}


# ---------------------------------------------------------------- 券可用性
def cn_weekday(d: dt.date) -> int:
    """把 Python 的 Mon=0 转成麦当劳的 1=周日 ... 7=周六。"""
    return (d.weekday() + 1) % 7 + 1


def check_coupon(c: dict, now: dt.datetime, channel: str, order_amount: Decimal) -> tuple[bool, str]:
    start, end = (c.get("dateRange") or [None, None])[:2]
    if start and now.date() < dt.date.fromisoformat(start):
        return False, f"未到生效日（{start} 起）"
    if end and now.date() > dt.date.fromisoformat(end):
        return False, f"已过期（{end} 止）"
    wds = c.get("weekdays") or []
    if wds and cn_weekday(now.date()) not in wds:
        return False, "限" + "、".join(WEEK_CN[w] for w in wds)
    periods = c.get("timePeriods") or []
    if periods:
        hm = now.strftime("%H:%M")
        if not any(s <= hm <= e for s, e in periods):
            return False, "仅在 " + "、".join(f"{s}-{e}" for s, e in periods) + " 可用"
    chans = {1: "dine_in", 2: "delivery"}.items()
    allowed = {name for code, name in chans if code in (c.get("channels") or [])}
    if allowed and channel not in allowed:
        return False, f"限{'/'.join(sorted(allowed))}使用".replace("dine_in", "到店").replace("delivery", "外送")
    left = c.get("leftCount")
    if isinstance(left, int) and left <= 0:
        return False, "券已用完"
    return True, "可用"


# ---------------------------------------------------------------- 凑券
def _scope_match(coupon: dict, item: dict) -> bool:
    """券的适用范围粗匹配：优先用 order.json 里显式声明的 scopeKeywords，
    否则用券标题里的关键词去撞商品名 / 分类。券的精确适用商品以 App 券详情为准。"""
    keys = coupon.get("scopeKeywords")
    if keys:
        hay = f"{item.get('name','')}{item.get('category','')}"
        return any(k in hay for k in keys)
    title = coupon.get("title", "")
    hay = f"{item.get('name','')}{item.get('category','')}"
    if "小食" in title:
        return "小食" in hay or "派" in hay or "薯" in hay or "鸡块" in hay or "新地" in hay or "麦旋风" in hay
    for token in ("麦辣鸡腿堡", "巨无霸", "麦乐鸡", "薯条", "脆薯饼", "麦旋风", "可乐", "咖啡"):
        if token in title:
            return token in hay
    return False


def plan_coupons(coupons: list[dict], lines: list[dict], now: dt.datetime,
                 channel: str, total: Decimal) -> tuple[list[dict], list[dict]]:
    """返回 (已用券, 未用券及原因)。lines 里每行是 {owner, name, price, qty, index}"""
    used, skipped = [], []
    taken: set[int] = set()
    checked = []
    for c in coupons:
        ok, reason = check_coupon(c, now, channel, total)
        if not ok:
            skipped.append({**c, "reason": reason})
            continue
        best = None
        for ln in lines:
            if ln["index"] in taken or not _scope_match(c, ln):
                continue
            unit = ln["price"]
            saving = (unit - D(c["value"])) if c.get("kind") == "price" else min(D(c["value"]), unit)
            if saving <= 0:
                continue
            if best is None or saving > best[1]:
                best = (ln, saving)
        if best:
            ln, saving = best
            taken.add(ln["index"])
            used.append({"coupon": c, "line": ln, "saving": saving})
        else:
            skipped.append({**c, "reason": "没有匹配的餐品（适用范围不符或该餐品已被别的券占用）"})
        checked.append(c)
    return used, skipped


# ---------------------------------------------------------------- 分摊
def allocate(orig: list[Decimal], discount: Decimal, mode: str, initiator_idx: int) -> list[Decimal]:
    n = len(orig)
    total_orig = sum(orig)
    if mode == "initiator":
        shares = [Decimal("0")] * n
        shares[initiator_idx] = discount
    elif mode == "equal":
        each = (discount / n).quantize(CENT, ROUND_HALF_UP)
        shares = [each] * n
        shares[initiator_idx] += discount - each * n  # 尾差给发起人
    else:  # proportional
        shares = [(discount * o / total_orig).quantize(CENT, ROUND_HALF_UP) if total_orig else Decimal("0")
                  for o in orig]
        shares[initiator_idx] += discount - sum(shares)
    return [s if s >= 0 else Decimal("0") for s in shares]


# ---------------------------------------------------------------- 输出
def money(x) -> str:
    return f"¥{Decimal(x).quantize(CENT, ROUND_HALF_UP):,}"


def render(order: dict, result: dict) -> str:
    o, r = order, result
    L: list[str] = []
    L.append(f"# 🍟 {o.get('title', '麦当劳拼单结算单')}\n")
    L.append("## 📋 拼单基础信息\n")
    L.append("| 项目 | 内容 |")
    L.append("|---|---|")
    L.append(f"| 门店 | {o.get('storeName','')} |")
    L.append(f"| 取餐方式 | {'麦乐送' if o.get('channel')=='delivery' else '到店自取'} |")
    L.append(f"| 参与人数 | {len(o['members'])} 人（发起人：{o.get('initiator','-')}） |")
    L.append(f"| 分摊规则 | {MODE_CN[o.get('mode','proportional')]} |")
    L.append(f"| 下单时间点 | {o['now']} |\n")

    L.append("## 🍔 全员点餐明细\n")
    L.append("| 昵称 | 餐品 | 定制要求 | 单价 | 数量 | 小计 |")
    L.append("|---|---|---|---:|---:|---:|")
    for ln in r["lines"]:
        L.append(f"| {ln['owner']} | {ln['name']} | {ln['custom'] or '—'} | {money(ln['price'])} | "
                 f"{ln['qty']} | {money(ln['subtotal'])} |")
    L.append(f"| **合计** | | | | **{r['total_qty']}** | **{money(r['total'])}** |\n")

    L.append("## 🎟️ 优惠券使用方案\n")
    if r["used"]:
        L.append("| 用掉的券 | 用在谁的哪份餐 | 原价 | 券后 | 省下 |")
        L.append("|---|---|---:|---:|---:|")
        for u in r["used"]:
            c, ln = u["coupon"], u["line"]
            after = money(ln["price"] - u["saving"])
            L.append(f"| {c['title']} | {ln['owner']} · {ln['name']} | {money(ln['price'])} | {after} | {money(u['saving'])} |")
        L.append(f"\n**优惠合计：{money(r['discount'])}** ｜ 原价 {money(r['total'])} → 实付 **{money(r['payable'])}**\n")
    else:
        L.append("⚠️ 本单没有可用的券（详见下方原因），按原价结算。\n")
    if r["skipped"]:
        L.append("**没用上的券 / 原因：**\n")
        L.append("| 券 | 状态 | 原因 |")
        L.append("|---|---|---|")
        for s in r["skipped"]:
            L.append(f"| {s['title']} | {s.get('datetimeText','')} | {s['reason']} |")
        L.append("")

    L.append("## 💰 费用结算表\n")
    L.append("| 昵称 | 原价 | 分摊优惠 | 实付应付 | 预算 | 校验 |")
    L.append("|---|---:|---:|---:|---:|---|")
    for m in r["members"]:
        flag = "✅" if m["pay"] <= (m["budget"] or 10**9) else "⚠️ 超预算"
        L.append(f"| {m['nick']} | {money(m['orig'])} | -{money(m['share'])} | **{money(m['pay'])}** | "
                 f"{money(m['budget']) if m['budget'] else '不限'} | {flag} |")
    L.append(f"| **合计** | **{money(r['total'])}** | **-{money(r['discount'])}** | **{money(r['payable'])}** | | |\n")

    L.append("## 💡 补充提示\n")
    for tip in r["tips"]:
        L.append(f"- {tip}")
    L.append("")
    L.append("> ⚠️ 本工具仅做选餐、凑券、算账**方案参考，不能直接下单支付**。餐品价格、券可用性以麦当劳 App 实时结果为准；"
             "有过敏或严格忌口的朋友，请以门店实物与配料说明为准。")
    return "\n".join(L)


def render_share(order: dict, result: dict) -> str:
    o, r = order, result
    L = [f"【麦当劳拼单结算】{o.get('storeName','')} · {len(o['members'])}人 · "
         f"{'麦乐送' if o.get('channel')=='delivery' else '到店自取'}",
         f"原价 {money(r['total'])} - 优惠 {money(r['discount'])} = 实付 {money(r['payable'])}",
         f"分摊方式：{MODE_CN[o.get('mode','proportional')]}", "———"]
    for m in r["members"]:
        L.append(f"{m['nick']}：应付 {money(m['pay'])}（原价 {money(m['orig'])}，摊 -{money(m['share'])}）")
    if r["used"]:
        L.append("———")
        L.append("用券：" + "、".join(f"{u['coupon']['title']}(省{money(u['saving'])})" for u in r["used"]))
    if r["mods"]:
        L.append("改配料：" + "；".join(r["mods"]))
    L.append("（方案参考，不下单；价格以App为准）")
    return "\n".join(L)


def render_html(order: dict, result: dict) -> str:
    """生成可直接截图分享的结算卡片（HTML，无外部依赖）。"""
    o, r = order, result
    rows = "".join(
        f"<tr><td class='n'>{m['nick']}</td><td class='r'>{money(m['orig'])}</td>"
        f"<td class='r g'>-{money(m['share'])}</td>"
        f"<td class='r b'>{money(m['pay'])}</td></tr>"
        for m in r["members"])
    items = "".join(
        f"<li><b>{ln['owner']}</b> · {ln['name']}"
        + (f"<span class='tag'>{ln['custom']}</span>" if ln["custom"] else "")
        + f"<em>{money(ln['subtotal'])}</em></li>"
        for ln in r["lines"])
    cp = "".join(
        f"<li>{u['coupon']['title']} → {u['line']['owner']}的{u['line']['name']}"
        f"<em>省 {money(u['saving'])}</em></li>" for u in r["used"]) or "<li>本单无可用券</li>"
    skip = "".join(f"<li class='muted'>{s['title']}：{s['reason']}</li>" for s in r["skipped"])
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{o.get('title','拼单结算单')}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#F5F5F7;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
display:flex;justify-content:center;padding:24px;color:#1D1D1F}}
.card{{width:600px;background:#fff;border-radius:24px;overflow:hidden;
box-shadow:0 12px 40px rgba(0,0,0,.12)}}
.hd{{background:linear-gradient(135deg,#FFC72C,#FFB400);padding:26px 28px 22px}}
.hd h1{{font-size:22px;letter-spacing:.5px}}
.hd p{{margin-top:6px;font-size:13px;color:#5A4A00}}
.total{{display:flex;align-items:baseline;gap:10px;margin-top:16px}}
.total b{{font-size:34px;color:#DA291C;letter-spacing:-1px}}
.total s{{color:#7A6A20;font-size:15px}}
.total span{{margin-left:auto;background:#DA291C;color:#fff;font-size:12px;
padding:5px 12px;border-radius:99px}}
.sec{{padding:20px 28px;border-bottom:1px dashed #EEE}}
.sec h2{{font-size:13px;color:#86868B;font-weight:600;letter-spacing:1px;margin-bottom:12px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{padding:9px 0;text-align:left}}
th{{font-size:12px;color:#86868B;font-weight:500;border-bottom:1px solid #EEE}}
td{{border-bottom:1px solid #F7F7F7}}
.r{{text-align:right}} .g{{color:#1B9C4B}} .b{{font-weight:700;font-size:15px}}
ul{{list-style:none;font-size:14px}}
ul li{{padding:7px 0;border-bottom:1px solid #F7F7F7;display:flex;align-items:center;gap:8px}}
ul li em{{margin-left:auto;font-style:normal;color:#1B9C4B;font-weight:600}}
.tag{{font-size:11px;background:#FFF3D6;color:#8A6D00;padding:2px 7px;border-radius:6px}}
.muted{{color:#A1A1A6;font-size:13px}}
.ft{{padding:16px 28px;background:#FAFAFC;font-size:11px;color:#A1A1A6;line-height:1.7}}
.wm{{padding:12px 28px 20px;font-size:12px;color:#86868B;display:flex;gap:6px;align-items:center}}
.wm b{{color:#DA291C}}
</style></head><body>
<div class="card">
  <div class="hd">
    <h1>🍟 {o.get('title','拼单结算单')}</h1>
    <p>{o.get('storeName','')} · {len(o['members'])} 人 · {'麦乐送' if o.get('channel')=='delivery' else '到店自取'} · {MODE_CN[o.get('mode','proportional')]}</p>
    <div class="total"><b>{money(r['payable'])}</b><s>{money(r['total'])}</s>
      <span>已省 {money(r['discount'])}</span></div>
  </div>
  <div class="sec"><h2>🍔 点餐明细</h2><ul>{items}</ul></div>
  <div class="sec"><h2>🎟️ 用券方案</h2><ul>{cp}</ul>
    {f"<ul style='margin-top:8px'>{skip}</ul>" if skip else ""}</div>
  <div class="sec"><h2>💰 每人应付</h2>
    <table><tr><th>昵称</th><th class="r">原价</th><th class="r">分摊优惠</th><th class="r">应付</th></tr>
    {rows}</table></div>
  <div class="ft">⚠️ 本工具仅做选餐、凑券、算账方案参考，不能直接下单支付。<br>
  餐品价格与券可用性以麦当劳 App 实时结果为准；过敏或严格忌口请以门店实物与配料说明为准。</div>
  <div class="wm"><b>麦麦拼单官</b> · 基于麦当劳 MCP + WorkBuddy</div>
</div></body></html>"""


# ---------------------------------------------------------------- 主流程
def build(order: dict) -> dict:
    now = dt.datetime.strptime(order["now"], "%Y-%m-%d %H:%M")
    channel = order.get("channel", "delivery")
    coupons = order.get("coupons") or []
    if order.get("couponsFile"):
        with open(order["couponsFile"], encoding="utf-8") as f:
            coupons = (json.load(f).get("wallet") or []) + coupons

    lines, members = [], []
    idx = 0
    for m in order["members"]:
        orig = Decimal("0")
        for it in m["items"]:
            price = D(it["price"])
            qty = int(it.get("qty", 1))
            subtotal = price * qty
            orig += subtotal
            lines.append({"index": idx, "owner": m["nick"], "name": it["name"], "code": it.get("code", ""),
                          "price": price, "qty": qty, "subtotal": subtotal,
                          "custom": it.get("custom", "") or "", "category": it.get("category", "")})
            idx += 1
        members.append({"nick": m["nick"], "orig": orig, "budget": D(m.get("budget") or 0)})

    total = sum(ln["subtotal"] for ln in lines)
    used, skipped = plan_coupons(coupons, lines, now, channel, total)
    discount = sum((u["saving"] for u in used), Decimal("0"))
    discount = min(discount, total)
    payable = total - discount

    nick_idx = {m["nick"]: i for i, m in enumerate(members)}
    init_i = nick_idx.get(order.get("initiator"), 0)
    shares = allocate([m["orig"] for m in members], discount, order.get("mode", "proportional"), init_i)
    for m, sh in zip(members, shares):
        m["share"] = sh
        m["pay"] = m["orig"] - sh

    tips = []
    for u in used:
        c = u["coupon"]
        if c.get("leftCount") is not None:
            tips.append(f"「{c['title']}」本账号还剩 {c['leftCount']} 张，用完即止。")
    for s in skipped:
        if "限周" in s["reason"] or "仅在" in s["reason"]:
            tips.append(f"「{s['title']}」{s['reason']}，换个时段/日子下单能省更多。")
    if not used:
        tips.append("本单一张券都没吃上，建议核对券的渠道（到店/外送）与时段是否匹配。")
    over = [m["nick"] for m in members if m["budget"] and m["pay"] > m["budget"]]
    if over:
        tips.append("超预算提醒：" + "、".join(over) + " 的实付高于其预算上限，建议减配或改选单品。")
    tips.append(f"整单餐品 {sum(ln['qty'] for ln in lines)} 件，记得核对取餐号与配料修改项是否被门店接收。")
    tips.append("分摊尾差已全部归到发起人「" + members[init_i]["nick"] + "」名下，保证每人金额相加等于整单实付。")

    mods = [f"{ln['owner']}的{ln['name']}：{ln['custom']}" for ln in lines if ln["custom"]]
    return {"lines": lines, "members": members, "total": total, "discount": discount,
            "payable": payable, "used": used, "skipped": skipped, "tips": tips, "mods": mods,
            "total_qty": sum(ln["qty"] for ln in lines)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--check", action="store_true", help="只输出券可用性检查")
    ap.add_argument("--html", action="store_true", help="额外生成可截图分享的结算卡片")
    ap.add_argument("--outdir", default="out")
    args = ap.parse_args()

    with open(args.spec, encoding="utf-8") as f:
        order = json.load(f)
    result = build(order)

    if args.check:
        now = dt.datetime.strptime(order["now"], "%Y-%m-%d %H:%M")
        total = result["total"]
        coupons = list(order.get("coupons") or [])
        if order.get("couponsFile"):
            with open(order["couponsFile"], encoding="utf-8") as f:
                coupons = (json.load(f).get("wallet") or []) + coupons
        print(f"券可用性检查 @ {order['now']} / {order.get('channel')}\n")
        for c in coupons:
            ok, why = check_coupon(c, now, order.get("channel", "delivery"), total)
            print(f"{'✅' if ok else '❌'} {c['title']:<18} {why}")
        return

    md = render(order, result)
    share = render_share(order, result)
    os.makedirs(args.outdir, exist_ok=True)
    with open(os.path.join(args.outdir, "结算单.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(args.outdir, "分享版清单.txt"), "w", encoding="utf-8") as f:
        f.write(share)
    if args.html:
        with open(os.path.join(args.outdir, "结算单.html"), "w", encoding="utf-8") as f:
            f.write(render_html(order, result))
    print(md)
    print("\n" + "-" * 40 + "\n" + share)
    tail = "、结算单.html" if args.html else ""
    print(f"\n[写出] {args.outdir}/结算单.md、{args.outdir}/分享版清单.txt{tail}")


if __name__ == "__main__":
    main()
