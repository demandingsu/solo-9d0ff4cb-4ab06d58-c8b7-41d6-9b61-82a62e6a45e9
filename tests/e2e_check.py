"""端到端检查：本地 HTTP 服务托管样本站，调用 FastAPI 完整链路。

运行：python3 tests/e2e_check.py
"""
import functools
import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402

SITE_DIR = ROOT / "sample_site"


def start_site_server() -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def check(name: str, cond: bool, extra: str = ""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        raise SystemExit(f"检查失败: {name}")


def main():
    httpd, base = start_site_server()
    client = TestClient(app)
    catalog = f"{base}/index.html"
    print(f"样本站: {catalog}")

    # 1) 正常求解：MF-50 需求 180 → 2 包 100 片装，¥450
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 180})
    assert r.status_code == 200, r.text
    d = r.json()
    check("MF-50/180 状态 ok", d["status"] == "ok")
    check("总价 ¥450.00", d["solution"]["total_price"] == "450.00")
    check("实到 200 片", d["solution"]["total_pieces"] == 200)
    check("超量 20 片", d["solution"]["overage"] == 20)
    items = d["solution"]["items"]
    check("仅一种包装", len(items) == 1)
    check("100片装 ×2", items[0]["pack_size"] == 100 and items[0]["purchased_packs"] == 2)
    check("详情来源 URL", items[0]["detail_url"].endswith("/details/mf50-100.html"))

    # 2) 分页与跨页去重：3 个列表页都被抓取，mf50-50 只计一次
    check("抓取 3 个列表页", len(d["list_pages"]) == 3, str(d["list_pages"]))
    check("跨页重复跳过 1 次", d["duplicate_detail_links_skipped"] == 1)
    check("匹配详情 3 个", len(d["matched_detail_pages"]) == 3, str(d["matched_detail_pages"]))

    # 3) 混购 + 库存约束：需求 175 → 100+50+25 各 1 包，¥413
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 175})
    d = r.json()
    check("MF-50/175 状态 ok", d["status"] == "ok")
    check("混购 3 种包装", len(d["solution"]["items"]) == 3)
    check("总价 ¥413.00", d["solution"]["total_price"] == "413.00")
    check("实到 175 片（零超量）", d["solution"]["total_pieces"] == 175 and d["solution"]["overage"] == 0)

    # 4) 库存上限：需求 1000 → 恰好买光 10 包 100 片装
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 1000})
    d = r.json()
    check("MF-50/1000 状态 ok", d["status"] == "ok")
    check("总价 ¥2250.00", d["solution"]["total_price"] == "2250.00")
    check("实到 1000 片", d["solution"]["total_pieces"] == 1000)

    # 5) 库存不足：需求 4000 > 可购总量 3750 → 不给确定最低价
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 4000})
    d = r.json()
    check("MF-50/4000 状态 insufficient_stock", d["status"] == "insufficient_stock")
    check("无方案、无总价", d["solution"] is None)
    check("报告可购总量 3750", d["total_available_pieces"] == 3750)

    # 6) 匹配详情抓取失败：BROKEN-1 的详情页 404 → 不给确定最低价
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "BROKEN-1", "demand": 10})
    d = r.json()
    check("BROKEN-1 状态 scrape_failed", d["status"] == "scrape_failed")
    check("无方案", d["solution"] is None)
    check("报告失败详情页", d["failed_detail_pages"][0].endswith("/details/broken-404.html"))

    # 7) 无匹配型号（兜底扫描时 broken-404 无法核对，应在响应中如实标注）
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "NOPE-9", "demand": 10})
    d = r.json()
    check("NOPE-9 状态 no_matching_products", d["status"] == "no_matching_products")
    check("无方案", d["solution"] is None)
    check(
        "标注未能核对的页面",
        any(u.endswith("/details/broken-404.html") for u in d.get("unverified_failed_pages", [])),
    )

    # 8) 其他型号正常匹配：NY-66 需求 150 → 2 包 100 片装 ¥360
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "NY-66", "demand": 150})
    d = r.json()
    check("NY-66/150 状态 ok", d["status"] == "ok")
    check("总价 ¥360.00", d["solution"]["total_price"] == "360.00")

    # 8b) X-10 分页混合选择器：第 2 页混用 a.product-link 与 li.product-item a[href]，
    #     含重复的 50 片装链接和未标型号（"查看规格"）的 100 片装链接。
    #     需求 100 片 → 1 包 100 片装 ¥150，而不是 2 包 50 片装 ¥240。
    x10_catalog = f"{base}/x10/index.html"
    r = client.post("/api/v1/quote", json={"catalog_url": x10_catalog, "model": "X-10", "demand": 100})
    d = r.json()
    check("X-10/100 状态 ok", d["status"] == "ok")
    check("X-10/100 总价 ¥150.00（未误报 ¥240）", d["solution"]["total_price"] == "150.00")
    check("X-10/100 实到 100 片", d["solution"]["total_pieces"] == 100)
    check(
        "X-10/100 选择 100 片装 ×1",
        len(d["solution"]["items"]) == 1
        and d["solution"]["items"][0]["pack_size"] == 100
        and d["solution"]["items"][0]["purchased_packs"] == 1,
    )
    check("X-10 抓取 2 个列表页", len(d["list_pages"]) == 2, str(d["list_pages"]))
    check("X-10 跨页重复跳过 1 次", d["duplicate_detail_links_skipped"] == 1)
    check("X-10 匹配 2 个详情（重复只计一次）", len(d["matched_detail_pages"]) == 2)
    check(
        "未标型号的 100 片装详情被采集",
        any(u.endswith("/details/x10-100.html") for u in d["matched_detail_pages"]),
    )

    # 8c) 未标型号详情核对后确认是其他型号 → 不计入匹配结果
    r = client.post(
        "/api/v1/quote",
        json={"catalog_url": f"{base}/x10/verify-other-model.html", "model": "X-10", "demand": 50},
    )
    d = r.json()
    check("X-10 核对其他型号后状态 ok", d["status"] == "ok")
    check("仅 X-10 50 片装被计入，总价 ¥120.00", d["solution"]["total_price"] == "120.00")
    check("NY-66 详情不计入匹配结果", len(d["matched_detail_pages"]) == 1)

    # 8d) 待核对的未标型号详情 404 → 返回失败地址，不给确定报价
    r = client.post(
        "/api/v1/quote",
        json={"catalog_url": f"{base}/x10/verify-404.html", "model": "X-10", "demand": 50},
    )
    d = r.json()
    check("待核对详情 404 → scrape_failed", d["status"] == "scrape_failed")
    check("404 无方案", d["solution"] is None)
    check(
        "404 返回失败地址",
        any(u.endswith("/details/x10-missing.html") for u in d.get("failed_detail_pages", [])),
    )

    # 8e) 待核对的未标型号详情无法解析 → 返回失败地址，不给确定报价
    r = client.post(
        "/api/v1/quote",
        json={"catalog_url": f"{base}/x10/verify-malformed.html", "model": "X-10", "demand": 50},
    )
    d = r.json()
    check("待核对详情解析失败 → scrape_failed", d["status"] == "scrape_failed")
    check("解析失败无方案", d["solution"] is None)
    check(
        "解析失败返回失败地址",
        any(u.endswith("/details/x10-malformed.html") for u in d.get("failed_detail_pages", [])),
    )

    # 9) 目录不可达
    r = client.post("/api/v1/quote", json={"catalog_url": f"{base}/no-such-page.html", "model": "MF-50", "demand": 10})
    d = r.json()
    check("目录不存在状态 catalog_unreachable", d["status"] == "catalog_unreachable")

    # 10) 兜底扫描语义：列表文案不含型号时逐个核对详情页
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        (tmp_path / "details").mkdir()
        (tmp_path / "index.html").write_text(
            '<html><body><ul class="product-list">'
            '<li class="product-item"><a class="product-link" href="details/d1.html">特价滤膜 甲</a></li>'
            '<li class="product-item"><a class="product-link" href="details/d2.html">特价滤膜 乙</a></li>'
            "</ul></body></html>",
            encoding="utf-8",
        )
        (tmp_path / "details" / "d1.html").write_text(
            '<html><body><h1 class="product-title">特价滤膜 甲</h1>'
            '<table><tr><td class="product-model">FALL-1</td>'
            '<td class="pack-size">40</td>'
            '<td class="pack-price">¥90.00</td>'
            '<td class="stock-packs">8</td></tr></table></body></html>',
            encoding="utf-8",
        )
        # d2.html 故意不存在（404）
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=tmp)
        httpd2 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd2.serve_forever, daemon=True).start()
        cat2 = f"http://127.0.0.1:{httpd2.server_address[1]}/index.html"

        # 10a) 详情页型号匹配（FALL-1 只在详情页出现）→ 命中，但 d2 未能核对 → 不给确定最低价
        r = client.post("/api/v1/quote", json={"catalog_url": cat2, "model": "FALL-1", "demand": 40})
        d = r.json()
        check("兜底命中但存在未核对页 → scrape_failed", d["status"] == "scrape_failed")
        check("无方案", d["solution"] is None)
        check("失败页为 d2", d["failed_detail_pages"][0].endswith("/details/d2.html"))

        # 10b) 全部不匹配 → no_matching_products 并标注未核对页
        r = client.post("/api/v1/quote", json={"catalog_url": cat2, "model": "ABSENT-7", "demand": 40})
        d = r.json()
        check("兜底无匹配 → no_matching_products", d["status"] == "no_matching_products")
        check("标注未核对页 d2", any(u.endswith("/details/d2.html") for u in d.get("unverified_failed_pages", [])))
        httpd2.shutdown()

    httpd.shutdown()
    print("\n全部端到端检查通过 ✓")


if __name__ == "__main__":
    main()
