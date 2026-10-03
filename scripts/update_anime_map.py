#!/usr/bin/env python3
"""预生成 MAL 动画榜单 → TMDB 的映射表。

背景：模块里每次加载 MAL 榜单都要「Jikan 拉榜 → 逐条去 TMDB 搜索」，
一页 25 条会产生 60+ 次 TMDB 请求，且第一跳 api.jikan.moe 经常很慢。
这里把映射提前算好存进 data/anime-mal-map.json，模块直接读文件，
加载从「60+ 次请求」降到「1 次请求」。

输出结构（模块侧只做本地拼装，不再发搜索请求）：
{
  "updated": "2026-10-03 21:30:00",
  "lists": {
    "all":    [ {malId, tmdbId, mediaType, title, date, poster, backdrop, rating, genreIds, overview}, ... ],
    "airing": [ ... ]
  }
}
"""
import asyncio
import datetime
import json
import os
import re
import sys

import aiohttp

TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
OUTPUT_FILE = os.path.join("data", "anime-mal-map.json")

JIKAN_BASE = "https://api.jikan.moe/v4/top/anime"
TMDB_BASE = "https://api.themoviedb.org/3"
ANIMATION_GENRE_ID = 16
PAGES_PER_LIST = 3          # 每个榜单抓 3 页（3 × 25 = 75 条）
TMDB_CONCURRENCY = 8        # TMDB 并发
JIKAN_INTERVAL = 1.2        # Jikan 限速 3 次/秒，留足余量

LISTS = [("all", None), ("airing", "airing")]


def tmdb_headers():
    h = {"accept": "application/json"}
    return h


def tmdb_params(extra):
    p = dict(extra)
    if TMDB_API_KEY and TMDB_API_KEY.startswith("eyJ"):
        pass
    else:
        p["api_key"] = TMDB_API_KEY
    return p


def clean_query(text):
    """与模块 searchTmdbAnimeStrict 的清洗规则保持一致。"""
    if not text or not isinstance(text, str):
        return ""
    q = re.sub(r"第[一二三四五六七八九十\d]+[季章]", "", text)
    q = re.sub(r"(?i)Season \d+", "", q)
    return q.strip()


async def jikan_get(session, params):
    """抓 Jikan 榜单，遇 429/5xx 退避重试。"""
    for attempt in range(4):
        try:
            async with session.get(JIKAN_BASE, params=params,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    return (await resp.json()).get("data", [])
                print(f"  ⚠️ Jikan HTTP {resp.status}（{params}），第 {attempt + 1}/4 次")
        except Exception as exc:
            print(f"  ⚠️ Jikan 异常（{params}）: {exc}")
        await asyncio.sleep(2 + attempt * 2)
    return None


async def tmdb_get(session, path, params, sem):
    async with sem:
        for attempt in range(3):
            try:
                async with session.get(f"{TMDB_BASE}{path}", params=params,
                                       headers=tmdb_headers(),
                                       timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status in (429, 500, 502, 503, 504):
                        await asyncio.sleep(1 + attempt)
                        continue
                    return None
            except Exception:
                await asyncio.sleep(1 + attempt)
    return None


async def search_once(session, query, sem, cache):
    """一次查询：先剧集后电影，只接受动画类型的结果（与模块逻辑一致）。"""
    q = clean_query(query)
    if not q:
        return None
    if q in cache:
        return cache[q]

    for kind, date_field in (("tv", "first_air_date"), ("movie", "release_date")):
        data = await tmdb_get(session, f"/search/{kind}",
                              tmdb_params({"query": q, "language": "zh-CN", "include_adult": "false"}), sem)
        results = (data or {}).get("results", []) or []
        anime = [r for r in results if ANIMATION_GENRE_ID in (r.get("genre_ids") or [])]
        if anime:
            hit = next((r for r in anime if r.get("poster_path")), anime[0])
            hit["_kind"] = kind
            cache[q] = hit
            return hit

    cache[q] = None
    return None


async def map_entry(session, entry, sem, cache):
    """把一条 MAL 条目映射到 TMDB 条目。"""
    queries = []
    for key in ("title_japanese", "title"):
        v = entry.get(key)
        if v and v not in queries:
            queries.append(v)
    en = entry.get("title_english")
    if en and en not in queries:
        queries.append(en)

    for query in queries:
        hit = await search_once(session, query, sem, cache)
        if hit:
            return hit
    return None


def to_record(mal_entry, tmdb_hit):
    kind = tmdb_hit.get("_kind", "tv")
    return {
        "malId": mal_entry.get("mal_id"),
        "tmdbId": tmdb_hit.get("id"),
        "mediaType": kind,
        "title": tmdb_hit.get("name") or tmdb_hit.get("title") or mal_entry.get("title"),
        "date": tmdb_hit.get("first_air_date") or tmdb_hit.get("release_date") or "",
        "poster": tmdb_hit.get("poster_path") or "",
        "backdrop": tmdb_hit.get("backdrop_path") or "",
        "rating": round(float(tmdb_hit.get("vote_average") or 0), 1),
        "genreIds": tmdb_hit.get("genre_ids") or [],
        "overview": tmdb_hit.get("overview") or "",
    }


async def build_list(session, key, filter_value, sem, cache):
    entries = []
    for page in range(1, PAGES_PER_LIST + 1):
        params = {"page": page}
        if filter_value:
            params["filter"] = filter_value
        data = await jikan_get(session, params)
        if data is None:
            print(f"❌ [{key}] Jikan 第 {page} 页抓取失败")
            return None
        if not data:
            break
        entries.extend(data)
        await asyncio.sleep(JIKAN_INTERVAL)

    print(f"📥 [{key}] MAL 共 {len(entries)} 条，开始映射 TMDB…")
    records = []
    seen = set()
    for i in range(0, len(entries), 10):
        chunk = entries[i:i + 10]
        hits = await asyncio.gather(*[map_entry(session, e, sem, cache) for e in chunk])
        for mal_entry, hit in zip(chunk, hits):
            if not hit:
                continue
            rec = to_record(mal_entry, hit)
            if not rec["tmdbId"] or rec["tmdbId"] in seen:
                continue          # 同作品多条目（各季/剧场版）会塌陷到同一 TMDB id，去重
            seen.add(rec["tmdbId"])
            records.append(rec)
    print(f"✅ [{key}] 映射完成: {len(entries)} 条 → {len(records)} 条（已按 TMDB id 去重）")
    return records


async def main():
    if not TMDB_API_KEY:
        print("❌ 未检测到 TMDB_API_KEY")
        return 1

    tz_bj = datetime.timezone(datetime.timedelta(hours=8))
    lists = {}
    sem = asyncio.Semaphore(TMDB_CONCURRENCY)
    cache = {}

    async with aiohttp.ClientSession() as session:
        for key, filter_value in LISTS:
            records = await build_list(session, key, filter_value, sem, cache)
            if records is None:
                print("❌ 榜单抓取失败，保留旧数据，不覆盖文件")
                return 1
            lists[key] = records

    if not any(lists.values()):
        print("❌ 所有榜单均为空，保留旧数据")
        return 1

    payload = {
        "updated": datetime.datetime.now(tz_bj).strftime("%Y-%m-%d %H:%M:%S"),
        "lists": lists,
    }
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\n🎉 已写入 {OUTPUT_FILE}: all={len(lists['all'])} / airing={len(lists['airing'])}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
