#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门店 / 菜单 / 券包 同步器

把麦当劳 MCP 返回的原始结构，归一化成拼单引擎能直接吃的 data/*.json。

用法：
    export MCD_MCP_TOKEN=xxx
    python scripts/menu_sync.py --city 杭州市 --keyword 临平 --channel delivery
    python scripts/menu_sync.py --store 3330478 --becode 333047802 --channel delivery
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcd_client as mcd  # noqa: E402

CHANNEL = {"dine_in": {"orderType": 1, "beType": 1},
           "delivery": {"orderType": 2, "beType": 2}}
WEEK_CN = {1: "周日", 2: "周一", 3: "周二", 4: "周三", 5: "周四", 6: "周五", 7: "周六"}


# ---------------------------------------------------------------- 门店
def find_store(city: str, keyword: str, be_type: int = 1) -> list[dict]:
    res = mcd.call_with_retry("query-nearby-stores",
                              {"searchType": 2, "city": city, "keyword": keyword, "beType": be_type})
    return res.get("data") or []


def store_be_code(address_id: str, be_type: int = 2) -> dict[str, str]:
    """麦乐送/团餐场景：通过配送地址拿到 storeCode -> beCode 映射。"""
    res = mcd.call_with_retry("delivery-query-stores", {"addressId": address_id, "beType": be_type})
    return {s["storeCode"]: s.get("beCode", "") for s in (res.get("data") or [])}


def pick_address() -> str:
    res = mcd.call_with_retry("delivery-query-addresses", {})
    addrs = (res.get("data") or {}).get("addresses") or []
    if not addrs:
        raise SystemExit("该账号下没有可配送地址，请先在 App 内新增，或改用 --store/--becode 指定门店。")
    for line in addrs:
        print(f"[地址] {line['addressId']}  {line['contactName']}  {line['fullAddress']}")
    return addrs[0]["addressId"]


# ---------------------------------------------------------------- 菜品
def fetch_menu(store: str, be: str, channel: str) -> dict:
    cfg = CHANNEL[channel]
    res = mcd.call_with_retry("query-meals", {"storeCode": store, "beCode": be, **cfg})
    data = res.get("data") or {}
    meals, cats = data.get("meals", {}), data.get("categories", [])
    items = []
    for cat in cats:
        cname = (cat.get("name") or "").replace("\n", " ").strip()
        for ref in cat.get("meals", []):
            code = ref.get("code")
            info = meals.get(code)
            if not info:
                continue
            items.append({
                "code": code,
                "name": info.get("name", code),
                "category": cname,
                "price": float(info.get("currentPrice") or 0),
                "originalPrice": float(info.get("originalPrice") or info.get("currentPrice") or 0),
                "discountType": info.get("discountType") or "",
                "tags": ref.get("tags") or [],
                "image": info.get("image", ""),
            })
    # 同一餐品可能挂在多个分类下，按 code 去重（保留首个分类）
    seen, uniq = set(), []
    for it in items:
        if it["code"] in seen:
            continue
        seen.add(it["code"])
        uniq.append(it)
    return {"channel": channel, "count": len(uniq), "items": uniq,
            "categories": [c.get("name", "").replace("\n", " ") for c in cats]}


# ---------------------------------------------------------------- 券
def _norm_coupon(raw: dict) -> dict:
    """把 MCP 券结构归一化成：门槛/用券价/渠道/星期/时段/名额。"""
    d = raw.get("discountInfo") or {}
    desc = d.get("discountDesc") or ""
    try:
        value = float(d.get("discountValue") or 0)
    except (TypeError, ValueError):
        value = 0.0
    kind = "price" if "价格" in desc else "cut"
    tags = [t.get("label", "") for t in (raw.get("tags") or [])]
    return {
        "id": raw.get("id"),
        "code": raw.get("code"),
        "title": raw.get("title"),
        "kind": kind,                       # price=用券价 / cut=立减或满减
        "value": value,
        "discountDesc": desc,
        "channels": raw.get("orderTypes") or [],   # [1]=到店 [2]=外送
        "beTypes": raw.get("beTypes") or [],
        "tags": tags,
        "weekdays": raw.get("weekdays") or [],     # 空=全周；1=周日 ... 7=周六
        "timePeriods": [(p.get("startTime", "")[:5], p.get("endTime", "")[:5])
                        for p in (raw.get("tradeTimePeriods") or [])],
        "dateRange": [raw.get("tradeStartDate"), raw.get("tradeEndDate")],
        "leftCount": raw.get("totalAvailableCount"),
        "orderLimit": raw.get("totalCount"),
        "datetimeText": raw.get("datetimeText", ""),
        "note": ";".join(t.get("text", "") for t in ((raw.get("instructions") or {}).get("labels") or [])),
    }


def fetch_coupons() -> dict:
    mine = mcd.call_with_retry("query-my-coupons", {})
    raw = ((mine.get("data") or {}).get("coupons")) or []
    coupons = [_norm_coupon(c) for c in raw]
    try:
        pool = mcd.call_with_retry("available-coupons", {}).get("data") or []
    except mcd.MCPError:
        pool = []
    return {
        "wallet": coupons,
        "claimable": [{"name": p.get("couponName"), "status": p.get("couponStatus"),
                       "label": p.get("label")} for p in pool],
    }


def store_coupons(store: str, be: str, channel: str) -> list:
    try:
        res = mcd.call_with_retry("query-store-coupons", {"storeCode": store, "beCode": be, **CHANNEL[channel]})
        return res.get("data") or []
    except mcd.MCPError as exc:
        print(f"[warn] 门店券查询失败：{exc}")
        return []


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="")
    ap.add_argument("--keyword", default="")
    ap.add_argument("--store", default="")
    ap.add_argument("--becode", default="")
    ap.add_argument("--channel", default="delivery", choices=list(CHANNEL))
    ap.add_argument("--outdir", default="data")
    args = ap.parse_args()

    mcd.initialize()
    os.makedirs(args.outdir, exist_ok=True)

    store_code, be_code, store_name, address = args.store, args.becode, "", ""
    if not store_code:
        if not (args.city and args.keyword):
            raise SystemExit("请提供 --city/--keyword 搜索门店，或直接 --store/--becode")
        cands = find_store(args.city, args.keyword)
        if not cands:
            raise SystemExit("没搜到门店，换个关键词试试。")
        store_code = cands[0]["storeCode"]
        store_name = cands[0]["storeName"]
        print(f"[门店] {store_code} {store_name} {cands[0].get('address','')}")
        if args.channel == "delivery" and not be_code:
            address = pick_address()
            mapping = store_be_code(address, 2)
            be_code = mapping.get(store_code, "")
            if not be_code:
                cand = [f"{k}->{v}" for k, v in mapping.items() if v]
                raise SystemExit(f"该门店不在配送范围内。附近可送门店：{cand}")
    if not be_code:
        be_code = f"{store_code}02"  # 麦乐送业务编码习惯：门店码 + 02

    now = mcd.call_with_retry("now-time-info", {}).get("data", {})
    payload = {
        "syncedAt": now.get("formatted") or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "store": {"storeCode": store_code, "beCode": be_code, "storeName": store_name,
                  "addressId": address, "channel": args.channel},
    }
    payload["menu"] = fetch_menu(store_code, be_code, args.channel)
    payload["coupons"] = fetch_coupons()
    payload["coupons"]["storeCoupons"] = store_coupons(store_code, be_code, args.channel)

    for name in ("store", "menu", "coupons"):
        path = os.path.join(args.outdir, f"{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload[name] if name != "menu" else payload["menu"], f, ensure_ascii=False, indent=2)
        print(f"[写出] {path}")

    m = payload["menu"]
    c = payload["coupons"]
    print(f"\n[汇总] 门店 {store_code}/{be_code} · 渠道 {args.channel} · "
          f"可售餐品 {m['count']} 个 · 券包 {len(c['wallet'])} 张 · "
          f"门店券 {len(c['storeCoupons'])} 张 · 可领 {len(c['claimable'])} 张")


if __name__ == "__main__":
    main()
