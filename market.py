"""市场交易同步辅助函数。

负责：
- 从 ESI 拉取角色市场交易详情并写入 DB
- 按 type_id 拉取 EVE 物品名称并缓存到 item_types 表
"""


def ensure_item_types(db, client, type_ids):
    """确保 item_types 表中有这些 type_id 的名称，缺失时从 ESI 获取。

    用 POST /universe/names/ 批量解析（单次最多 1000 个 id，1 次请求替代逐个
    GET /universe/types/{id}/ 的 N 次串行请求）。注意该端点只返回英文名，
    适用于 SDE 索引（约 1.95 万条已发布市场物品）之外的冷门/新物品兜底。
    """
    ids = list({int(x) for x in type_ids if x})
    if not ids:
        return
    missing = db.get_missing_item_type_ids(ids)
    if not missing:
        return
    try:
        data = client.resolve_ids(missing)
    except Exception as exc:
        # 批量失败不阻塞整体同步（保持原逐个兜底的兼容性）
        print(f"  批量获取物品名失败: {exc}")
        return
    entries = [
        {"type_id": int(e["id"]), "name": str(e.get("name") or ""),
         "name_en": str(e.get("name") or "")}
        for e in (data or []) if e.get("id")
    ]
    if entries:
        db.upsert_item_types(entries)


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
