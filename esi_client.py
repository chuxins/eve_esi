"""EVE ESI API 客户端。

提供对以下 ESI 端点的封装：
- GET /v1/characters/{character_id}/wallet/        钱包当前余额
- GET /v4/characters/{character_id}/wallet/journal/ 钱包变动流水
"""

import re

import requests

ESI_BASE = "https://esi.evetech.net"

# ---------------------------------------------------------------- 描述翻译

# (正则, 中文模板)。模板用 {0}/{1}... 占位捕获组；无占位则为纯中文。
# 注意：带 paid 的规则必须放在通用 paid 规则之前，避免把
# "Fee ... paid from ... to ..." / "Insurance paid by ..." 等误翻。
_TRANSLATION_RULES = [
    (re.compile(r"^(.+?) deposited cash into (.+?)'s account$"),
     "{0} 向 {1} 的账户存入现金"),
    (re.compile(r"^(.+?) got bounty prizes for killing pirates in (.+)$"),
     "{0} 在 {1} 星系击杀海盗获得赏金"),
    (re.compile(r"^Market escrow release$"), "市场托管释放"),
    (re.compile(r"^Contract Broker's Fee$"), "合同经纪人手续费"),
    (re.compile(r"^Price for accepting a contract$"), "接受合同付款"),
    (re.compile(r"^Direct trade between (.+) and (.+)$"), "{0} 与 {1} 直接交易"),
    (re.compile(r"^Player donation$"), "玩家捐赠"),
    (re.compile(r"^Corporation account withdrawal$"), "公司账户提款"),
    (re.compile(r"^Corporation account deposit$"), "公司账户存款"),
    (re.compile(r"^Sales tax$"), "销售税"),
    (re.compile(r"^Transaction tax$"), "交易税"),
    (re.compile(r"^Broker fee$"), "经纪费"),
    (re.compile(r"^Insurance payout$"), "保险赔付"),
    (re.compile(r"^Mission reward$"), "任务奖励"),
    (re.compile(r"^Agent reward$"), "代理人奖励"),
    (re.compile(r"^Escrow$"), "托管"),
    (re.compile(r"^Bounty paid by (.+)$"), "{0} 支付的赏金"),
    # ---- 任务 / 系统相关 ----
    (re.compile(r"^Mission reward from agent (.+) to (.+)$"),
     "代理人 {0} 发放给 {1} 的任务奖励"),
    (re.compile(r"^Mission reward bonus from agent (.+) to (.+)$"),
     "代理人 {0} 发放给 {1} 的任务奖励加成"),
    (re.compile(r"^Mission collateral refunded by agent (.+) to\s+(.+)$"),
     "代理人 {0} 退还给 {1} 的任务抵押金"),
    (re.compile(r"^Mission collateral paid by (.+) to agent (.+)$"),
     "{0} 向代理人 {1} 支付任务抵押金"),
    (re.compile(r"^Encounter Surveillance System in (.+) transferred funds to (.+)$"),
     "{0} 的事件监测装置向 {1} 转账"),
    (re.compile(r"^Repair bill between (.+) and (.+)$"),
     "{0} 与 {1} 的维修账单"),
    (re.compile(r"^Reward for completion of an AIR Career Program goal$"),
     "完成 AIR 生涯计划目标奖励"),
    (re.compile(r"^Reward for redeeming ISK token$"),
     "兑换 ISK 代币奖励"),
    (re.compile(r"^Reward deposited for completing a contract$"),
     "完成合同获得的奖励"),
    (re.compile(r"^Reward given for scientific contribution to Project Discovery\.$"),
     "为 Project Discovery 科学贡献发放的奖励"),
    (re.compile(r"^Market order commission to broker authorized by: (.+)$"),
     "{0} 授权的经纪人市场订单佣金"),
    (re.compile(r"^Market: (.+) bought stuff from (.+)$"),
     "市场：{0} 从 {1} 购买了物品"),
    (re.compile(r"^Payment to LP Store$"), "向 LP 商店付款"),
    (re.compile(r"^Planetary Construction: (.+) built on (.+)$"),
     "行星开发：{0} 在 {1} 建造"),
    (re.compile(r"^Planetary Export Tax: (.+) exported from (.+)$"),
     "行星出口税：{0} 从 {1} 出口"),
    (re.compile(r"^Planetary Import Tax: (.+) imported to (.+)$"),
     "行星进口税：{0} 向 {1} 进口"),
    (re.compile(r"^(.+) transferred cash from (.+)'s corporate account to (.+)'s account$"),
     "{0} 从 {1} 的公司账户向 {2} 的账户转账"),
    (re.compile(r"^(.+) purchased a skill \((.+)\)$"),
     "{0} 购买了技能（{1}）"),
    (re.compile(r"^Manufacturing job fee between (.+) and (.+) \(Job ID: (.+)\)$"),
     "{0} 与 {1} 的制造工作费用（任务ID: {2}）"),
    (re.compile(r"^Inheritance from (.+)$"), "来自 {0} 的遗产"),
    (re.compile(r"^Welcome to New Eden!$"), "欢迎来到新伊甸！"),
    (re.compile(r"^-\s*$"), "无描述"),
    # ---- 带 paid 的具体规则（必须在通用 paid 之前）----
    (re.compile(r"^Fee for activating a Upwell Jump Bridge paid from (.+) to (.+)$"),
     "{0} 向 {1} 支付启动跃迁跳桥的费用"),
    (re.compile(r"^Fee for activating a Jump Clone paid from (.+) to (.+)$"),
     "{0} 向 {1} 支付启动跳跃克隆的费用"),
    (re.compile(r"^Fee for installing a Jump Clone paid from (.+) to (.+)$"),
     "{0} 向 {1} 支付安装跳跃克隆的费用"),
    (re.compile(r"^Fee paid by (.+) for use of (.+) reprocessing facility$"),
     "{0} 支付使用 {1} 精炼设施的费用"),
    (re.compile(r"^Sales tax paid to the SCC$"), "向 SCC 支付销售税"),
    (re.compile(r"^Insurance paid by EVE Central Bank to (.+) covering loss of a (.+)$"),
     "EVE 中央银行因 {1} 损失向 {0} 支付保险赔付"),
    (re.compile(r"^Insurance paid by (.+) to (.+) for ship (.+)$"),
     "{0} 向 {1} 支付船只保险：{2}"),
    (re.compile(r"^(.+?) bought from (.+)$"), "{0} 从 {1} 购买"),
    (re.compile(r"^(.+?) sold to (.+)$"), "{0} 出售给 {1}"),
]


def translate_description(desc):
    """将 ESI 英文描述翻译为中文（未匹配时保留原文）。"""
    if not desc:
        return desc
    text = str(desc).strip()
    for pattern, template in _TRANSLATION_RULES:
        m = pattern.match(text)
        if m:
            groups = m.groups()
            if template and groups:
                try:
                    return template.format(*groups)
                except (IndexError, KeyError):
                    return text
            return template
    return text


class ESIError(RuntimeError):
    """ESI 请求相关的错误。"""


class ESIClient:
    def __init__(self, access_token=None, user_agent="eve-wallet-tracker/1.0"):
        """access_token 为 None 时只访问公开端点（不带 Authorization 头）。"""
        self.access_token = access_token
        self.user_agent = user_agent

    def _headers(self):
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _get_response(self, path, params=None, allow_404=False):
        """发起请求并返回响应对象（allow_404=True 且命中 404 时返回 None）。"""
        url = f"{ESI_BASE}{path}"
        resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code != 200:
            raise ESIError(f"ESI 请求失败：{url} -> HTTP {resp.status_code} - {resp.text[:200]}")
        return resp

    def _get(self, path, params=None, allow_404=False):
        resp = self._get_response(path, params=params, allow_404=allow_404)
        return None if resp is None else resp.json()

    def _post(self, path, payload, params=None):
        """发起 POST 请求（用于 /v1/universe/ids/ 等端点）。"""
        url = f"{ESI_BASE}{path}"
        resp = requests.post(
            url, headers=self._headers(), params=params, json=payload, timeout=30
        )
        if resp.status_code != 200:
            raise ESIError(f"ESI 请求失败：{url} -> HTTP {resp.status_code} - {resp.text[:200]}")
        return resp.json()

    # ------------------------------------------------------------ 钱包余额

    def get_wallet_balance(self, character_id):
        """返回钱包当前余额（float，单位 ISK）。"""
        return self._get(f"/v1/characters/{character_id}/wallet/")

    # ------------------------------------------------------------ 钱包流水

    def get_wallet_journal(self, character_id, limit=50):
        """获取钱包变动流水（journal），按时间倒序。

        返回条目列表，每条包含：amount、balance、date、description、
        ref_id、first_party_id、second_party_id、tax 等字段。
        limit 为最多返回的条数。
        """
        entries = []
        page = 1
        while len(entries) < limit:
            batch = self._get(
                f"/v4/characters/{character_id}/wallet/journal/",
                params={"page": page},
            )
            if not batch:
                break
            entries.extend(batch)
            page += 1
            # 安全保护：防止异常端点返回过多分页
            if page > 20:
                break
        return entries[:limit]

    def sync_wallet_journal(self, character_id, since_ref_id=None, max_pages=20):
        """增量同步钱包流水，返回 ref_id 大于 since_ref_id 的条目（时间倒序）。

        - ``since_ref_id`` 为 None：全量拉取（首次同步），最多 max_pages 页；
        - 否则从第 1 页开始，只要该页出现已入库的流水就停止翻页，
          正常情况（只有少量新流水）只需 1 个请求；
        - 用响应的 ``X-Pages`` 头判断末页，避免用“翻到 404”探测而多一个请求
          并消耗 ESI 错误配额。

        返回的是 ESI 原始条目列表（已按 ref_id 过滤），由调用方入库去重。
        """
        collected = []
        page = 1
        total_pages = None
        threshold = None if since_ref_id is None else int(since_ref_id)

        while page <= max_pages:
            resp = self._get_response(
                f"/v4/characters/{character_id}/wallet/journal/",
                params={"page": page},
                allow_404=True,  # 兼容部分时刻 X-Pages 缺失的极端情况
            )
            if resp is None:
                break
            if total_pages is None:
                try:
                    total_pages = max(1, int(resp.headers.get("X-Pages") or 1))
                except (TypeError, ValueError):
                    total_pages = 1
            batch = resp.json() or []
            if not batch:
                break
            if threshold is None:
                collected.extend(batch)
            else:
                fresh = [
                    e for e in batch
                    if int(e.get("id") or e.get("ref_id") or 0) > threshold
                ]
                collected.extend(fresh)
                if len(fresh) < len(batch):
                    break  # 本页已含历史流水，后续页只会更旧
            if page >= total_pages:
                break
            page += 1

        return collected

    # ------------------------------------------------------------ 市场交易

    def get_wallet_transactions(self, character_id):
        """获取角色的全部市场交易详情（wallet transactions）。

        返回条目列表，每条包含：transaction_id、journal_ref_id、type_id、
        location_id、client_id、date、is_buy、is_personal、quantity、unit_price。
        """
        # from_id=0 显式请求完整交易列表；默认省略时 ESI 可能只返回部分较新/较旧记录。
        return self._get(
            f"/v3/characters/{character_id}/wallet/transactions/",
            params={"from_id": 0},
        )

    def get_universe_type(self, type_id):
        """获取 EVE 物品类型信息（用于 type_id -> 中文名称翻译）。"""
        return self._get(
            f"/v3/universe/types/{int(type_id)}/",
            params={"language": "zh"},
        )

    def resolve_names(self, names, language=None):
        """名称 → ID（POST /v1/universe/ids/）；language 可选 zh/en。"""
        params = {"language": language} if language else None
        return self._post("/v1/universe/ids/", list(names), params=params)

    def resolve_ids(self, ids):
        """ID → 名称（POST /latest/universe/names/，公开端点，单次最多 1000 个）。

        返回 [{"id":..., "name":..., "category":...}, ...]，未知 id 不会出现在结果里。
        """
        ids = [int(i) for i in ids if i]
        if not ids:
            return []
        return self._post("/latest/universe/names/", ids) or []

    def get_region_ids(self):
        """全部星域 ID 列表（GET /latest/universe/regions/，公开端点）。"""
        return [int(r) for r in (self._get("/latest/universe/regions/") or [])]

    def get_character_fittings(self, character_id):
        """获取角色已保存的装配方案（需 esi-fittings.read_fittings.v1）。

        注意：ESI 只提供角色个人装配，军团共享装配没有接口。
        """
        return self._get(f"/v2/characters/{int(character_id)}/fittings/") or []

    # ------------------------------------------------------------ 市场行情（公开端点）

    def get_region_orders(self, region_id, type_id, order_type=None, max_pages=5):
        """获取星域内某物品的挂单（默认买/卖一并返回）。

        用响应头 X-Pages 判断末页；max_pages 上限防止异常数据导致翻页过多。
        """
        orders = []
        page = 1
        while page <= max_pages:
            params = {"type_id": int(type_id), "page": page}
            if order_type:
                params["order_type"] = order_type
            resp = self._get_response(
                f"/v1/markets/{int(region_id)}/orders/", params=params
            )
            orders.extend(resp.json() or [])
            try:
                total_pages = max(1, int(resp.headers.get("X-Pages") or 1))
            except (TypeError, ValueError):
                total_pages = 1
            if page >= total_pages:
                break
            page += 1
        return orders

    def get_region_history(self, region_id, type_id):
        """获取星域内某物品的历史日线（均价/最高/最低/成交量）。"""
        return self._get(
            f"/v1/markets/{int(region_id)}/history/",
            params={"type_id": int(type_id)},
        ) or []

    def get_market_prices(self):
        """获取全局参考价列表（/v1/markets/prices/，每日更新）。

        返回 [{type_id, adjusted_price, average_price}, ...]。
        PLEX 这类没有星域挂单的物品只能从这里取到参考价。
        """
        return self._get("/v1/markets/prices/") or []

    # ------------------------------------------------------------ 汇总统计

    @staticmethod
    def summarize(entries):
        """对流水条目做简单的收支汇总统计。"""
        total_income = 0.0  # 收入（amount > 0）
        total_expense = 0.0  # 支出（amount < 0）
        tax_total = 0.0  # 税费合计
        types = {}
        for e in entries:
            amount = float(e.get("amount", 0.0) or 0.0)
            if amount > 0:
                total_income += amount
            else:
                total_expense += amount
            tax = float(e.get("tax", 0.0) or 0.0)
            tax_total += abs(tax)
            # 按描述分类（取 description 关键字）
            desc = (e.get("description") or "").strip()
            key = desc if desc else "(空)"
            types[key] = types.get(key, 0) + 1
        return {
            "count": len(entries),
            "total_income": total_income,
            "total_expense": total_expense,
            "tax_total": tax_total,
            "net": total_income + total_expense - tax_total,
            "top_descriptions": sorted(types.items(), key=lambda kv: kv[1], reverse=True)[:10],
        }
