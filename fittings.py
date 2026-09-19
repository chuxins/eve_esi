"""角色装配方案（fittings）查询与展示。

数据来源：ESI
    GET /v2/characters/{character_id}/fittings/   （需 esi-fittings.read_fittings.v1）
    返回 [{fitting_id, name, description, ship_type_id, items:[{type_id, flag, quantity}]}]

重要限制：ESI **只提供角色个人保存的装配**，军团共享装配没有任何接口能获取
（2026-09-20 实测：游戏内「我的装配」34 套 = 接口 34 套，「军团装配」474 套完全不在返回中）。
想把军团装配纳入查询，只能在游戏内「复制到我的装配」。

物品名解析全部走本地 item_types 索引（约 1.95 万条），不产生额外请求。

用法：
    python3 fittings.py chuxins1              # 列出该角色全部装配
    python3 fittings.py chuxins1 3            # 查看第 3 套详情
    python3 fittings.py chuxins1 狂暴          # 按舰船名查看（如狂暴级的所有装配）
"""

import argparse
import os
import sys

from esi_client import ESIClient, ESIError
from main import get_access_token, get_db, load_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LIST_LIMIT = 40        # 列表最多展示套数
GROUP_ITEM_LIMIT = 12  # 每组最多展示的物品种类数
NAME_LIMIT = 30        # 物品名截断长度

# (flag 前缀, 中文槽位名)，按展示顺序；Invalid 作为兜底
SLOT_GROUPS = (
    ("HiSlot", "🔫 高槽"),
    ("MedSlot", "🎯 中槽"),
    ("LoSlot", "🛡 低槽"),
    ("RigSlot", "📦 改装件"),
    ("SubSystemSlot", "🧩 子系统"),
    ("ServiceSlot", "🔧 服务槽"),
    ("DroneBay", "🛸 无人机舱"),
    ("FighterBay", "✈️ 铁骑舰载机舱"),
    ("Cargo", "🧳 货舱"),
)
FALLBACK_LABEL = "❓ 其它"

# EFT 导出顺序（与游戏内「导出装配」一致）：低槽 → 中槽 → 高槽 → 改装件 →
# 子系统 → 服务槽 → 无人机舱 → 铁骑舰载机舱 → 货舱；组间空一行，空组也占一个空行。
EFT_GROUP_ORDER = (
    ("LoSlot", "low"),
    ("MedSlot", "med"),
    ("HiSlot", "high"),
    ("RigSlot", "rig"),
    ("SubSystemSlot", "subsystem"),
    ("ServiceSlot", "service"),
    ("DroneBay", "drone"),
    ("FighterBay", "fighter"),
    ("Cargo", "cargo"),
)
EFT_STACKED_GROUPS = ("drone", "fighter", "cargo")  # 这些组用 name xN 写法


class FittingError(RuntimeError):
    """装配查询相关错误。"""


class FittingScopeError(FittingError):
    """token 缺少 esi-fittings.read_fittings.v1 权限。"""


# ---------------------------------------------------------------- 拉取

def fetch_fittings(db, config, character_id):
    """拉取角色装配列表。缺权限时抛 FittingScopeError。"""
    token = get_access_token(config, db, character_id)
    if not token:
        raise FittingError("无法获取有效 token，请重新授权（发送「添加账号」）")
    client = ESIClient(token, config["user_agent"])
    try:
        return client.get_character_fittings(character_id)
    except ESIError as exc:
        message = str(exc)
        if "401" in message or "required scope" in message:
            raise FittingScopeError(message) from exc
        raise


# ---------------------------------------------------------------- 槽位与名称

def slot_label(flag):
    """把 ESI 的 flag 映射为中文槽位名。"""
    for prefix, label in SLOT_GROUPS:
        if str(flag).startswith(prefix):
            return label
    return FALLBACK_LABEL


def collect_type_ids(fittings):
    """收集装配涉及的舰船与物品 type_id。"""
    ids = set()
    for fitting in fittings:
        if fitting.get("ship_type_id"):
            ids.add(int(fitting["ship_type_id"]))
        for item in fitting.get("items") or []:
            if item.get("type_id"):
                ids.add(int(item["type_id"]))
    return ids


def _name_of(names, type_id):
    name = names.get(int(type_id))
    if not name:
        return f"#{type_id}"
    return name if len(name) <= NAME_LIMIT else name[:NAME_LIMIT] + "…"


def group_items(fitting):
    """把装配件按槽位分组：{槽位名: {type_id: 数量}}，顺序遵循 SLOT_GROUPS。"""
    groups = {}
    for item in fitting.get("items") or []:
        label = slot_label(item.get("flag"))
        bucket = groups.setdefault(label, {})
        type_id = int(item["type_id"])
        bucket[type_id] = bucket.get(type_id, 0) + int(item.get("quantity") or 1)
    return groups


# ---------------------------------------------------------------- 选择

def resolve_fitting(fittings, selector, names=None):
    """按序号、**舰船名**或装配名挑选装配。

    优先级：序号 → 装配名精确匹配 → **舰船名（子串）** → 装配名子串（兜底）
    返回 (命中的装配或 None, 候选列表, 匹配方式)；匹配方式为
    "index" / "name" / "ship" / "loose" / None。
    候选多于 1 套时由调用方提示用户用序号选择。
    """
    selector = str(selector or "").strip()
    if not selector:
        return None, [], None
    if selector.isdigit():
        index = int(selector) - 1
        if 0 <= index < len(fittings):
            return fittings[index], [], "index"
        return None, [], None

    lowered = selector.lower()
    for fitting in fittings:  # 装配名精确匹配
        if str(fitting.get("name", "")).lower() == lowered:
            return fitting, [], "name"

    if names:
        # 关键词按舰船名匹配（如「狂暴」「台风级」）
        ship_hits = [
            f for f in fittings
            if lowered in str(names.get(int(f.get("ship_type_id") or 0), "")).lower()
        ]
        if len(ship_hits) == 1:
            return ship_hits[0], ship_hits, "ship"
        if ship_hits:
            return None, ship_hits, "ship"

    loose = [f for f in fittings if lowered in str(f.get("name", "")).lower()]
    if len(loose) == 1:
        return loose[0], loose, "loose"
    if loose:
        return None, loose, "loose"
    return None, [], None


def format_candidates(fittings, candidates, names, mode=None):
    """列出候选装配，使用**全局序号**（可直接用该序号查看详情）。"""
    title = f"🔍 匹配到 {len(candidates)} 套"
    if mode == "ship":
        title += "（按舰船名）"
    lines = [f"{title}，直接回复序号看详情：", "────────────────"]
    index_of = {id(f): i for i, f in enumerate(fittings)}
    for fitting in candidates[:12]:
        idx = index_of.get(id(fitting), 0) + 1
        ship = _name_of(names, fitting.get("ship_type_id")) if fitting.get("ship_type_id") else "?"
        name = str(fitting.get("name") or "(未命名)")
        if len(name) > 24:
            name = name[:24] + "…"
        lines.append(f"{idx}. {name} — {ship}")
    if len(candidates) > 12:
        lines.append(f"…另有 {len(candidates) - 12} 套")
    return "\n".join(lines)


# ---------------------------------------------------------------- 展示

def format_list(fittings, character_name, names):
    """装配清单文本。"""
    lines = [f"🛠 {character_name} 的装配（共 {len(fittings)} 套）", "────────────────"]
    for i, fitting in enumerate(fittings[:LIST_LIMIT], start=1):
        ship = _name_of(names, fitting.get("ship_type_id")) if fitting.get("ship_type_id") else "?"
        name = str(fitting.get("name") or "(未命名)")
        if len(name) > 24:
            name = name[:24] + "…"
        lines.append(f"{i}. {name} — {ship}（{len(fitting.get('items') or [])} 件）")
    if len(fittings) > LIST_LIMIT:
        lines.append(f"…另有 {len(fittings) - LIST_LIMIT} 套未列出，可用关键词查看")
    lines.append("────────────────")
    lines.append("直接回复序号即可查看详情（EFT 格式，可直接导入游戏）")
    lines.append(f"也可用「装配 {character_name} <序号、舰船名或关键词>」")
    return "\n".join(lines)


def format_detail(fitting, names, prices=None):
    """装配详情文本：按槽位分组 + 可选参考估价。"""
    ship = _name_of(names, fitting.get("ship_type_id")) if fitting.get("ship_type_id") else "?"
    lines = [
        f"🛠 {fitting.get('name') or '(未命名)'}",
        f"🚀 舰船：{ship}　🆔 装配ID {fitting.get('fitting_id')}",
    ]
    description = str(fitting.get("description") or "").strip()
    if description:
        lines.append(f"📝 {description}")
    lines.append("────────────────")

    groups = group_items(fitting)
    if not groups:
        lines.append("（该装配没有保存任何物品）")
    for _, label in SLOT_GROUPS:
        bucket = groups.get(label)
        if not bucket:
            continue
        parts = []
        for type_id, qty in sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0])):
            name = _name_of(names, type_id)
            parts.append(f"{name} ×{qty}" if qty > 1 else name)
        shown = parts[:GROUP_ITEM_LIMIT]
        text = " ｜ ".join(shown)
        if len(parts) > GROUP_ITEM_LIMIT:
            text += f" …等 {len(parts)} 种"
        lines.append(f"{label}({len(parts)} 种)：{text}")
    if groups.get(FALLBACK_LABEL):
        parts = [
            f"{_name_of(names, tid)} ×{qty}" if qty > 1 else _name_of(names, tid)
            for tid, qty in sorted(groups[FALLBACK_LABEL].items(), key=lambda kv: -kv[1])
        ]
        lines.append(f"{FALLBACK_LABEL}({len(parts)} 种)：{' ｜ '.join(parts[:GROUP_ITEM_LIMIT])}")

    if prices:
        total, priced, count = estimate_value(fitting, prices)
        if priced:
            lines.append("────────────────")
            lines.append(
                f"💰 参考估价：{total:,.2f} ISK"
                f"（按 ESI 全局均价，{priced}/{count} 种物品已定价）"
            )
    return "\n".join(lines)


def format_eft(fitting, names, names_en=None, language="zh"):
    """按 EFT 格式输出整套装配，可直接粘贴进游戏「导入装配」或分享给他人。

    输出即为纯 EFT 文本（无额外说明行），便于整段复制：
        [舰船名, 装配名]
        低槽装备（每件一行）
        （空行）
        中槽装备
        …
        无人机 xN
        货舱物品 xN

    language="en" 时使用英文物品名（部分客户端只能识别英文名）。
    """
    def item_name(type_id):
        type_id = int(type_id)
        if language == "en":
            return (names_en or {}).get(type_id) or names.get(type_id) or f"#{type_id}"
        return names.get(type_id) or (names_en or {}).get(type_id) or f"#{type_id}"

    def slot_index(flag):
        digits = "".join(ch for ch in str(flag) if ch.isdigit())
        return int(digits) if digits else 0

    grouped = {key: [] for _, key in EFT_GROUP_ORDER}
    for item in fitting.get("items") or []:
        flag = str(item.get("flag") or "")
        for prefix, key in EFT_GROUP_ORDER:
            if flag.startswith(prefix):
                grouped[key].append((slot_index(flag), item))
                break

    ship = _name_of(names, fitting.get("ship_type_id")) if fitting.get("ship_type_id") else "?"
    if language == "en" and names_en and fitting.get("ship_type_id"):
        ship = names_en.get(int(fitting["ship_type_id"])) or ship

    lines = [f"[{ship}, {fitting.get('name') or 'Unnamed'}]\n"]
    for _, key in EFT_GROUP_ORDER:
        entries = grouped[key]
        if key in EFT_STACKED_GROUPS:
            for _, item in sorted(entries, key=lambda e: item_name(e[1]["type_id"])):
                quantity = max(1, int(item.get("quantity") or 1))
                lines.append(f"{item_name(item['type_id'])} x{quantity}\n")
        else:
            for _, item in sorted(entries, key=lambda e: e[0]):
                for _ in range(max(1, int(item.get("quantity") or 1))):
                    lines.append(f"{item_name(item['type_id'])}\n")
        lines.append("\n")  # 组间空行（空组也占一个）

    text = "".join(lines)
    return text.rstrip("\n")


def estimate_value(fitting, prices):
    """按全局参考均价估算整套装配价值，返回 (总额, 已定价种类数, 总种类数)。"""
    if not prices:
        return 0.0, 0, 0
    total = 0.0
    priced = 0
    items = fitting.get("items") or []
    for item in items:
        unit = prices.get(int(item["type_id"]))
        if unit:
            total += float(unit) * int(item.get("quantity") or 1)
            priced += 1
    return total, priced, len(items)


# ---------------------------------------------------------------- CLI

def main():
    parser = argparse.ArgumentParser(description="查询角色已保存的装配方案")
    parser.add_argument("character", help="角色名或角色 ID")
    parser.add_argument("selector", nargs="?", help="序号或名称关键词（省略则列出全部）")
    parser.add_argument("--no-price", action="store_true", help="不显示参考估价")
    args = parser.parse_args()

    config = load_config()
    db = get_db(config)
    matched = [
        c for c in db.list_characters()
        if str(c["character_id"]) == args.character or c["character_name"] == args.character
    ]
    if not matched:
        print(f"未找到角色「{args.character}」")
        sys.exit(1)
    character = matched[0]

    try:
        fittings = fetch_fittings(db, config, character["character_id"])
    except FittingScopeError:
        print("该角色尚未授予装配权限（esi-fittings.read_fittings.v1）。\n"
              "请在 EVE 账号设置里撤销本应用授权后，重新发送「添加账号」授权。")
        sys.exit(2)

    if not fittings:
        print(f"{character['character_name']} 没有保存过装配方案。")
        return

    names = db.get_item_type_names(collect_type_ids(fittings))
    if not args.selector:
        print(format_list(fittings, character["character_name"], names))
        return

    fitting, candidates, mode = resolve_fitting(fittings, args.selector, names)
    if fitting is None:
        if candidates:
            print(format_candidates(fittings, candidates, names, mode))
        else:
            print(f"未找到匹配「{args.selector}」的装配（共 {len(fittings)} 套）")
        sys.exit(1)

    prices = {}
    if not args.no_price:
        try:
            from market_price import get_price_table
            prices = get_price_table()
        except Exception as exc:
            print(f"（参考价不可用：{exc}）", file=sys.stderr)
    print(format_detail(fitting, names, prices))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        sys.exit(1)
