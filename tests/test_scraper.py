"""抓取器单元测试：python3 -m unittest discover -s tests -v"""
import pathlib
import sys
import unittest

from bs4 import BeautifulSoup

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

from scraper import _mentions_other_model, _product_links, normalize_model  # noqa: E402
from scraper import parse_detail_page, ScrapeError  # noqa: E402


class TestProductLinks(unittest.TestCase):
    def test_union_of_mixed_selectors(self):
        # 同一页混用 a.product-link 与 li.product-item a[href]：两种都要采集
        soup = BeautifulSoup(
            """
            <ul class="product-list">
              <li class="product-item">
                <a class="product-link" href="a.html">X-10 50片装</a>
              </li>
              <li class="product-item">
                <a href="b.html">查看规格</a>
              </li>
            </ul>
            """,
            "html.parser",
        )
        hrefs = [a["href"] for a in _product_links(soup)]
        self.assertEqual(hrefs, ["a.html", "b.html"])

    def test_same_anchor_hit_by_multiple_selectors_once(self):
        soup = BeautifulSoup(
            """
            <ul class="product-list">
              <li class="product-item">
                <a class="product-link" href="a.html">X-10 50片装</a>
              </li>
            </ul>
            """,
            "html.parser",
        )
        hrefs = [a["href"] for a in _product_links(soup)]
        self.assertEqual(hrefs, ["a.html"])


class TestModelMention(unittest.TestCase):
    def test_unlabeled_link_needs_detail_check(self):
        self.assertFalse(_mentions_other_model("查看规格", normalize_model("X-10")))

    def test_explicit_other_model_skipped(self):
        self.assertTrue(_mentions_other_model("NY-66 滤膜", normalize_model("X-10")))

    def test_target_link_is_not_other(self):
        self.assertFalse(_mentions_other_model("X-10 50片装", normalize_model("X-10")))


class TestParseDetail(unittest.TestCase):
    def test_missing_required_fields_raises(self):
        soup = BeautifulSoup(
            """
            <h1 class="product-title">X-10（字段缺失）</h1>
            <table>
              <tr><td class="product-model">X-10</td></tr>
              <tr><td class="pack-size">100</td></tr>
            </table>
            """,
            "html.parser",
        )
        with self.assertRaises(ScrapeError):
            parse_detail_page("http://example.test/details/missing.html", soup)


if __name__ == "__main__":
    unittest.main()
