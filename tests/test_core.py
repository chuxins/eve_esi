"""核心纯逻辑单元测试（不依赖网络 / 数据库）。

覆盖：物品查询串解析、挂单价计算、流水描述翻译、物品名规范化、
OAuth PKCE / token 过期判断、ISK 格式化。
"""

import base64
import time

import pytest

from auth import _basic_auth, _pkce_pair, is_token_expired
from db import build_item_name_norm, normalize_item_name
from esi_client import translate_description
from main import fmt_isk
from market_price import _calc_prices, parse_item_query


# ---------------------------------------------------------------- parse_item_query

class TestParseItemQuery:
    def test_bare_name(self):
        assert parse_item_query("三钛合金") == ("三钛合金", None)

    def test_name_with_qty(self):
        assert parse_item_query("三钛合金*1000") == ("三钛合金", 1000)

    def test_star_empty_means_one(self):
        assert parse_item_query("三钛合金*") == ("三钛合金", 1)

    def test_thousands_separator(self):
        assert parse_item_query("三钛合金*1,000") == ("三钛合金", 1000)
        assert parse_item_query("三钛合金*1_000") == ("三钛合金", 1000)

    def test_fullwidth_star(self):
        assert parse_item_query("三钛合金＊100") == ("三钛合金", 100)

    def test_single_space_tail_number(self):
        assert parse_item_query("三钛 合金 1000") == ("三钛 合金", 1000)

    def test_multi_column_asterisk(self):
        # 名称*市场分类*数量：取第 1 列为名称、末列数字为数量
        assert parse_item_query("三钛*原材料*100") == ("三钛", 100)

    def test_table_paste(self):
        # 制表符 / 2+ 空格分列，'-' 占位列忽略，第一个纯数字列为数量
        assert parse_item_query("名称  100  -  -") == ("名称", 100)
        assert parse_item_query("名称\t200\t-\t-") == ("名称", 200)

    def test_zero_quantity_raises(self):
        with pytest.raises(ValueError):
            parse_item_query("三钛*0")

    def test_negative_quantity_raises(self):
        with pytest.raises(ValueError):
            parse_item_query("三钛*-5")

    def test_empty_name_raises(self):
        with pytest.raises(ValueError):
            parse_item_query("*100")


# ---------------------------------------------------------------- _calc_prices

class TestCalcPrices:
    @staticmethod
    def _order(price, is_buy, location):
        return {"price": price, "is_buy_order": is_buy, "location_id": location}

    JITA = 60003760
    OTHER = 60000001

    def test_jita_buy_and_sell(self):
        orders = [
            self._order(100.0, True, self.JITA),
            self._order(101.0, True, self.JITA),
            self._order(110.0, False, self.JITA),
            self._order(120.0, False, self.OTHER),
        ]
        r = _calc_prices(orders)
        assert r["scope"] == "Jita 4-4"
        assert r["buy"] == 101.0
        assert r["sell"] == 110.0
        assert r["mid"] == (101.0 + 110.0) / 2

    def test_buy_only(self):
        orders = [self._order(100.0, True, self.JITA)]
        r = _calc_prices(orders)
        assert r["scope"] == "Jita 4-4"
        assert r["buy"] == 100.0
        assert r["sell"] is None
        assert r["mid"] is None

    def test_region_fallback(self):
        orders = [self._order(50.0, True, self.OTHER),
                  self._order(60.0, False, self.OTHER)]
        r = _calc_prices(orders)
        assert r["scope"] == "The Forge 星域"
        assert r["buy"] == 50.0
        assert r["sell"] == 60.0

    def test_empty_orders(self):
        r = _calc_prices([])
        assert r["buy"] is None and r["sell"] is None and r["mid"] is None
        assert r["scope"] == "-"


# ---------------------------------------------------------------- translate_description

class TestTranslate:
    def test_player_donation(self):
        assert translate_description("alice deposited cash into bob's account") == \
            "alice 向 bob 的账户存入现金"

    def test_plain_donation(self):
        assert translate_description("Player donation") == "玩家捐赠"

    def test_market_escrow_release(self):
        assert translate_description("Market escrow release") == "市场托管释放"

    def test_scc_sales_tax(self):
        assert translate_description("Sales tax paid to the SCC") == "向 SCC 支付销售税"

    def test_unknown_unchanged(self):
        text = "Some unknown description here"
        assert translate_description(text) == text

    def test_none_safe(self):
        assert translate_description(None) is None


# ---------------------------------------------------------------- item name normalize

class TestItemNameNormalize:
    def test_cjk_spaces_removed(self):
        assert normalize_item_name("三钛 合金") == "三钛合金"

    def test_alnum_kept_lowercased(self):
        assert normalize_item_name("125mm Railgun I") == "125mmrailguni"

    def test_build_norm_key(self):
        assert build_item_name_norm("三钛合金", "Tritanium") == "|三钛合金|tritanium|"

    def test_build_norm_no_names(self):
        assert build_item_name_norm("", None) is None


# ---------------------------------------------------------------- auth

class TestAuth:
    def test_pkce_pair(self):
        verifier, challenge = _pkce_pair()
        assert isinstance(verifier, str) and len(verifier) > 40
        assert isinstance(challenge, str) and challenge

    def test_basic_auth_encoding(self):
        header = _basic_auth("client", "secret")
        decoded = base64.urlsafe_b64decode(header[len("Basic "):]).decode()
        assert decoded == "client:secret"

    def test_token_not_expired(self):
        assert is_token_expired({"expires_at": int(time.time()) + 3600}) is False

    def test_token_expired(self):
        assert is_token_expired({"expires_at": int(time.time()) - 1}) is True

    def test_missing_expiry_considered_expired(self):
        assert is_token_expired({}) is True


# ---------------------------------------------------------------- fmt_isk

class TestFmtIsk:
    def test_thousands(self):
        assert fmt_isk(1904579755.75) == "1,904,579,755.75"

    def test_zero(self):
        assert fmt_isk(0) == "0.00"
