WidgetMetadata = {
    id: "movie_ultimate_makka_v2",
    title: "全能电影榜",
    description: "提供流行、高分、年度最佳以及按类型探索电影",
    author: "𝙈𝙖𝙠𝙠𝙖𝙋𝙖𝙠𝙠𝙖",
    version: "1.3.0", // 更新版本号
    requiredVersion: "0.0.1",
    modules: [
        {
            title: "电影榜单",
            functionName: "loadMovieList",
            // --- 核心修正 1：改用 video 类型以获得更好的影视元数据排版支持 ---
            type: "video", 
            cacheDuration: 3600,
            params: [
                // --- 一级菜单：主分类 ---
                {
                    name: "category",
                    title: "榜单分类",
                    type: "enumeration",
                    value: "popular", // 默认显示流行榜
                    enumOptions: [
                        { title: "🔥 流行趋势 (Popular)", value: "popular" },
                        { title: "⭐️ 历史高分 (Top Rated)", value: "top_rated" },
                        { title: "💰 全球票房榜 (Box Office)", value: "box_office" },
                        { title: "🏆 奥斯卡佳片 (Oscar)", value: "oscar" },
                        { title: "🎬 年度最佳电影", value: "best_of_years" },
                        { title: "🏷️ 按类型探索", value: "by_genre" }
                    ]
                },
                // --- 二级菜单 A：年份选择 (仅在选中“年度最佳”时出现) ---
                {
                    name: "year",
                    title: "选择年份",
                    type: "enumeration",
                    value: "2024",
                    belongTo: { paramName: "category", value: ["best_of_years"] },
                    enumOptions: [
                        { title: "2025", value: "2025" },
                        { title: "2024", value: "2024" },
                        { title: "2023", value: "2023" },
                        { title: "2022", value: "2022" },
                        { title: "2021", value: "2021" },
                        { title: "2020", value: "2020" },
                        { title: "2019", value: "2019" },
                        { title: "2018", value: "2018" },
                        { title: "2017", value: "2017" },
                        { title: "2016", value: "2016" },
                        { title: "2015", value: "2015" }
                    ]
                },
                // --- 二级菜单 B：类型选择 (仅在选中“按类型探索”时出现) ---
                {
                    name: "genre",
                    title: "选择类型",
                    type: "enumeration",
                    value: "878", // 默认科幻
                    belongTo: { paramName: "category", value: ["by_genre"] },
                    enumOptions: [
                        { title: "🛸 科幻 (Sci-Fi)", value: "878" },
                        { title: "🎭 剧情 (Drama)", value: "18" },
                        { title: "🤯 悬疑 (Mystery)", value: "9648" },
                        { title: "💥 动作 (Action)", value: "28" },
                        { title: "😂 喜剧 (Comedy)", value: "35" },
                        { title: "❤️ 爱情 (Romance)", value: "10749" },
                        { title: "👻 恐怖 (Horror)", value: "27" },
                        { title: "🔪 犯罪 (Crime)", value: "80" },
                        { title: "🧙‍♂️ 奇幻 (Fantasy)", value: "14" },
                        { title: "🦄 动画 (Animation)", value: "16" }
                    ]
                },
                // --- 翻页组件 ---
                { name: "page", title: "页码", type: "page", startPage: 1 }
            ]
        }
    ]
};

// ================= 逻辑处理部分 =================

const GENRE_MAP = {
    28: "动作", 12: "冒险", 16: "动画", 35: "喜剧", 80: "犯罪", 99: "纪录片",
    18: "剧情", 10751: "家庭", 14: "奇幻", 36: "历史", 27: "恐怖", 10402: "音乐",
    9648: "悬疑", 10749: "爱情", 878: "科幻", 10770: "电视电影", 53: "惊悚",
    10752: "战争", 37: "西部", 10759: "动作冒险"
};

function getGenreText(ids) {
    if (!ids || !Array.isArray(ids)) return "电影";
    const genres = ids.map(id => GENRE_MAP[id]).filter(Boolean);
    return genres.length > 0 ? genres.slice(0, 2).join(" / ") : "电影";
}

// --- 核心修正 2：完全对齐参考代码的字段结构 ---
function buildItem(item) {
    if (!item) return null;
    
    const releaseDate = item.release_date || "";
    const score = item.vote_average ? item.vote_average.toFixed(1) : "0.0";
    const genreText = getGenreText(item.genre_ids);

    return {
        id: String(item.id),
        tmdbId: parseInt(item.id),
        type: "tmdb",
        mediaType: "movie",
        title: item.title,
        
        // 修正 A：只留类型标签，不要手动加年份。Forward 会在横版时自动把年份加上。
        genreTitle: genreText,
        
        // 修正 B：竖版海报下方读取的是 description，我们把日期和评分放这里
        description: releaseDate ? `${releaseDate} · ⭐ ${score}` : `⭐ ${score}`,
        
        // 修正 C：传给内核的日期字段，内核会自动提取年份给横版 UI
        releaseDate: releaseDate,
        
        // 修正 D：使用与参考代码一致的图片字段名
        posterPath: item.poster_path ? `https://image.tmdb.org/t/p/w500${item.poster_path}` : "",
        backdropPath: item.backdrop_path ? `https://image.tmdb.org/t/p/w780${item.backdrop_path}` : "",
        
        rating: parseFloat(score)
    };
}

// 主请求函数
async function loadMovieList(params) {
    const category = params.category || "popular";
    const page = params.page || 1;

    try {
        let endpoint = "";
        let queryParams = { 
            language: "zh-CN", 
            page: page 
        };

        if (category === "popular") {
            endpoint = "/movie/popular";
        } else if (category === "top_rated") {
            endpoint = "/movie/top_rated";
        } else if (category === "box_office") {
            endpoint = "/discover/movie";
            queryParams.sort_by = "revenue.desc";
        } else if (category === "oscar") {
            endpoint = "/discover/movie";
            queryParams.with_keywords = "818";
            queryParams.sort_by = "vote_average.desc";
            queryParams["vote_count.gte"] = 1000;
        } else if (category === "best_of_years") {
            const targetYear = params.year || "2024";
            endpoint = "/discover/movie";
            queryParams.primary_release_year = targetYear;
            queryParams.sort_by = "vote_average.desc";
            queryParams["vote_count.gte"] = 500; 
        } else if (category === "by_genre") {
            const targetGenre = params.genre || "878";
            endpoint = "/discover/movie";
            queryParams.with_genres = targetGenre;
            queryParams.sort_by = "popularity.desc"; 
        }

        const res = await Widget.tmdb.get(endpoint, { params: queryParams });
        
        const items = (res.results || []).map(i => buildItem(i)).filter(Boolean);
        return items;

    } catch (error) {
        console.error("数据请求异常:", error);
        return [{
            id: "error",
            type: "text",
            title: "加载异常",
            description: "网络开小差了，请下拉刷新重试"
        }];
    }
}
