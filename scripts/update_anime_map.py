#!/usr/bin/env python3
"""预生成 MAL 动画榜单 → TMDB 的映射表。

背景：模块里每次加载 MAL 榜单都要「拉榜 → 逐条去 TMDB 搜索」，
一页 25 条会产生 60+ 次 TMDB 请求。这里把映射提前算好存进
data/anime-mal-map.json，模块直接读文件，加载从 60+ 次请求降到 1 次。

数据源用 MAL 官方榜单页（https://myanimelist.net/topanime.php）：
- 与 Jikan 同源（Jikan 本来就是 MAL 的非官方 API），口径完全一致
- 更关键：api.jikan.moe 目前只有 IPv6 记录，GitHub runner 没有 IPv6 出口，
  必然连接超时；MAL 官网页面则稳定可达（实测 200 / 0.5s）

输出结构（模块侧只做本地拼装，不再发搜索请求）：
{
  "updated": "2026-10-03 21:30:00",
  "source": "myanimelist.net/topanime.php",
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
from bs4 import BeautifulSoup

TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
OUTPUT_FILE = os.path.join("data", "anime-mal-map.json")

MAL_TOP_URL = "https://myanimelist.net/topanime.php"
MAL_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
MAL_HEADERS = {
    "User-Agent": MAL_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TMDB_BASE = "https://api.themoviedb.org/3"
ANIMATION_GENRE_ID = 16
MAL_PAGE_SIZE = 50          # MAL 服务端每页 50 条，limit 参数是偏移量
MAL_PAGES = 2               # 抓 2 页 = 100 条
TARGET_PER_LIST = 75        # 去重后每榜保留的上限
TMDB_CONCURRENCY = 8

LISTS = [("all", None), ("airing", "airing")]


def tmdb_params(extra):
    p = dict(extra)
    if not (TMDB_API_KEY or "").startswith("eyJ"):
        p["api_key"] = TMDB_API_KEY
    return p


def tmdb_headers():
    h = {"accept": "application/json"}
    if (TMDB_API_KEY or "").startswith("eyJ"):
        h["Authorization"] = f"Bearer {TMDB_API_KEY}"
    return h


def clean_query(text):
    """与模块 searchTmdbAnimeStrict 的清洗规则保持一致。"""
    if not text or not isinstance(text, str):
        return ""
    q = re.sub(r"第[一二三四五六七八九十\d]+[季章]", "", text)
    q = re.sub(r"(?i)Season \d+", "", q)
    return re.sub(r"\s+", " ", q).strip()


def parse_mal_page(html):
    """解析 MAL 榜单页（桌面版表格结构）。"""
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    for tr in soup.select("tr.ranking-list"):
        anchor = tr.select_one("td.title h3 a") or tr.select_one("td.title a")
        if not anchor:
            continue
        href = anchor.get("href") or ""
        mal_id = re.search(r"/anime/(\d+)/", href)
        rank = tr.select_one("td.rank span")
        score = tr.select_one("td.score")
        info = tr.select_one("td.title .information")
        entries.append({
            "malId": int(mal_id.group(1)) if mal_id else None,
            "title": anchor.get_text(strip=True),
            "rank": rank.get_text(strip=True) if rank else "",
            "score": score.get_text(strip=True) if score else "",
            "info": info.get_text(" ", strip=True) if info else "",
        })
    return entries


async def fetch_mal_page(session, list_type, offset):
    params = {"limit": offset}
    if list_type == "airing":
        params["type"] = "airing"
    for attempt in range(3):
        try:
            async with session.get(MAL_TOP_URL, params=params, headers=MAL_HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    return parse_mal_page(await resp.text())
                print(f"  ⚠️ MAL HTTP {resp.status}（{params}），第 {attempt + 1}/3 次")
        except Exception as exc:
            print(f"  ⚠️ MAL 异常（{params}）: {type(exc).__name__} {exc}")
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

    for kind in ("tv", "movie"):
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
    title = entry.get("title") or ""
    hit = await search_once(session, title, sem, cache)
    if hit:
        return hit
    # 罗马音标题偶尔带副标题，去掉冒号后半段再试一次
    if ":" in title:
        return await search_once(session, title.split(":", 1)[0], sem, cache)
    return None


def to_record(mal_entry, tmdb_hit):
    kind = tmdb_hit.get("_kind", "tv")
    return {
        "malId": mal_entry.get("malId"),
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


async def build_list(session, list_type, filter_value, sem, cache):
    entries = []
    for page in range(MAL_PAGES):
        offset = page * MAL_PAGE_SIZE
        data = await fetch_mal_page(session, filter_value, offset)
        if data is None:
            print(f"❌ [{list_type}] MAL 第 {page + 1} 页抓取失败")
            return None
        if not data:
            break
        entries.extend(data)
        await asyncio.sleep(0.5)

    print(f"📥 [{list_type}] MAL 榜单共 {len(entries)} 条，开始映射 TMDB…")
    records, seen = [], set()
    for i in range(0, len(entries), 10):
        chunk = entries[i:i + 10]
        hits = await asyncio.gather(*[map_entry(session, e, sem, cache) for e in chunk])
        for mal_entry, hit in zip(chunk, hits):
            if not hit:
                continue
            rec = to_record(mal_entry, hit)
            if not rec["tmdbId"] or rec["tmdbId"] in seen:
                continue      # 同作品多条目（各季/剧场版）会塌陷到同一 TMDB id，去重
            seen.add(rec["tmdbId"])
            records.append(rec)
            if len(records) >= TARGET_PER_LIST:
                break
        if len(records) >= TARGET_PER_LIST:
            break
    print(f"✅ [{list_type}] 映射完成: {len(entries)} 条 → {len(records)} 条（已按 TMDB id 去重）")
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
        "source": "myanimelist.net/topanime.php",
        "lists": lists,
    }
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\n🎉 已写入 {OUTPUT_FILE}: all={len(lists['all'])} / airing={len(lists['airing'])}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
