"""市场交易同步辅助函数。

负责：
- 从 ESI 拉取角色市场交易详情并写入 DB
- 按 type_id 拉取 EVE 物品名称并缓存到 item_types 表
"""


def ensure_item_types(db, client, type_ids):
    """确保 item_types 表中有这些 type_id 的名称，缺失时从 ESI 获取。"""
    ids = list({int(x) for x in type_ids if x})
    if not ids:
        return
    missing = db.get_missing_item_type_ids(ids)
    for type_id in missing:
        try:
            info = client.get_universe_type(type_id)
            name = (info or {}).get("name") or ""
            if name:
                db.upsert_item_types([{"type_id": type_id, "name": name}])
        except Exception as exc:
            # 单个物品名失败不影响整体同步
            print(f"  获取物品名失败 type_id={type_id}: {exc}")


def sync_market_transactions(db, client, character_id):
    """拉取指定角色的全部市场交易详情并入库，同时补充物品名称。

    返回 ESI 原始交易列表。
    """
    entries = client.get_wallet_transactions(character_id)
    if entries:
        db.upsert_wallet_transactions(character_id, entries)
        type_ids = {int(e["type_id"]) for e in entries if e.get("type_id")}
        ensure_item_types(db, client, type_ids)
    return entries
