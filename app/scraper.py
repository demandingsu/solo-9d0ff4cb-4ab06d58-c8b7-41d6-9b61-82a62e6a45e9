"""供应商目录抓取：同站分页列表 + 匹配型号详情页解析（requests + BeautifulSoup）。

抓取流程：
1. 从目录列表页出发，沿"下一页"链接在同站内翻页（去重、限页数）；
2. 每页合并所有详情链接选择器（a.product-link / li.product-item a[href] 等），
   同一详情 URL 跨页重复出现只计一次；
3. 列表文案含目标型号的链接，以及文案未标型号（如"查看规格"）而需要核对
   详情页的链接，均抓取详情并以详情页型号为准核对；
4. 待核对的详情页抓取或解析失败时，记录失败地址（调用方据此不给出确定
   最低价）；详情页确认是其他型号的，不计入匹配结果。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

MAX_PAGES = 50          # 分页抓取上限，防止死循环或异常站点
REQUEST_TIMEOUT = 10    # 单次请求超时（秒）
USER_AGENT = "membrane-procurement-bot/1.0"

# 列表页中详情链接的 CSS 选择器；同一页可能混用多种写法，需全部合并
PRODUCT_LINK_SELECTORS = [
    "a.product-link",
    "li.product-item a[href]",
    ".product-list a[href]",
]
NEXT_TEXT_RE = re.compile(r"(下一页|下页|next|»|›)", re.I)
NUM_RE = re.compile(r"([\d,]+(?:\.\d+)?)")
# 列表文案中的型号样式（如 X-10、MF50、BROKEN-1）；用于识别"其他型号"条目
MODEL_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*[-_.]?[0-9]+")


class ScrapeError(Exception):
    """抓取或解析失败。"""


@dataclass
class OfferInfo:
    """一个匹配详情页解析出的采购信息。"""

    detail_url: str
    title: str
    model: str
    pack_size: int    # 每包片数
    price_fen: int    # 含税包价（分）
    stock_packs: int  # 可购包数


@dataclass
class CrawlResult:
    list_pages: list[str]           # 实际抓取到的列表页 URL（按顺序）
    matched: list[OfferInfo]        # 匹配型号且解析成功的详情
    failed_details: list[str]       # 列表文案已含目标型号、但抓取/解析失败的详情 URL
    duplicate_details: int          # 跨页重复出现被跳过的详情链接数
    unverified_failed: list[str]    # 文案未标型号、待核对但抓取/解析失败的详情 URL


def normalize_model(text: str) -> str:
    """型号归一化：去空白/连字符/下划线并转大写，用于包含匹配。"""
    return re.sub(r"[\s\-_]+", "", text or "").upper()


def _abs_url(href: str, base: str) -> str:
    return urldefrag(urljoin(base, href))[0]


def _same_site(url: str, netloc: str) -> bool:
    return urlparse(url).netloc == netloc


def _get_soup(session: requests.Session, url: str) -> BeautifulSoup:
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise ScrapeError(f"页面抓取失败 {url}: {exc}") from exc
    # 用原始字节交给 BeautifulSoup，按页面 <meta charset> 自动识别编码
    return BeautifulSoup(resp.content, "html.parser")


def _product_links(soup: BeautifulSoup):
    """合并所有详情链接选择器的结果（同一页可能混用多种链接写法）。

    同一 <a> 可能被多个选择器命中，按 DOM 对象身份去重，避免把同页同一
    链接误算成"跨页重复"。
    """
    links = []
    seen_ids: set[int] = set()
    for sel in PRODUCT_LINK_SELECTORS:
        for a in soup.select(sel):
            if id(a) in seen_ids:
                continue
            seen_ids.add(id(a))
            links.append(a)
    return links


def _mentions_other_model(text: str, target: str) -> bool:
    """列表文案是否明确写了非目标型号。

    文案不含目标且出现型号样式（如 X-10 / BROKEN-1）时，可在列表阶段
    直接判定为其他型号，无需抓取详情；"查看规格"这类未标型号的文案
    返回 False，必须抓取详情核对。
    """
    if target in normalize_model(text):
        return False
    return bool(MODEL_TOKEN_RE.search(text or ""))


def _next_page_url(soup: BeautifulSoup, current_url: str, netloc: str):
    """在同站内寻找"下一页"链接：rel=next / class=next / 分页区文本匹配。"""
    candidates = []
    for a in soup.find_all("a", href=True):
        rel = a.get("rel") or []
        classes = a.get("class") or []
        if "next" in rel or "next" in classes:
            candidates.append(a)
    if not candidates:
        for a in soup.select(".pagination a[href]"):
            if NEXT_TEXT_RE.search(a.get_text(strip=True)):
                candidates.append(a)
    for a in candidates:
        url = _abs_url(a["href"], current_url)
        if _same_site(url, netloc) and url != current_url:
            return url
    return None


def _pick_text(soup: BeautifulSoup, selectors: list[str]):
    for sel in selectors:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)
    return None


def _to_fen(num_str: str) -> int:
    d = Decimal(num_str.replace(",", ""))
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_detail_page(url: str, soup: BeautifulSoup) -> OfferInfo:
    """解析详情页：型号、每包片数、含税包价、可购包数。缺关键字段视为解析失败。"""
    page_text = soup.get_text("\n", strip=True)

    title = _pick_text(soup, ["h1.product-title", "h1"])
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    title = title or ""

    model = _pick_text(soup, [".product-model", ".model", '[data-field="model"]'])
    if not model:
        m = re.search(r"型号\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9\-_]*)", page_text)
        model = m.group(1) if m else ""

    pack_size = None
    raw = _pick_text(soup, [".pack-size", '[data-field="pack-size"]'])
    if raw:
        m = NUM_RE.search(raw)
        pack_size = int(m.group(1).replace(",", "")) if m else None
    if pack_size is None:
        m = re.search(r"每包片数\s*[:：]?\s*(\d+)", page_text) or re.search(r"(\d+)\s*片\s*/\s*包", page_text)
        pack_size = int(m.group(1)) if m else None

    price_fen = None
    raw = _pick_text(soup, [".pack-price", ".price", '[data-field="price"]'])
    if raw:
        m = NUM_RE.search(raw)
        price_fen = _to_fen(m.group(1)) if m else None
    if price_fen is None:
        m = re.search(r"含税包价\s*[:：]?\s*[¥￥]?\s*([\d,]+(?:\.\d+)?)", page_text)
        price_fen = _to_fen(m.group(1)) if m else None

    stock_packs = None
    raw = _pick_text(soup, [".stock-packs", ".stock", '[data-field="stock"]'])
    if raw:
        m = NUM_RE.search(raw)
        stock_packs = int(m.group(1).replace(",", "")) if m else None
    if stock_packs is None:
        m = re.search(r"可购包数\s*[:：]?\s*(\d+)", page_text)
        stock_packs = int(m.group(1)) if m else None

    missing = [
        name
        for name, val in [("每包片数", pack_size), ("含税包价", price_fen), ("可购包数", stock_packs)]
        if val is None
    ]
    if missing:
        raise ScrapeError(f"详情页解析失败 {url}: 缺少字段 {'、'.join(missing)}")

    return OfferInfo(
        detail_url=url,
        title=title,
        model=model,
        pack_size=pack_size,      # type: ignore[arg-type]
        price_fen=price_fen,      # type: ignore[arg-type]
        stock_packs=stock_packs,  # type: ignore[arg-type]
    )


def crawl_catalog(catalog_url: str, model: str) -> CrawlResult:
    """抓取目录分页并收集匹配型号的详情。列表页抓取失败会抛 ScrapeError。"""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    netloc = urlparse(catalog_url).netloc
    target = normalize_model(model)

    list_pages: list[str] = []
    entries: list[tuple[str, str]] = []  # (详情 URL, 列表文案)
    seen_details: set[str] = set()
    duplicates = 0

    url: str | None = catalog_url
    seen_pages: set[str] = set()
    while url and url not in seen_pages and len(list_pages) < MAX_PAGES:
        seen_pages.add(url)
        soup = _get_soup(session, url)  # 列表页失败：整个目录不可用，直接抛错
        list_pages.append(url)
        page_urls: set[str] = set()
        for a in _product_links(soup):
            href = a.get("href")
            if not href:
                continue
            detail_url = _abs_url(href, url)
            if not _same_site(detail_url, netloc):
                continue  # 只接受同站详情链接
            if detail_url in page_urls:
                continue  # 同页重复（含同一 <a> 被多个选择器命中）
            page_urls.add(detail_url)
            if detail_url in seen_details:
                duplicates += 1  # 同一详情跨页重复出现，只计一次
                continue
            seen_details.add(detail_url)
            entries.append((detail_url, a.get_text(" ", strip=True)))
        url = _next_page_url(soup, url, netloc)

    # 列表文案已含目标型号 → 必抓详情核对；
    # 文案未标型号（如"查看规格"）→ 也可能命中，不能因为同页有带型号的
    # 链接就跳过，需抓取详情核对；文案明确写了其他型号 → 不计入。
    # 若所有文案都没出现目标型号，则退回全量核对（无法确定目标是否只在
    # 详情页出现），此时无法核对的页面记为 unverified_failed。
    text_matched = [(u, t) for u, t in entries if target in normalize_model(t)]
    fallback_scan = not text_matched and bool(entries)
    if fallback_scan:
        candidates = entries
    else:
        candidates = [
            (u, t)
            for u, t in entries
            if target in normalize_model(t) or not _mentions_other_model(t, target)
        ]

    matched: list[OfferInfo] = []
    failed: list[str] = []
    unverified: list[str] = []
    for detail_url, link_text in candidates:
        confirmed_by_text = target in normalize_model(link_text)
        try:
            soup = _get_soup(session, detail_url)
            offer = parse_detail_page(detail_url, soup)
        except ScrapeError:
            if confirmed_by_text:
                # 列表文案已确认匹配的详情抓取/解析失败：无法保证最低价确定性
                failed.append(detail_url)
            else:
                # 文案未标型号、待核对但无法读取/解析：无法确认是否匹配
                unverified.append(detail_url)
            continue

        detail_model = normalize_model(offer.model)
        if detail_model:
            is_match = target in detail_model
        else:
            # 详情页没有型号字段时，退回用标题核对
            is_match = target in normalize_model(offer.title)
        if is_match:
            matched.append(offer)
        # 详情页确认是其他型号的，不计入匹配结果

    return CrawlResult(
        list_pages=list_pages,
        matched=matched,
        failed_details=failed,
        duplicate_details=duplicates,
        unverified_failed=unverified,
    )
