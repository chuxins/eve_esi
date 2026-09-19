"""构建本地物品名称索引（供「查价」模糊匹配使用）。

数据来源：EVE 官方 SDE
    https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip
其中 ``types.jsonl`` 的 ``name`` 字段内含各语言名称，例如：
    {"_key": 34, "name": {"en": "Tritanium", "zh": "三钛 合金", ...}, "published": true}

只索引「已发布且属于某个市场分类」的物品（约 1.9 万个），写入 item_types 表：
- name      显示名（中文优先）
- name_en   英文名
- name_norm 规范化搜索键 ``|中文|英文|``，供中英文模糊匹配

用法：
    python3 build_item_index.py                 # 复用本地 sde.zip，没有则下载（约 95MB）
    python3 build_item_index.py --force-download
    python3 build_item_index.py --zip /path/to/sde.zip
    python3 build_item_index.py --stats         # 只看索引统计
"""

import argparse
import json
import os
import re
import sys
import time
import zipfile

import requests

from main import get_db, load_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SDE_URL = ("https://developers.eveonline.com/static-data/"
           "eve-online-static-data-latest-jsonl.zip")
SDE_ZIP_PATH = os.path.join(BASE_DIR, "sde.zip")
BATCH_SIZE = 2000
SAMPLE_KEYWORDS = ("磁轨炮", "三钛", "tritan", "伊甸币")

# SDE 中文名里存在「三钛 合金」这类汉字间多余空格（ESI 返回的写法没有空格），
# 这里仅删除两个汉字之间的空白，不影响 "125mm 磁轨炮 I" 这类混排名称。
_CJK_SPACE_RE = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")


def clean_zh_name(name):
    """清理中文名中的多余空白（全角空格、汉字间空格）。"""
    return _CJK_SPACE_RE.sub("", str(name or "").replace("\u3000", " ")).strip()


def download_sde(path, user_agent):
    """下载 SDE zip（约 95MB）到 path。"""
    print(f"下载 SDE（约 95MB）：{SDE_URL}")
    tmp = path + ".part"
    with requests.get(SDE_URL, headers={"User-Agent": user_agent},
                      stream=True, timeout=300, allow_redirects=True) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    os.replace(tmp, path)
    print(f"已保存 {path}（{os.path.getsize(path) / 1048576:.1f} MB）")


def iter_types(zip_path):
    """遍历 SDE types.jsonl，产出 (type_id, 中文名(缺失用英文), 英文名)。"""
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open("types.jsonl") as f:
            for raw in f:
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue
                # 只看已发布且在市场分类下的物品（其余没有行情）
                if not item.get("published") or not item.get("marketGroupID"):
                    continue
                type_id = item.get("_key")
                names = item.get("name") or {}
                zh = clean_zh_name(names.get("zh"))
                en = str(names.get("en") or "").strip()
                if not type_id or not (zh or en):
                    continue
                yield int(type_id), zh or en, en


def build_index(db, zip_path, reset=False):
    """把 SDE 物品名批量写入 item_types，返回写入条数。

    reset=True 时先清空索引表（用于修正历史写入的名称），
    随后由 SDE 重建；非市场物品不受影响（它们不在 SDE 索引范围内）。
    """
    if reset:
        with db._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM item_types")
        print("  已清空原索引，准备重建")
    total = 0
    batch = []
    for type_id, name, name_en in iter_types(zip_path):
        batch.append({"type_id": type_id, "name": name, "name_en": name_en})
        if len(batch) >= BATCH_SIZE:
            db.upsert_item_types(batch)
            total += len(batch)
            batch.clear()
            print(f"  已写入 {total} 条…", end="\r", flush=True)
    if batch:
        db.upsert_item_types(batch)
        total += len(batch)
    print(f"  已写入 {total} 条      ")
    return total


def show_sample_searches(db):
    """打印几个关键词的模糊匹配结果，便于快速验证索引质量。"""
    print("模糊匹配抽样：")
    for keyword in SAMPLE_KEYWORDS:
        hits = db.search_item_types(keyword, limit=3)
        text = "、".join(h["name"] for h in hits) or "（无匹配）"
        print(f"  「{keyword}」→ {text}")


def main():
    parser = argparse.ArgumentParser(description="构建本地物品名称索引")
    parser.add_argument("--zip", default=None, help="本地 SDE zip 路径")
    parser.add_argument("--force-download", action="store_true", help="强制重新下载 SDE")
    parser.add_argument("--stats", action="store_true", help="只显示索引统计")
    parser.add_argument("--reset", action="store_true",
                        help="重建前清空索引表（修正历史写入的名称）")
    args = parser.parse_args()

    config = load_config()
    db = get_db(config)

    if args.stats:
        total, with_en = db.item_types_count()
        print(f"索引物品数：{total}（含英文名 {with_en}）")
        show_sample_searches(db)
        return

    zip_path = args.zip or SDE_ZIP_PATH
    if args.force_download or not os.path.exists(zip_path):
        download_sde(zip_path, config["user_agent"])
    else:
        print(f"复用本地 SDE：{zip_path}")

    started = time.time()
    count = build_index(db, zip_path, reset=args.reset)
    total, with_en = db.item_types_count()
    print(f"✅ 索引已更新：本次写入 {count} 个物品，耗时 {time.time() - started:.1f}s")
    print(f"   当前索引共 {total} 个物品（含英文名 {with_en}）")
    show_sample_searches(db)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        sys.exit(1)
