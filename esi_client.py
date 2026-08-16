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
    def __init__(self, access_token, user_agent):
        self.access_token = access_token
        self.user_agent = user_agent

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }

    def _get(self, path, params=None, allow_404=False):
        url = f"{ESI_BASE}{path}"
        resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        if resp.status_code == 404 and allow_404:
            return None
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

    def sync_wallet_journal(self, character_id, max_pages=20):
        """全量增量同步钱包流水。

        从最新一页开始持续翻页直到返回空（或达到 max_pages），
        返回所有条目（含历史），由调用方按 ref_id 去重入库。
        相比只取最新 N 条，能确保不漏任何历史记录。

        max_pages 上限防止异常数据导致无限翻页（每页约 1000 条）。
        """
        all_entries = []
        page = 1
        while page <= max_pages:
            batch = self._get(
                f"/v4/characters/{character_id}/wallet/journal/",
                params={"page": page},
                allow_404=True,  # 页不存在(404)表示已无更多数据
            )
            if not batch:
                break
            all_entries.extend(batch)
            page += 1
        return all_entries

    # ------------------------------------------------------------ 市场交易

    def get_wallet_transactions(self, character_id):
        """获取角色的全部市场交易详情（wallet transactions）。

        返回条目列表，每条包含：transaction_id、journal_ref_id、type_id、
        location_id、client_id、date、is_buy、is_personal、quantity、unit_price。
        """
        return self._get(f"/v3/characters/{character_id}/wallet/transactions/")

    def get_universe_type(self, type_id):
        """获取 EVE 物品类型信息（用于 type_id -> 名称翻译）。"""
        return self._get(f"/v3/universe/types/{int(type_id)}/")

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
