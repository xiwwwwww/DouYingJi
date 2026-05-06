"""
豆瓣电影抓取：支持多数据源、数量上限与条件筛选。
排行榜：https://movie.douban.com/chart
Top250（可翻页）：https://movie.douban.com/top250
"""
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from web_app.config import MYSQL_DB, MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, MYSQL_USER

CHART_URL = "https://movie.douban.com/chart"
# 说明：新片 / 一周口碑 / 北美票房 等同页多个版块，由 parse_chart_page_sections 按标题拆分。
TOP250_BASE = "https://movie.douban.com/top250"
# 豆瓣「选电影 / 探索」使用的 JSON 接口（分页拉取，用于大范围发现条目）
EXPLORE_JSON_API = "https://movie.douban.com/j/new_search_subjects"
EXPLORE_REFERER = "https://movie.douban.com/explore"
EXPLORE_PAGE_SIZE = 20
EXPLORE_MAX_PAGES_HARD = 100  # 单次任务最多翻页（约 2000 条原始候选）

# 豆瓣电影「关键词搜索」结果页（内嵌 window.__DATA__ JSON）
SEARCH_SUBJECT_SEARCH = "https://search.douban.com/movie/subject_search"
SEARCH_PAGE_SIZE = 15
SEARCH_MAX_PAGES_HARD = 80  # 约 1200 条候选上限

_STATIC_ROOT = Path(__file__).resolve().parent / "static"
_POSTER_DIR = _STATIC_ROOT / "posters"
SUBJECT_PAGE_BASE = "https://movie.douban.com/subject/{}/"
# 免 PoW 的条目摘要 JSON（导演/演员/片长等）
SUBJECT_ABSTRACT_API = "https://movie.douban.com/j/subject_abstract"
# 短评列表 JSON（热门排序，用于入库「摘录」字段）
SUBJECT_COMMENTS_API = "https://movie.douban.com/j/subject/{}/comments"
COMMENT_FETCH_MAX = 8
# 每条条目抓取详情页 + 下载封面后的间隔（秒），减轻对方站点压力
ENRICH_PAUSE_SEC = 0.35

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://movie.douban.com/",
    "Connection": "keep-alive",
}

# 请求失败或解析为空时使用的内置备用行（保证服务可用、界面非空库）
FALLBACK_ROWS = [
    {
        "subject_id": "37116612",
        "title": "世界的主人",
        "release_year": 2025,
        "region": "韩国",
        "tags": "剧情",
        "rating": 9.2,
        "votes": 70550,
        "page_url": "https://movie.douban.com/subject/37116612/",
    },
    {
        "subject_id": "26967871",
        "title": "寂静的朋友",
        "release_year": 2025,
        "region": "中国大陆",
        "tags": "剧情",
        "rating": 7.9,
        "votes": 12000,
        "page_url": "https://movie.douban.com/subject/26967871/",
    },
    {
        "subject_id": "36219411",
        "title": "像我这样的爱情",
        "release_year": 2024,
        "region": "中国大陆",
        "tags": "爱情,剧情",
        "rating": 8.1,
        "votes": 8600,
        "page_url": "https://movie.douban.com/subject/36219411/",
    },
]


def _normalize_poster_url(raw: object) -> Optional[str]:
    """接口里 cover / cover_url 可能是字符串或嵌套对象。"""
    if raw is None:
        return None
    if isinstance(raw, dict):
        u = raw.get("large") or raw.get("medium") or raw.get("small") or raw.get("url")
        if isinstance(u, str) and u.strip():
            return u.strip().split("?")[0]
        return None
    if isinstance(raw, str):
        s = raw.strip()
        return s.split("?")[0] if s else None
    return None


def _empty_detail() -> Dict[str, Optional[str]]:
    return {
        "summary": None,
        "directors": None,
        "casts": None,
        "aliases": None,
        "duration": None,
        "imdb_id": None,
    }


def _is_pow_challenge_page(html: str) -> bool:
    """豆瓣对脚本请求常返回「载入中」+ PoW 表单，无正文 DOM。"""
    if not html or len(html) < 1500:
        return True
    if 'name="sec"' in html and "载入中" in html:
        return True
    if "link-report" not in html and "v:summary" not in html and "og:description" not in html:
        if len(html) < 8000:
            return True
    return False


def parse_subject_page(html: str) -> Dict[str, Optional[str]]:
    """解析豆瓣电影条目页 HTML：导演、主演、又名、片长、IMDb（不写 summary，摘录用短评接口）。"""
    out = _empty_detail()
    if not html or len(html) < 800:
        return out

    soup = BeautifulSoup(html, "html.parser")

    info = soup.select_one("#info")
    if info:
        for pl in info.select("span.pl"):
            lab = (pl.get_text("", strip=True) or "").replace("\xa0", " ")
            lab = lab.strip().strip(":").strip("：")
            attrs_el = pl.find_next("span", class_="attrs")
            if attrs_el:
                val = attrs_el.get_text(" / ", strip=True)
            else:
                chunk = []
                sib = pl.next_sibling
                while sib is not None:
                    nm = getattr(sib, "name", None)
                    if nm == "br":
                        break
                    if nm == "span" and "pl" in (sib.get("class") or []):
                        break
                    if nm == "span" and "attrs" in (sib.get("class") or []):
                        chunk.append(sib.get_text(" / ", strip=True))
                        break
                    if nm == "a":
                        chunk.append(sib.get_text(strip=True))
                    elif nm == "span":
                        chunk.append(sib.get_text(" ", strip=True))
                    elif isinstance(sib, str) and sib.strip():
                        chunk.append(sib.strip())
                    sib = sib.next_sibling
                val = " ".join(chunk).strip().lstrip(":").lstrip("：").strip()

            if not val:
                continue
            if lab == "导演" or lab.endswith("导演"):
                out["directors"] = val[:512]
            elif lab == "主演" or lab.endswith("主演"):
                out["casts"] = val[:768]
            elif "又名" in lab:
                out["aliases"] = val[:512]
            elif "片长" in lab:
                out["duration"] = val[:64]

        for a in info.select('a[href*="imdb.com/title"]'):
            href = a.get("href") or ""
            m = re.search(r"(tt\d+)", href, re.I)
            if m:
                out["imdb_id"] = m.group(1)
                break
        if not out["imdb_id"]:
            tinfo = info.get_text("\n", strip=True)
            m = re.search(r"tt\d+", tinfo)
            if m:
                out["imdb_id"] = m.group(0)

        block = info.get_text("\n", strip=True)
        if not out["directors"]:
            m = re.search(r"导演\s*[：:\s]+\s*([^\n\r]+)", block)
            if m:
                out["directors"] = m.group(1).strip()[:512]
        if not out["casts"]:
            m = re.search(r"主演\s*[：:\s]+\s*([^\n\r]+)", block)
            if m:
                out["casts"] = m.group(1).strip()[:768]
        if not out["aliases"]:
            m = re.search(r"又名\s*[：:\s]+\s*([^\n\r]+)", block)
            if m:
                out["aliases"] = m.group(1).strip()[:512]
        if not out["duration"]:
            m = re.search(r"片长\s*[：:\s]+\s*([^\n\r]+)", block)
            if m:
                out["duration"] = m.group(1).strip()[:64]

    return out


def fetch_subject_api_meta(subject_id: str) -> Dict[str, Optional[str]]:
    """
    调用 j/subject_abstract（免 PoW）：导演、演员、片长等，不写 summary。
    """
    out = _empty_detail()
    sid = str(subject_id or "").strip()
    if not sid.isdigit():
        return out
    hdr = dict(HEADERS)
    hdr["Referer"] = SUBJECT_PAGE_BASE.format(sid)
    hdr["Accept"] = "application/json, text/plain, */*"
    try:
        r = requests.get(SUBJECT_ABSTRACT_API, params={"subject_id": sid}, headers=hdr, timeout=22)
        r.raise_for_status()
        js = r.json()
    except Exception:
        return out
    if js.get("r") != 0:
        return out
    sub = js.get("subject") or {}
    dirs = sub.get("directors") or []
    if dirs:
        out["directors"] = " / ".join(str(x) for x in dirs)[:512]
    acts = sub.get("actors") or []
    if acts:
        out["casts"] = " / ".join(str(x) for x in acts)[:768]
    dur = (sub.get("duration") or "").strip()
    if dur:
        out["duration"] = dur[:64]
    return out


def fetch_subject_comments(subject_id: str, max_items: int = COMMENT_FETCH_MAX) -> Optional[str]:
    """
    拉取 j/subject/{id}/comments JSON（免 PoW），拼成多条短评摘录写入 summary 字段。
    """
    sid = str(subject_id or "").strip()
    if not sid.isdigit():
        return None
    n = max(1, min(int(max_items), 20))
    url = SUBJECT_COMMENTS_API.format(sid)
    hdr = dict(HEADERS)
    hdr["Referer"] = SUBJECT_PAGE_BASE.format(sid)
    hdr["Accept"] = "application/json, text/plain, */*"
    try:
        r = requests.get(
            url,
            params={"start": 0, "limit": n, "status": "P", "sort": "new_score"},
            headers=hdr,
            timeout=22,
        )
        r.raise_for_status()
        js = r.json()
    except Exception:
        return None
    if js.get("retcode") != 1:
        return None
    result = js.get("result") or {}
    normal = result.get("normal")
    if not isinstance(normal, list) or not normal:
        return None
    lines: List[str] = []
    for item in normal[:n]:
        if not isinstance(item, dict):
            continue
        content = (item.get("content") or "").strip()
        if not content:
            continue
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        name = (user.get("name") or "").strip() or "匿名"
        rw = (item.get("rating_word") or "").strip()
        head = f"【{name}"
        if rw:
            head += f" · {rw}"
        head += "】"
        lines.append(f"{head}\n{content}")
    if not lines:
        return None
    return "\n\n".join(lines)[:12000]


def fetch_subject_enrichment(subject_id: str) -> Dict[str, Optional[str]]:
    """
    详情：JSON 摘要 + 热门短评；条目 HTML 非验证页时解析导演等覆盖。
    summary 存「多条短评摘录」，不再抓剧情正文。
    """
    sid = str(subject_id or "").strip()
    if not sid.isdigit():
        return _empty_detail()

    api_meta = fetch_subject_api_meta(sid)
    comments = fetch_subject_comments(sid)

    merged = dict(api_meta)

    url = SUBJECT_PAGE_BASE.format(sid)
    hdr = dict(HEADERS)
    hdr["Referer"] = "https://movie.douban.com/"
    text = ""
    try:
        r = requests.get(url, headers=hdr, timeout=22)
        r.raise_for_status()
        text = r.text
    except Exception:
        if comments:
            merged["summary"] = comments
        return merged

    if "检测到有异常请求" in text or "豆瓣说你访问频率太快" in text:
        if comments:
            merged["summary"] = comments
        return merged
    if _is_pow_challenge_page(text):
        if comments:
            merged["summary"] = comments
        return merged

    parsed = parse_subject_page(text)
    for k, v in parsed.items():
        if v:
            merged[k] = v
    if comments:
        merged["summary"] = comments
    return merged


def save_poster_local(subject_id: str, poster_url: Optional[str]) -> Optional[str]:
    """把远程封面保存到 static/posters/{subject_id}.xxx，返回相对 static 的路径（如 posters/123.jpg）。"""
    sid = str(subject_id or "").strip()
    if not poster_url or not sid:
        return None
    try:
        _POSTER_DIR.mkdir(parents=True, exist_ok=True)
        hdr = dict(HEADERS)
        hdr["Referer"] = "https://movie.douban.com/"
        r = requests.get(poster_url, headers=hdr, timeout=28)
        r.raise_for_status()
        if len(r.content) < 256:
            return None
        ct = (r.headers.get("Content-Type") or "").lower()
        ext = ".jpg"
        if "png" in ct:
            ext = ".png"
        elif "webp" in ct:
            ext = ".webp"
        low = poster_url.lower()
        if low.endswith(".png"):
            ext = ".png"
        elif low.endswith(".webp"):
            ext = ".webp"

        fn = f"{sid}{ext}"
        path = _POSTER_DIR / fn
        path.write_bytes(r.content)
        return f"posters/{fn}"
    except Exception:
        return None


def enrich_row_for_store(row: Dict) -> Dict:
    """补充条目页详情 + 本地封面路径（合并进 row）。"""
    er = dict(row)
    for k in (
        "poster_local",
        "summary",
        "directors",
        "casts",
        "aliases",
        "duration",
        "imdb_id",
    ):
        er.setdefault(k, None)

    sid = str(er.get("subject_id") or "").strip()
    if not sid:
        return er

    try:
        det = fetch_subject_enrichment(sid)
        for k in ("directors", "casts", "aliases", "duration", "imdb_id"):
            if det.get(k):
                er[k] = det[k]
        ds = det.get("summary")
        if ds:
            er["summary"] = ds
    except Exception:
        pass

    try:
        local = save_poster_local(sid, er.get("poster_url"))
        if local:
            er["poster_local"] = local
    except Exception:
        pass

    time.sleep(ENRICH_PAUSE_SEC)
    return er


# 单次任务最多入库条数上限（探索模式可较大；请勿对豆瓣高频施压）
ABS_MAX_LIMIT = 2000
# Top250 每页约 25 条，最多翻页数
TOP250_MAX_PAGES = 10

# 从豆瓣简介行中匹配地区、类型（排行榜页文本较乱，类型用关键词扫描）
_REGION_RE = re.compile(
    r"中国大陆|中国香港|中国台湾|美国|日本|韩国|英国|法国|德国|意大利|加拿大|澳大利亚|印度|西班牙|俄罗斯|瑞典|丹麦|波兰|墨西哥|巴西|新西兰|爱尔兰|比利时|荷兰|瑞士|奥地利|挪威|芬兰|泰国|越南|伊朗|以色列|南非|捷克|匈牙利|阿根廷|智利|哥伦比亚"
)
_KNOWN_GENRES = (
    "剧情",
    "喜剧",
    "动作",
    "爱情",
    "科幻",
    "动画",
    "悬疑",
    "恐怖",
    "纪录片",
    "短片",
    "奇幻",
    "犯罪",
    "战争",
    "家庭",
    "音乐",
    "传记",
    "历史",
    "武侠",
    "古装",
    "惊悚",
    "歌舞",
    "运动",
    "灾难",
    "西部",
    "儿童",
    "黑色幽默",
)


def _meta_from_chart_tr(tr) -> Tuple[Optional[int], Optional[str], str]:
    """从排行榜 `tr.item` 的简介段解析年份、地区、类型标签。"""
    year, region, tags_s = None, None, ""
    p = tr.select_one("div.pl2 p")
    if not p:
        return year, region, tags_s
    text = p.get_text(" ", strip=True)
    ym = re.search(r"(19|20)\d{2}", text)
    if ym:
        try:
            y = int(ym.group(0))
            if 1900 <= y <= 2035:
                year = y
        except ValueError:
            pass
    rm = _REGION_RE.search(text)
    if rm:
        region = rm.group(0)
    found = [g for g in _KNOWN_GENRES if g in text]
    tags_s = ",".join(found)
    return year, region, tags_s


def _meta_from_top250_li(li) -> Tuple[Optional[int], Optional[str], str]:
    """从 Top250 条目 `div.bd p` 第二行「年份 / 国家 / 类型」解析。"""
    year, region, tags_s = None, None, ""
    p = li.select_one("div.bd p")
    if not p:
        return year, region, tags_s
    for line in p.get_text("\n").split("\n"):
        line = line.strip()
        if not line or line.startswith("导演"):
            continue
        if "/" not in line:
            continue
        parts = [x.strip() for x in line.split("/") if x.strip()]
        if not parts:
            continue
        if re.match(r"^(19|20)\d{2}$", parts[0]):
            year = int(parts[0])
        if len(parts) >= 2:
            region = parts[1][:64]
        if len(parts) >= 3:
            tags_s = ",".join(parts[2].split())
        break
    return year, region, tags_s


def _row_from_chart_tr(tr, idx: int) -> Optional[dict]:
    """解析排行榜单行 `tr.item`。"""
    a = tr.select_one('div.pl2 > a[href*="subject"]')
    if not a or not a.get("href"):
        return None
    href = a["href"]
    m = re.search(r"subject/(\d+)/", href)
    subject_id = m.group(1) if m else ""
    title = a.get_text(strip=True).split("/")[0].strip()

    rate_el = tr.select_one("span.rating_nums")
    rating = None
    if rate_el:
        try:
            rating = float(rate_el.get_text(strip=True))
        except ValueError:
            pass

    votes = None
    pl = tr.select_one("span.pl")
    if pl:
        vm = re.search(r"([\d,]+)\s*人评价", pl.get_text())
        if vm:
            votes = int(vm.group(1).replace(",", ""))

    ry, reg, tgs = _meta_from_chart_tr(tr)

    pic_el = (
        tr.select_one("td.pic img")
        or tr.select_one("div.pic img")
        or tr.select_one("a.nbg img")
        or tr.select_one("img[src*='doubanio.com']")
    )
    poster_url = _normalize_poster_url(pic_el.get("src") if pic_el else None)

    return {
        "subject_id": subject_id,
        "title": title[:500],
        "release_year": ry,
        "region": reg,
        "tags": tgs,
        "rating": rating,
        "votes": votes,
        "chart_rank": idx,
        "page_url": href.split("?")[0],
        "poster_url": poster_url,
    }


def parse_chart_html(html: str) -> List[dict]:
    """解析排行榜页全部 `tr.item`（不区分版块；版块拆分见 parse_chart_page_sections）。"""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for idx, tr in enumerate(soup.select("tr.item"), start=1):
        item = _row_from_chart_tr(tr, idx)
        if item and item.get("subject_id"):
            rows.append(item)
    return rows


def _chart_section_key_from_h2(text: str) -> Optional[str]:
    """将版块标题映射为内部键：new / weekly / na_boxoffice。"""
    if not text:
        return None
    t = text.replace("\xa0", " ").strip()
    if "北美票房" in t:
        return "na_boxoffice"
    if "一周口碑" in t or ("口碑榜" in t and "一周" in t):
        return "weekly"
    if "新片" in t or "豆瓣新片" in t:
        return "new"
    return None


def parse_chart_page_sections(html: str) -> Dict[str, List[dict]]:
    """
    解析排行榜首页多个版块（豆瓣新片榜 / 一周口碑榜 / 北美票房榜）。
    各版块为紧随对应 h2 标题后的第一张含 `tr.item` 的 table。
    """
    soup = BeautifulSoup(html, "html.parser")
    out: Dict[str, List[dict]] = {"new": [], "weekly": [], "na_boxoffice": []}
    for h2 in soup.find_all("h2"):
        key = _chart_section_key_from_h2(h2.get_text(" ", strip=True))
        if not key:
            continue
        table = h2.find_next("table")
        while table is not None and not table.select("tr.item"):
            table = table.find_next("table")
        if not table:
            continue
        trs = table.select("tr.item")
        if not trs:
            continue
        rows_sec: List[dict] = []
        for idx, tr in enumerate(trs, start=1):
            row = _row_from_chart_tr(tr, idx)
            if row and row.get("subject_id"):
                rows_sec.append(row)
        if rows_sec:
            out[key] = rows_sec
    return out


CHART_MODE_LABELS = {
    "new": "豆瓣排行榜·新片榜",
    "weekly": "豆瓣排行榜·一周口碑榜",
    "na_boxoffice": "豆瓣排行榜·北美票房榜",
}


def chart_source_label(chart_mode: str) -> str:
    """入库 source_label 可读前缀。"""
    cm = (chart_mode or "new").strip().lower()
    base = CHART_MODE_LABELS.get(cm, CHART_MODE_LABELS["new"])
    return f"{base} ({CHART_URL})"


def parse_top250_html(html: str, rank_offset: int = 0) -> List[dict]:
    """解析 Top250 列表页 `ol.grid_view li`。"""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    lis = soup.select("ol.grid_view li")
    for i, li in enumerate(lis):
        rank = rank_offset + i + 1
        a = li.select_one("div.hd a[href*='subject']")
        if not a or not a.get("href"):
            continue
        href = a["href"].split("?")[0]
        m = re.search(r"subject/(\d+)/", href)
        subject_id = m.group(1) if m else ""
        ts = a.select_one("span.title")
        title = ts.get_text(strip=True) if ts else a.get_text(strip=True).split("/")[0].strip()

        rate_el = li.select_one("span.rating_num")
        rating = None
        if rate_el:
            try:
                rating = float(rate_el.get_text(strip=True))
            except ValueError:
                pass

        votes = None
        bd = li.select_one("div.bd")
        if bd:
            for sp in bd.find_all("span"):
                tx = sp.get_text()
                if "人评价" not in tx:
                    continue
                vm = re.search(r"([\d,]+)\s*人评价", tx)
                if vm:
                    votes = int(vm.group(1).replace(",", ""))
                break

        ry, reg, tgs = _meta_from_top250_li(li)

        pic_el = li.select_one("div.pic img") or li.select_one("img[src*='doubanio.com']")
        poster_url = _normalize_poster_url(pic_el.get("src") if pic_el else None)

        rows.append(
            {
                "subject_id": subject_id,
                "title": title[:500],
                "release_year": ry,
                "region": reg,
                "tags": tgs,
                "rating": rating,
                "votes": votes,
                "chart_rank": rank,
                "page_url": href,
                "poster_url": poster_url,
            }
        )
    return rows


def fetch_top250_rows(max_raw: int, start_offset: int = 0) -> List[dict]:
    """翻页抓取 Top250，最多 max_raw 条（用于后续筛选）。
    start_offset：列表起点，与页面 URL `?start=` 一致，通常为 0、25、…、225。
    """
    rows: List[dict] = []
    start_param = max(0, min(int(start_offset), 249))
    start_param = (start_param // 25) * 25
    pages = 0
    while len(rows) < max_raw and start_param < 250 and pages < TOP250_MAX_PAGES:
        url = f"{TOP250_BASE}?start={start_param}"
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        batch = parse_top250_html(r.text, rank_offset=len(rows))
        if not batch:
            break
        rows.extend(batch)
        start_param += 25
        pages += 1
        if len(rows) >= max_raw:
            break
    return rows[:max_raw]


def _row_from_explore_item(item: dict, rank: int, hint_tag: str) -> dict:
    """解析探索接口返回的单条 JSON。"""
    sid = str(item.get("id") or "").strip()
    title = (item.get("title") or "").strip()
    rate = item.get("rate")
    rating = None
    if rate not in (None, ""):
        try:
            rating = float(str(rate).strip())
        except ValueError:
            pass
    url = (item.get("url") or "").strip()
    if not url and sid:
        url = f"https://movie.douban.com/subject/{sid}/"
    tags = (hint_tag or "").strip()
    poster_raw = item.get("cover") or item.get("pic") or item.get("img")
    poster_url = _normalize_poster_url(poster_raw)
    return {
        "subject_id": sid,
        "title": title[:500],
        "release_year": None,
        "region": None,
        "tags": tags,
        "rating": rating,
        "votes": None,
        "chart_rank": rank,
        "page_url": url.split("?")[0],
        "poster_url": poster_url,
    }


def fetch_explore_rows(
    max_raw: int,
    *,
    tag: str = "",
    sort: str = "U",
    rating_range: str = "0,10",
    year_range: str = "",
    max_pages: int = 50,
    start_offset: int = 0,
    pause_sec: float = 0.35,
) -> Tuple[List[dict], str]:
    """
    豆瓣探索 JSON 分页：按 start 递增直至凑够条数或达到页数上限。
    start_offset：接口参数 start 的初值（从第几条结果开始向后翻页）。
    若接口返回风控/登录提示，则尽可能返回已收集部分并带上状态说明。
    """
    max_raw = max(1, min(max_raw, ABS_MAX_LIMIT))
    max_pages = max(1, min(int(max_pages), EXPLORE_MAX_PAGES_HARD))

    rows: List[dict] = []
    start = max(0, int(start_offset))
    pages = 0
    status = "ok"

    hdr = dict(HEADERS)
    hdr["Referer"] = EXPLORE_REFERER
    hdr["Accept"] = "application/json, text/plain, */*"

    while len(rows) < max_raw and pages < max_pages:
        params: Dict[str, object] = {
            "sort": sort or "U",
            "range": rating_range or "0,10",
            "tags": tag.strip(),
            "start": start,
        }
        yr = (year_range or "").strip()
        if yr:
            params["year_range"] = yr

        try:
            r = requests.get(EXPLORE_JSON_API, params=params, headers=hdr, timeout=28)
            r.raise_for_status()
            js = r.json()
        except Exception as ex:
            status = f"http_err:{ex}"
            break

        if js.get("r") == 1:
            status = "blocked:" + str(js.get("msg", ""))[:120]
            break

        batch = js.get("data")
        if batch is None:
            batch = js.get("subjects", [])
        if not isinstance(batch, list) or not batch:
            status = "empty_batch"
            break

        for item in batch:
            if len(rows) >= max_raw:
                break
            if not isinstance(item, dict):
                continue
            rows.append(_row_from_explore_item(item, len(rows) + 1, tag))

        start += len(batch)
        pages += 1

        if len(batch) < EXPLORE_PAGE_SIZE:
            status = "eof_short_page"

        time.sleep(pause_sec)

    if not rows and status == "ok":
        status = "no_items"
    return rows[:max_raw], status


def _summary_from_search_abstract(abstract: str) -> Optional[str]:
    """搜索结果里的 abstract：较长或含句号时当作简介候选。"""
    a = (abstract or "").strip()
    if len(a) < 30:
        return None
    if a.count("/") >= 2 and len(a) < 120 and "。" not in a and "\n" not in a:
        return None
    return a[:12000]


def _parse_search_window_data(html: str) -> Optional[dict]:
    """从搜索结果页解析 `window.__DATA__ = {...}`。"""
    m = re.search(r"window\.__DATA__\s*=\s*", html)
    if not m:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html, m.end())
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _row_from_search_item(it: dict, rank: int) -> dict:
    """单条搜索结果（含评分、评价人数、简介中的地区/类型）。"""
    sid = str(it.get("id") or "").strip()
    raw_title = (it.get("title") or "").replace("\u200e", "").strip()
    title = raw_title[:500]
    url = (it.get("url") or "").strip().split("?")[0]
    if not url and sid:
        url = f"https://movie.douban.com/subject/{sid}/"

    rating_val = None
    votes = None
    ri = it.get("rating")
    if isinstance(ri, dict):
        v = ri.get("value")
        if v is not None:
            try:
                rating_val = float(v)
            except (TypeError, ValueError):
                pass
        c = ri.get("count")
        if c is not None:
            try:
                votes = int(c)
            except (TypeError, ValueError):
                pass

    release_year = None
    ym = re.search(r"\((\d{4})\)", raw_title)
    if ym:
        release_year = int(ym.group(1))

    abstract = (it.get("abstract") or "").strip()
    region = None
    tag_parts: List[str] = []
    if abstract:
        segs = [x.strip() for x in abstract.split("/") if x.strip()]
        if segs:
            region = segs[0][:64]
            for s in segs[1:]:
                if len(s) <= 16 and not re.match(r"^\d", s):
                    tag_parts.append(s)
    tags = ",".join(tag_parts[:12])

    poster_url = _normalize_poster_url(it.get("cover_url") or it.get("cover"))

    return {
        "subject_id": sid,
        "title": title,
        "release_year": release_year,
        "region": region,
        "tags": tags,
        "rating": rating_val,
        "votes": votes,
        "chart_rank": rank,
        "page_url": url,
        "poster_url": poster_url,
        "summary": _summary_from_search_abstract(it.get("abstract") or ""),
    }


def fetch_search_rows(
    max_raw: int,
    query: str,
    *,
    max_pages: int = 30,
    start_offset: int = 0,
    pause_sec: float = 0.4,
) -> Tuple[List[dict], str]:
    """
    关键词搜索：翻页参数 start=0,15,30...（每页约 15 条）。
    start_offset：从第几条搜索结果开始（与接口 start 一致）。
    """
    q = (query or "").strip()
    if not q:
        return [], "empty_query"

    max_raw = max(1, min(max_raw, ABS_MAX_LIMIT))
    max_pages = max(1, min(int(max_pages), SEARCH_MAX_PAGES_HARD))

    rows: List[dict] = []
    start_offset = max(0, min(int(start_offset), 50000))
    pages = 0
    status = "ok"

    hdr = dict(HEADERS)
    hdr["Referer"] = SEARCH_SUBJECT_SEARCH

    while len(rows) < max_raw and pages < max_pages:
        params = {"search_text": q, "start": start_offset}
        try:
            r = requests.get(SEARCH_SUBJECT_SEARCH, params=params, headers=hdr, timeout=28)
            r.raise_for_status()
        except Exception as ex:
            status = f"http_err:{ex}"
            break

        data = _parse_search_window_data(r.text)
        if not data:
            status = "no_window_data"
            break

        items = data.get("items") or []
        if not items:
            status = "empty_items"
            break

        total = int(data.get("total") or 0)

        for it in items:
            if len(rows) >= max_raw:
                break
            if not isinstance(it, dict):
                continue
            rows.append(_row_from_search_item(it, len(rows) + 1))

        start_offset += len(items)
        pages += 1

        if start_offset >= total:
            status = "eof_total"
            break
        if len(items) < SEARCH_PAGE_SIZE:
            status = "eof_short"
            break

        time.sleep(pause_sec)

    if not rows and status == "ok":
        status = "no_items"
    return rows[:max_raw], status


def fetch_live_rows(
    source: str,
    prefetch_cap: int,
    explore_opts: Optional[dict] = None,
    search_opts: Optional[dict] = None,
    top250_start: int = 0,
    chart_mode: str = "new",
) -> Tuple[List[dict], str]:
    """
    source: chart | top250 | explore | search
    prefetch_cap: 预先最多抓多少条原始记录（用于筛选后再 limit）
    explore_opts: source=explore
    search_opts: source=search，含 query、max_pages、start_offset
    top250_start: source=top250 时列表起点（与 Top250 页 start= 一致）
    chart_mode: source=chart 时选版块 new=新片榜 weekly=一周口碑 na_boxoffice=北美票房
    """
    prefetch_cap = max(1, min(prefetch_cap, ABS_MAX_LIMIT))

    if source == "chart":
        r = requests.get(CHART_URL, headers=HEADERS, timeout=25)
        r.raise_for_status()
        html = r.text
        cm = (chart_mode or "new").strip().lower()
        if cm not in ("new", "weekly", "na_boxoffice"):
            cm = "new"
        sections = parse_chart_page_sections(html)
        rows = sections.get(cm) or []
        if not rows and cm == "new":
            rows = parse_chart_html(html)
        return rows, f"live:chart:{cm}"

    if source == "top250":
        t250 = max(0, min(int(top250_start), 249))
        rows = fetch_top250_rows(prefetch_cap, start_offset=t250)
        return rows, "live:top250"

    if source == "search":
        opts = search_opts or {}
        q = str(opts.get("query", "") or "").strip()
        if not q:
            return [], "live:search:empty_query"
        mp = int(opts.get("max_pages", 30) or 30)
        so = int(opts.get("start_offset", 0) or 0)
        rows, st = fetch_search_rows(prefetch_cap, q, max_pages=mp, start_offset=so)
        return rows, f"live:search:{st}"

    if source == "explore":
        opts = explore_opts or {}
        eso = int(opts.get("start_offset", 0) or 0)
        rows, st = fetch_explore_rows(
            prefetch_cap,
            tag=str(opts.get("tag", "") or ""),
            sort=str(opts.get("sort", "U") or "U"),
            rating_range=str(opts.get("range", "0,10") or "0,10"),
            year_range=str(opts.get("year_range", "") or ""),
            max_pages=int(opts.get("max_pages", 50) or 50),
            start_offset=eso,
        )
        return rows, f"live:explore:{st}"

    rows = parse_chart_html("")
    return rows, "live:unknown"


def apply_filters(
    rows: List[dict],
    *,
    min_rating: Optional[float] = None,
    max_rating: Optional[float] = None,
    title_contains: Optional[str] = None,
    min_votes: Optional[int] = None,
    min_year: Optional[int] = None,
    max_year: Optional[int] = None,
    tag_contains: Optional[str] = None,
) -> List[dict]:
    """按条件筛选，保持原有顺序。"""
    out = []
    title_kw = (title_contains or "").strip()
    tag_kw = (tag_contains or "").strip()
    for r in rows:
        rating = r.get("rating")
        if min_rating is not None:
            if rating is None or rating < min_rating:
                continue
        if max_rating is not None:
            if rating is None or rating > max_rating:
                continue
        if title_kw:
            if title_kw.lower() not in (r.get("title") or "").lower():
                continue
        if min_votes is not None:
            v = r.get("votes")
            if v is None or v < min_votes:
                continue
        ry = r.get("release_year")
        if min_year is not None:
            if ry is None or ry < min_year:
                continue
        if max_year is not None:
            if ry is None or ry > max_year:
                continue
        if tag_kw:
            tags = r.get("tags") or ""
            if tag_kw not in tags:
                continue
        out.append(r)
    return out


def finalize_ranks(rows: List[dict]) -> List[dict]:
    """筛选后重排行名次 1..n。"""
    out = []
    for i, r in enumerate(rows, start=1):
        item = dict(r)
        item["chart_rank"] = i
        out.append(item)
    return out


def fetch_rows_with_fallback(
    source: str,
    prefetch_cap: int,
    limit: int,
    min_rating: Optional[float],
    max_rating: Optional[float],
    title_contains: Optional[str],
    min_votes: Optional[int],
    min_year: Optional[int] = None,
    max_year: Optional[int] = None,
    tag_contains: Optional[str] = None,
    explore_opts: Optional[dict] = None,
    search_opts: Optional[dict] = None,
    top250_start: int = 0,
    chart_mode: str = "new",
) -> Tuple[List[dict], str]:
    """
    抓取 → 筛选 → 按 limit 截断；若解析失败则回退到备用数据。
    返回 (rows, src_tag)。
    """
    try:
        raw, tag = fetch_live_rows(
            source,
            prefetch_cap,
            explore_opts,
            search_opts,
            top250_start=top250_start,
            chart_mode=chart_mode,
        )
        if not raw:
            if source == "chart":
                filtered = finalize_ranks([])
                return filtered[:limit], tag
            raise ValueError("empty_parse")
        filtered = apply_filters(
            raw,
            min_rating=min_rating,
            max_rating=max_rating,
            title_contains=title_contains,
            min_votes=min_votes,
            min_year=min_year,
            max_year=max_year,
            tag_contains=tag_contains,
        )
        filtered = finalize_ranks(filtered)
        filtered = filtered[:limit]
        return filtered, tag
    except Exception as ex:
        fb = [dict(r, chart_rank=i + 1) for i, r in enumerate(FALLBACK_ROWS)]
        fb = apply_filters(
            fb,
            min_rating=min_rating,
            max_rating=max_rating,
            title_contains=title_contains,
            min_votes=min_votes,
            min_year=min_year,
            max_year=max_year,
            tag_contains=tag_contains,
        )
        fb = finalize_ranks(fb)
        fb = fb[:limit]
        return fb, f"fallback:{ex}"


def run_crawl_and_store(
    *,
    source: str = "chart",
    limit: int = 20,
    min_rating: Optional[float] = None,
    max_rating: Optional[float] = None,
    title_contains: Optional[str] = None,
    min_votes: Optional[int] = None,
    min_year: Optional[int] = None,
    max_year: Optional[int] = None,
    tag_contains: Optional[str] = None,
    prefetch_ratio: float = 3.0,
    explore_sort: str = "U",
    explore_tag: str = "",
    explore_range: str = "0,10",
    explore_year_range: str = "",
    explore_max_pages: int = 50,
    search_query: str = "",
    search_max_pages: int = 30,
    search_start_offset: int = 0,
    explore_start_offset: int = 0,
    top250_start: int = 0,
    chart_mode: str = "new",
    progress_callback: Optional[Callable[[str, int, int, str], None]] = None,
):
    """
    入库。prefetch_ratio：先抓取约 limit * ratio 条再筛选（Top250 / 探索 / 搜索 需要缓冲）。
    search：豆瓣「电影」关键词搜索结果页（window.__DATA__）。
    explore：选电影 JSON 接口分页。
    chart_mode：仅 chart 源；new 新片榜、weekly 一周口碑榜、na_boxoffice 北美票房榜（同页解析）。
    """
    import pymysql

    source = (source or "chart").strip().lower()
    if source not in ("chart", "top250", "explore", "search"):
        source = "chart"
    cm = (chart_mode or "new").strip().lower()
    if cm not in ("new", "weekly", "na_boxoffice"):
        cm = "new"
    chart_mode = cm

    limit = int(limit)
    limit = max(1, min(limit, ABS_MAX_LIMIT))

    explore_opts = None
    search_opts = None

    if source == "search":
        cap_pages = max(1, min(int(search_max_pages), SEARCH_MAX_PAGES_HARD))
        prefetch_cap = min(
            ABS_MAX_LIMIT,
            cap_pages * SEARCH_PAGE_SIZE,
            max(limit, int(limit * prefetch_ratio)),
        )
        search_opts = {
            "query": search_query.strip(),
            "max_pages": cap_pages,
            "start_offset": max(0, min(int(search_start_offset), 50000)),
        }
    elif source == "explore":
        cap_pages = max(1, min(int(explore_max_pages), EXPLORE_MAX_PAGES_HARD))
        prefetch_cap = min(
            ABS_MAX_LIMIT,
            cap_pages * EXPLORE_PAGE_SIZE,
            max(limit, int(limit * prefetch_ratio)),
        )
        explore_opts = {
            "tag": explore_tag,
            "sort": explore_sort,
            "range": explore_range,
            "year_range": explore_year_range,
            "max_pages": cap_pages,
            "start_offset": max(0, int(explore_start_offset)),
        }
    else:
        prefetch_cap = min(ABS_MAX_LIMIT, max(limit, int(limit * prefetch_ratio)))

    if progress_callback:
        progress_callback("list", 0, 1, "正在获取候选列表…")
    t250 = max(0, min(int(top250_start), 249))
    t250 = (t250 // 25) * 25

    rows, src = fetch_rows_with_fallback(
        source,
        prefetch_cap,
        limit,
        min_rating,
        max_rating,
        title_contains,
        min_votes,
        min_year,
        max_year,
        tag_contains,
        explore_opts=explore_opts,
        search_opts=search_opts,
        top250_start=t250,
        chart_mode=chart_mode,
    )
    if progress_callback:
        progress_callback(
            "list",
            1,
            1,
            f"候选 {len(rows)} 条，开始补充详情并入库…",
        )

    if source == "chart":
        source_label = chart_source_label(chart_mode)
    elif source == "top250":
        source_label = TOP250_BASE
    elif source == "search":
        source_label = SEARCH_SUBJECT_SEARCH
    else:
        source_label = EXPLORE_JSON_API

    now = datetime.now()
    conn = pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    inserted = 0
    try:
        with conn.cursor() as cur:
            nrows = len(rows)
            for idx, row in enumerate(rows):
                if progress_callback:
                    title_hint = (row.get("title") or "")[:120]
                    progress_callback("enrich", idx + 1, nrows, title_hint or row.get("subject_id", ""))
                row = enrich_row_for_store(row)
                cur.execute(
                    """
                    INSERT INTO douban_chart_movies
                    (subject_id, title, release_year, region, tags, rating, votes,
                     chart_rank, page_url, poster_url, poster_local, summary, directors,
                     casts, aliases, duration, imdb_id, source_label, crawled_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                      title=VALUES(title),
                      release_year=VALUES(release_year),
                      region=VALUES(region),
                      tags=VALUES(tags),
                      rating=VALUES(rating),
                      votes=VALUES(votes),
                      chart_rank=VALUES(chart_rank),
                      page_url=VALUES(page_url),
                      poster_url=VALUES(poster_url),
                      poster_local=VALUES(poster_local),
                      summary=VALUES(summary),
                      directors=VALUES(directors),
                      casts=VALUES(casts),
                      aliases=VALUES(aliases),
                      duration=VALUES(duration),
                      imdb_id=VALUES(imdb_id),
                      crawled_at=VALUES(crawled_at),
                      source_label=VALUES(source_label)
                    """,
                    (
                        row["subject_id"],
                        row["title"],
                        row.get("release_year"),
                        row.get("region"),
                        row.get("tags") or "",
                        row.get("rating"),
                        row.get("votes"),
                        row.get("chart_rank"),
                        row.get("page_url") or "",
                        row.get("poster_url"),
                        row.get("poster_local"),
                        row.get("summary"),
                        row.get("directors"),
                        row.get("casts"),
                        row.get("aliases"),
                        row.get("duration"),
                        row.get("imdb_id"),
                        source_label,
                        now,
                    ),
                )
                inserted += 1
            summary = (
                f"{src}; source={source}; limit={limit}; "
                f"min_r={min_rating}; max_r={max_rating}; "
                f"title~={title_contains!s}; min_votes={min_votes}; "
                f"y={min_year}-{max_year}; tag~={tag_contains!s}; "
                f"explore={explore_sort}/{explore_tag!s}/{explore_range}/pages={explore_max_pages}/start={explore_start_offset}; "
                f"search_q={search_query!s}/pages={search_max_pages}/start={search_start_offset}; "
                f"t250_start={t250}; "
                f"chart_mode={chart_mode}; "
                f"rows_out={len(rows)}"
            )
            cur.execute(
                "INSERT INTO crawl_log (rows_inserted, message) VALUES (%s,%s)",
                (inserted, summary[:512]),
            )
        conn.commit()
    finally:
        conn.close()
    return inserted, src, len(rows)
