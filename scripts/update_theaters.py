import asyncio
import aiohttp
from bs4 import BeautifulSoup
import json
import os
import re
import unicodedata
import datetime

# --- 配置区 ---
TMDB_API_KEY = os.environ.get('TMDB_API_KEY')
DATA_DIR = "data"
OUTPUT_FILE = os.path.join(DATA_DIR, "theater-data.json")

# TMDB 类型映射表
GENRE_MAP = {
    28: "动作", 12: "冒险", 16: "动画", 35: "喜剧", 80: "犯罪", 99: "纪录片", 18: "剧情", 
    10751: "家庭", 14: "奇幻", 36: "历史", 27: "恐怖", 10402: "音乐", 9648: "悬疑", 
    10749: "爱情", 878: "科幻", 10770: "电视电影", 53: "惊悚", 10752: "战争", 37: "西部", 
    10759: "动作冒险", 10762: "儿童", 10763: "新闻", 10764: "真人秀", 10765: "科幻奇幻", 
    10766: "肥皂剧", 10767: "脱口秀", 10768: "战争政治"
}

# 你的终极片单宇宙！
THEATERS = [
    { "name": "迷雾剧场", "id": "164880152" },
    { "name": "白夜剧场", "id": "164880158" },
    { "name": "X剧场", "id": "164880165" },
    { "name": "横屏短剧", "id": "152299516" },
    { "name": "生花剧场", "id": "164880852" },
    { "name": "暗流剧场", "id": "164879624" },
    { "name": "大家剧场", "id": "160644809" },
    { "name": "小逗剧场", "id": "146055365" },
    { "name": "十分剧场", "id": "147708618" },
    { "name": "板凳单元", "id": "163392459" },
    { "name": "萤火单元", "id": "164881201" },
    { "name": "正午阳光", "id": "164881266" },
    { "name": "恋恋剧场", "id": "164880465" },
    { "name": "悬疑剧场", "id": "128400108" },
    { "name": "微尘剧场", "id": "161658331" }
]

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_BIG = {"万": 10000, "亿": 100000000}
_CN_NUM_RE = re.compile("[零〇一二三四五六七八九十百千万亿两]+")


def _cn_numeral_to_int(seg):
    """把中文数字串解析成整数，无法解析时返回 None。

    纯数字串（如「一九四二」）按逐位拼接处理——这是年号/编号的常见写法；
    含十百千万亿时按位值解析（如「十八」= 18、「二十」= 20）。
    """
    if all(ch in _CN_DIGITS for ch in seg):
        return int("".join(str(_CN_DIGITS[ch]) for ch in seg))

    total = section = number = 0
    for ch in seg:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            section += (number or 1) * _CN_UNITS[ch]
            number = 0
        elif ch in _CN_BIG:
            section = (section + number) * _CN_BIG[ch]
            total += section
            section = number = 0
        else:
            return None
    return total + section + number


def convert_cn_numerals(text):
    """把中文数字转成阿拉伯数字，仅在写法明确时转换。

    规则（保守，避免误伤「一生一世」「三国」「万万没想到」这类词）：
      - 连续两个及以上的数字串（如「十八」「二十一」「一九四二」）才转换
      - 单字数字仅在「第X」或位于标题末尾时转换（如「第2季」「欢乐颂2」）
      - 解析结果为 0 或无法解析时不转换

    两侧使用同一套转换，因此转换本身保持一致，不会引入错配。
    """
    def repl(m):
        seg = m.group(0)
        start, end = m.start(), m.end()
        n = _cn_numeral_to_int(seg)
        if not n:                      # None 或 0，保持原样
            return seg
        if len(seg) == 1:
            prev = text[start - 1] if start > 0 else ""
            if prev != "第" and end != len(text):
                return seg
        return str(n)

    return _CN_NUM_RE.sub(repl, text)


def normalize_title(text):
    """标题比对前的归一化：只消除「书写形式」差异。

    处理范围：全角/半角（NFKC）、标点与空白（Unicode 分类 P*/Z*）、
    中文数字与阿拉伯数字的写法差异。
    刻意不做语序调整或字符替换，因此不会把不同剧集（例如
    「遮云」与「云遮月」）误判为同一部。
    """
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(
        ch for ch in t
        if not (unicodedata.category(ch).startswith("P") or unicodedata.category(ch).startswith("Z"))
    )
    return convert_cn_numerals(t).lower()


def clean_douban_title(raw_title):
    """去除标题中可能的括号、年份后缀，以及季数 (第X季/Season X)"""
    # 1. 先去除结尾的年份，例如 (2022)
    match = re.match(r'^(.*?)(?:\((\d{4})\))?$', raw_title)
    if match:
        title = match.group(1).strip()
    else:
        title = raw_title.strip()
        
    # 2. 正则剔除 "第一季"、"第1季"、"Season 1"、"season1" 等字眼 (忽略大小写)
    title = re.sub(r'第[一二三四五六七八九十百\d]+季', '', title)
    title = re.sub(r'(?i)Season\s*\d+', '', title)
    
    # 3. 清理剔除后可能残留的多余空格 (例如 "巴瑞   Barry " 变成 "巴瑞 Barry")
    title = re.sub(r'\s+', ' ', title).strip()
    
    return title

async def fetch_doulist_pages(session, theater):
    """翻页抓取豆瓣片单里的所有剧集"""
    print(f"🎬 开始获取 [{theater['name']}] 数据...")
    all_items = []
    start = 0
    page_size = 25
    page_count = 0
    
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Referer": "https://m.douban.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9"
    }

    while True:
        page_count += 1
        url = f"https://m.douban.com/doulist/{theater['id']}/?start={start}"
        try:
            html = None
            for attempt in range(3):
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        body = await resp.text()
                        if resp.status == 200 and "doulist-items" in body:
                            html = body
                            break
                        print(f"[{theater['name']}] 豆瓣 HTTP {resp.status}，第 {attempt + 1}/3 次尝试")
                except Exception as e:
                    print(f"[{theater['name']}] 请求异常，第 {attempt + 1}/3 次尝试: {e}")
                await asyncio.sleep(2 + attempt * 2)
            if html is None:
                print(f"[{theater['name']}] 抓取失败，跳过本页")
                break
            soup = BeautifulSoup(html, 'html.parser')
            items = soup.select('ul.doulist-items > li, .doulist-items li')
            
            if not items: break
            
            for item in items:
                title_elem = item.select_one('.info .title')
                meta_elem = item.select_one('.info .meta')
                if title_elem:
                    raw_title = title_elem.text.strip()
                    clean_title = clean_douban_title(raw_title)
                    year = None
                    if meta_elem:
                        meta_text = meta_elem.text.strip()
                        year_match = re.search(r'(\d{4})(?=-\d{2}-\d{2})', meta_text)
                        if year_match:
                            year = year_match.group(1)
                    all_items.append({"title": clean_title, "year": year})
            if len(items) < page_size:
                break
            start += page_size
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"获取 {theater['name']} 第 {page_count} 页出错: {e}")
            break
            
    return {"items": all_items, "page_count": page_count}

async def search_tmdb(session, item, cache):
    """在 TMDB 中进行严格匹配"""
    title = item['title']
    year = item['year']
    cache_key = f"{title}_{year}"
    
    if cache_key in cache: return cache[cache_key]

    url = "https://api.themoviedb.org/3/search/tv"
    headers = {"accept": "application/json"}
    params = {"query": title, "language": "zh-CN"}
    
    if TMDB_API_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {TMDB_API_KEY}"
    else:
        params["api_key"] = TMDB_API_KEY

    if year: params["first_air_date_year"] = year

    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                results = data.get("results", [])
                if not results:
                    print(f"    ⚠️ [未匹配] 「{title}」（{year or '无年份'}）: TMDB 搜索无结果")
                
                # 获取当天的北京时间，用于拦截未开播的剧
                tz_bj = datetime.timezone(datetime.timedelta(hours=8))
                today_str = datetime.datetime.now(tz_bj).strftime("%Y-%m-%d")
                
                for res in results:
                    norm_query = normalize_title(title)
                    norm_tmdb = normalize_title(res.get("name"))
                    
                    # 匹配规则：完全相同直接命中；否则仅当较短一方不少于 3 个字时
                    # 才允许包含匹配，避免「深渊」误配「深渊无间」这类短标题误伤。
                    if not norm_query or not norm_tmdb:
                        is_title_match = False
                    elif norm_query == norm_tmdb:
                        is_title_match = True
                    else:
                        shorter = min(len(norm_query), len(norm_tmdb))
                        is_title_match = shorter >= 3 and (
                            norm_query in norm_tmdb or norm_tmdb in norm_query)
                    is_year_match = True
                    first_air = res.get("first_air_date")
                    
                    if year and first_air:
                        is_year_match = first_air.startswith(year)
                        
                    if not is_title_match:
                        print(f"    ⏭ [跳过] 「{title}」: 标题不匹配（TMDB=「{res.get('name')}」｜归一化: {norm_query} vs {norm_tmdb}）")
                    elif not is_year_match:
                        print(f"    ⏭ [跳过] 「{title}」: 年份不匹配（豆列={year}，TMDB首播={first_air}）")

                    if is_title_match and is_year_match:
                        # 🔴 核心拦截逻辑 1：检查是否缺失ID和海报
                        tmdb_id = res.get("id")
                        poster_path = res.get("poster_path")
                        backdrop_path = res.get("backdrop_path")
                        
                        if not tmdb_id or not poster_path:
                            print(f"    ⏭ [跳过] 「{title}」: TMDB 缺 id 或海报")
                            continue

                        # 剧照缺失时用海报兜底，避免刚开播的新剧被整条丢弃。
                        # 注意：TMDB 搜索接口的索引会滞后，新剧常常搜不到剧照，
                        # 下面请求详情接口后会再补正一次。
                        used_poster_fallback = False
                        if not backdrop_path:
                            print(f"    ℹ️ [兜底] 「{title}」: 搜索接口暂缺剧照，先用海报")
                            backdrop_path = poster_path
                            used_poster_fallback = True
                            
                        # 🔴 核心拦截逻辑 2：检查是否未开播
                        if not first_air:
                            print(f"    ⏭ [跳过] 「{title}」: TMDB 未填写首播日期")
                            continue
                        if first_air > today_str:
                            print(f"    ⏭ [跳过] 「{title}」: 尚未开播（TMDB首播={first_air}，今天={today_str}）")
                            continue

                        # 🔴 新增：拿着 id 去请求详情，获取最新更新日期 (last_air_date)
                        detail_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}"
                        detail_params = {"language": "zh-CN"}
                        if not TMDB_API_KEY.startswith("eyJ"):
                            detail_params["api_key"] = TMDB_API_KEY
                            
                        last_update_date = first_air # 默认用首播日期兜底
                        try:
                            async with session.get(detail_url, params=detail_params, headers=headers) as d_resp:
                                if d_resp.status == 200:
                                    d_data = await d_resp.json()
                                    last_update_date = d_data.get("last_air_date") or first_air
                                    # 详情接口的剧照才是权威来源（搜索接口索引滞后）
                                    real_backdrop = d_data.get("backdrop_path")
                                    if real_backdrop and not (backdrop_path and not used_poster_fallback):
                                        if used_poster_fallback:
                                            print(f"    ✅ [剧照] 「{title}」: 详情接口取到真实剧照，替换海报兜底")
                                        backdrop_path = real_backdrop
                        except Exception as e:
                            pass # 详情获取失败不影响主体逻辑

                        genre_ids = res.get("genre_ids", [])
                        genre_names = ",".join([GENRE_MAP.get(gid) for gid in genre_ids if GENRE_MAP.get(gid)])
                        
                        info = {
                            "id": str(tmdb_id),
                            "type": "tmdb",
                            "title": res.get("name"),
                            "description": res.get("overview"),
                            "rating": res.get("vote_average"),
                            "voteCount": res.get("vote_count"),
                            "popularity": res.get("popularity"),
                            "releaseDate": first_air,
                            "lastUpdateDate": last_update_date, # 🔴 新增：这里保存给前端排序用
                            "posterPath": poster_path,
                            "backdropPath": backdrop_path,
                            "mediaType": "tv",
                            "genreTitle": genre_names
                        }
                        cache[cache_key] = info
                        return info
    except: pass
    return None

async def process_theater(session, theater, cache):
    douban_data = await fetch_doulist_pages(session, theater)
    items = douban_data["items"]
    
    shows = []
    # 控制并发，防止 TMDB 报错
    for i in range(0, len(items), 5):
        chunk = items[i:i + 5]
        tasks = [search_tmdb(session, item, cache) for item in chunk]
        results = await asyncio.gather(*tasks)
        for tmdb_info in results:
            if tmdb_info:
                shows.append(tmdb_info)
        await asyncio.sleep(0.3) # ⚠️ 稍微放慢一点点，因为多了二次详情请求

    # 经过 search_tmdb 过滤，能留下的 100% 都是已开播的数据，所以 upcoming 恒定为空数组即可
    aired = shows
    upcoming = []
            
    # 已开播按时间倒序排列 (最新的在前面)
    aired.sort(key=lambda x: x.get("releaseDate") or "0000-00-00", reverse=True)
    
    print(f"✅ [{theater['name']}] 处理完成: 共发现 {len(items)} 部，完美匹配 {len(shows)} 部 (全部为已播双图精品)")
    
    return {
        theater["name"]: {
            "aired": aired,
            "upcoming": upcoming, # 保持结构兼容前端，即使为空
            "totalItems": len(items),
            "totalPages": douban_data["page_count"]
        }
    }

async def main():
    if not TMDB_API_KEY:
        print("❌ 错误: 未检测到 TMDB_API_KEY！")
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    
    tz_bj = datetime.timezone(datetime.timedelta(hours=8))
    final_data = {
        "last_updated": datetime.datetime.now(tz_bj).strftime("%Y-%m-%d %H:%M:%S")
    }

    async with aiohttp.ClientSession() as session:
        cache = {}
        for theater in THEATERS:
            theater_result = await process_theater(session, theater, cache)
            final_data.update(theater_result)

    total_items = sum(
        len(v.get("aired", [])) + len(v.get("upcoming", []))
        for v in final_data.values() if isinstance(v, dict)
    )
    if total_items == 0:
        print("❌ 所有剧场结果均为空，保留旧数据，不覆盖 theater-data.json")
        return

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final_data, f, ensure_ascii=False, indent=2)
        
    print(f"\n🎉 伟大工程完成！所有洁净版剧场数据已保存至 {OUTPUT_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
