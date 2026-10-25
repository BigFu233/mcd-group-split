#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦拼单官 · 图形版本地服务

零第三方依赖，只用 Python 标准库：
    python3 web/server.py            # 打开 http://127.0.0.1:8765

职责：
- 托管前端页面（web/index.html）
- /api/snapshot  读取本地菜单/券包快照，页面秒开
- /api/sync      调麦当劳 MCP 拉取门店 + 菜单 + 券包（需要 Token）
- /api/settle    复用 scripts/split_engine.py 的算法，返回结算结果
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import mcd_client as mcd            # noqa: E402
import menu_sync as sync            # noqa: E402
import split_engine as engine       # noqa: E402

DATA = os.path.join(ROOT, "data")
WEB = os.path.join(ROOT, "web")
FALLBACK_COUPONS = os.path.join(ROOT, "examples", "coupons.example.json")

# 允许直接访问的静态资源（白名单，避免任意文件读取）
STATIC = {
    "/manifest.json": "application/manifest+json; charset=utf-8",
    "/icon-192.png": "image/png",
    "/icon-512.png": "image/png",
    "/apple-touch-icon.png": "image/png",
}


# ---------------------------------------------------------------- 数据
def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def snapshot() -> dict:
    store = load_json(os.path.join(DATA, "store.json"), {})
    menu = load_json(os.path.join(DATA, "menu.json"), {"items": [], "count": 0})
    coupons_path = os.path.join(DATA, "coupons.json")
    if not os.path.exists(coupons_path):
        coupons_path = FALLBACK_COUPONS
    coupons = load_json(coupons_path, {"wallet": [], "claimable": []})
    return {"store": store, "menu": menu, "coupons": coupons,
            "hasLiveCoupons": os.path.exists(os.path.join(DATA, "coupons.json"))}


def do_sync(body: dict) -> dict:
    token = (body.get("token") or "").strip()
    if token:
        os.environ["MCD_MCP_TOKEN"] = token
    channel = body.get("channel") or "delivery"
    cfg = sync.CHANNEL[channel]

    store_code = (body.get("store") or "").strip()
    be_code = (body.get("becode") or "").strip()
    store_name = body.get("storeName") or ""
    address = ""
    if not store_code:
        city, keyword = (body.get("city") or "").strip(), (body.get("keyword") or "").strip()
        if not (city and keyword):
            raise ValueError("请填写城市和门店关键词，或直接填门店编码")
        cands = sync.find_store(city, keyword)
        if not cands:
            raise ValueError("没搜到门店，换个关键词试试")
        # 名称命中关键词的优先，其次按距离由近到远
        cands.sort(key=lambda c: (0 if keyword in (c.get("storeName") or "") else 1,
                                  c.get("distance") or 9e9))
        if channel == "delivery":
            address = sync.pick_address()
            mapping = sync.store_be_code(address, 2)
            hit = next(((c, mapping[c["storeCode"]]) for c in cands if mapping.get(c["storeCode"])), None)
            if not hit:
                names = "、".join(c["storeName"] for c in cands[:4])
                raise ValueError(f"搜到的门店（{names}）都不在你填的地址配送范围内，换个关键词或改到店自取")
            store, be_code = hit[0], hit[1]
            store_code, store_name = store["storeCode"], store.get("storeName", "")
        else:
            store_code = cands[0]["storeCode"]
            store_name = cands[0].get("storeName", "")
    if not be_code:
        be_code = f"{store_code}02"

    now = mcd.call_with_retry("now-time-info", {}).get("data", {})
    menu = sync.fetch_menu(store_code, be_code, channel)
    coupons = sync.fetch_coupons()
    coupons["storeCoupons"] = sync.store_coupons(store_code, be_code, channel)

    store = {"storeCode": store_code, "beCode": be_code, "storeName": store_name,
             "addressId": "", "channel": channel,
             "syncedAt": now.get("formatted", "")}

    # 默认无状态：数据只回给发起请求的那台手机，服务端不落盘。
    # 这样多人共用同一个公网链接时，谁也不会看到别人的券码。
    if os.environ.get("MCD_PERSIST") == "1":
        os.makedirs(DATA, exist_ok=True)
        for name, payload in (("store", store), ("menu", menu), ("coupons", coupons)):
            with open(os.path.join(DATA, f"{name}.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

    return {"store": store, "menu": menu, "coupons": coupons, "hasLiveCoupons": True}


def do_verify(body: dict) -> dict:
    """只校验 Token 是否有效，顺手返回券包数量，用于首次引导。"""
    token = (body.get("token") or "").strip()
    if not token:
        raise ValueError("请先粘贴 Token")
    os.environ["MCD_MCP_TOKEN"] = token
    mcd.initialize()
    now = mcd.call_with_retry("now-time-info", {}).get("data", {})
    coupons = mcd.call_with_retry("query-my-coupons", {}).get("data", {}) or {}
    wallet = coupons.get("coupons") or []
    return {"ok": True, "serverTime": now.get("formatted", ""),
            "couponCount": len(wallet),
            "titles": [c.get("title") for c in wallet][:8]}


def do_settle(order: dict) -> dict:
    import datetime as dt
    order.setdefault("title", "麦当劳拼单结算单")
    order.setdefault("channel", "delivery")
    order.setdefault("mode", "proportional")
    if not order.get("now"):
        order["now"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    if not order.get("members"):
        order["members"] = []
    if not order.get("initiator"):
        order["initiator"] = order["members"][0]["nick"] if order["members"] else "我"
    result = engine.build(order)
    return {
        "ok": True,
        "lines": [{**ln, "price": str(ln["price"]), "subtotal": str(ln["subtotal"])}
                  for ln in result["lines"]],
        "members": [{**m, "orig": str(m["orig"]), "share": str(m["share"]),
                     "pay": str(m["pay"]), "budget": str(m["budget"])}
                    for m in result["members"]],
        "total": str(result["total"]),
        "discount": str(result["discount"]),
        "payable": str(result["payable"]),
        "used": [{"title": u["coupon"]["title"], "owner": u["line"]["owner"],
                  "item": u["line"]["name"], "saving": str(u["saving"])} for u in result["used"]],
        "skipped": [{"title": s["title"], "reason": s["reason"]} for s in result["skipped"]],
        "tips": result["tips"],
        "mods": result["mods"],
        "markdown": engine.render(order, result),
        "share": engine.render_share(order, result),
        "card": engine.render_html(order, result),
    }


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "McGroupSplit/1.0"

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *args):  # 安静一点
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(WEB, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path in STATIC:
            with open(os.path.join(WEB, path.lstrip("/")), "rb") as f:
                return self._send(200, f.read(), STATIC[path])
        if path == "/api/snapshot":
            return self._json(200, snapshot())
        if path == "/api/health":
            return self._json(200, {"ok": True, "tokenSet": bool(os.environ.get("MCD_MCP_TOKEN"))})
        if path == "/api/demo":
            demo = load_json(os.path.join(ROOT, "examples", "order_demo.json"), {})
            demo.pop("couponsFile", None)   # 前端直接用当前券包
            return self._json(200, {"ok": True, "order": demo})
        return self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/sync":
                return self._json(200, {"ok": True, "data": do_sync(body)})
            if path == "/api/verify":
                return self._json(200, do_verify(body))
            if path == "/api/settle":
                return self._json(200, do_settle(body.get("order") or {}))
            return self._json(404, {"ok": False, "error": "not found"})
        except mcd.MCPError as exc:
            return self._json(400, {"ok": False, "error": str(exc)})
        except ValueError as exc:
            return self._json(400, {"ok": False, "error": str(exc)})
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._json(500, {"ok": False, "error": "服务端异常，请看终端日志"})


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8765"))
    host = os.environ.get("HOST", "0.0.0.0")
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"🍟 麦麦拼单官 · 图形版已启动： http://127.0.0.1:{port}")
    if host == "0.0.0.0":
        print("   手机同一 Wi-Fi 下可用「电脑内网 IP:端口」访问")
    print("   按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
