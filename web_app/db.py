"""MySQL：豆瓣榜单影片（含年份、地区、类型标签）。"""
from collections import Counter
from pathlib import Path
from typing import List, Optional, Tuple

import pymysql
from pymysql.err import OperationalError

from web_app.config import MYSQL_DB, MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, MYSQL_USER

_POSTERS_DIR = Path(__file__).resolve().parent / "static" / "posters"


def get_conn():
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def init_tables():
    sqls = [
        """
        CREATE TABLE IF NOT EXISTS douban_chart_movies (
          id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
          subject_id VARCHAR(32) NOT NULL,
          title VARCHAR(512) NOT NULL,
          release_year SMALLINT UNSIGNED DEFAULT NULL COMMENT '上映年份',
          region VARCHAR(64) DEFAULT NULL COMMENT '国家/地区',
          tags VARCHAR(512) DEFAULT NULL COMMENT '类型标签，英文逗号分隔',
          rating DECIMAL(4,1) DEFAULT NULL,
          votes INT DEFAULT NULL,
          chart_rank SMALLINT UNSIGNED DEFAULT NULL,
          page_url VARCHAR(512) DEFAULT '',
          poster_url VARCHAR(512) DEFAULT NULL COMMENT '海报图 URL',
          poster_local VARCHAR(512) DEFAULT NULL COMMENT '本地海报相对 static 的路径',
          summary TEXT NULL COMMENT '热门短评摘录（多条拼接）',
          directors VARCHAR(512) DEFAULT NULL,
          casts VARCHAR(768) DEFAULT NULL,
          aliases VARCHAR(512) DEFAULT NULL,
          duration VARCHAR(64) DEFAULT NULL,
          imdb_id VARCHAR(16) DEFAULT NULL,
          source_label VARCHAR(128) DEFAULT '',
          crawled_at DATETIME NOT NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          UNIQUE KEY uk_subject (subject_id),
          KEY idx_crawled (crawled_at),
          KEY idx_year (release_year),
          KEY idx_region (region),
          KEY idx_rating (rating)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS crawl_log (
          id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
          ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          rows_inserted INT DEFAULT 0,
          message VARCHAR(512) DEFAULT ''
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
    ]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for s in sqls:
                cur.execute(s)
        conn.commit()
    finally:
        conn.close()


def ensure_extra_columns():
    """旧库升级：补充缺失列。"""
    alters = [
        "ADD COLUMN release_year SMALLINT UNSIGNED NULL COMMENT '上映年份' AFTER title",
        "ADD COLUMN region VARCHAR(64) NULL COMMENT '国家/地区' AFTER release_year",
        "ADD COLUMN tags VARCHAR(512) NULL COMMENT '类型标签' AFTER region",
        "ADD COLUMN poster_url VARCHAR(512) NULL COMMENT '海报 URL' AFTER page_url",
        "ADD COLUMN poster_local VARCHAR(512) NULL COMMENT '本地海报路径'",
        "ADD COLUMN summary TEXT NULL COMMENT '热门短评摘录（多条拼接）'",
        "ADD COLUMN directors VARCHAR(512) NULL",
        "ADD COLUMN casts VARCHAR(768) NULL",
        "ADD COLUMN aliases VARCHAR(512) NULL",
        "ADD COLUMN duration VARCHAR(64) NULL",
        "ADD COLUMN imdb_id VARCHAR(16) NULL",
    ]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for ddl in alters:
                try:
                    cur.execute(f"ALTER TABLE douban_chart_movies {ddl}")
                    conn.commit()
                except OperationalError as e:
                    if e.args[0] != 1060:
                        raise
    finally:
        conn.close()


def ensure_crawl_log_message_width():
    """旧库升级：抓取摘要可能较长，将 crawl_log.message 扩至 VARCHAR(512)。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE crawl_log MODIFY COLUMN message VARCHAR(512) DEFAULT ''"
            )
        conn.commit()
    except OperationalError:
        conn.rollback()
    finally:
        conn.close()


def fetch_movies(limit=400):
    return fetch_movies_filtered(limit=limit)


# 影片库排序：仅允许这些列进入 ORDER BY，避免拼接注入
_MOVIE_LIST_SORT_COLS = {
    "rank": "chart_rank",
    "year": "release_year",
    "rating": "rating",
    "votes": "votes",
    "title": "title",
}


def fetch_movies_filtered(
    limit=400,
    year=None,
    region=None,
    tag=None,
    min_rating=None,
    search=None,
    sort_key="rank",
    sort_order="asc",
):
    """列表筛选：年份、地区、标签（子串匹配 tags）、最低评分、标题搜索；支持排序。
    limit 为 None 时不加 SQL LIMIT（仅供导出等场景，慎用）。"""
    conds = []
    params = []
    if year:
        conds.append("release_year = %s")
        params.append(int(year))
    if region:
        conds.append("region = %s")
        params.append(region)
    if tag:
        conds.append("tags LIKE %s")
        params.append(f"%{tag}%")
    if min_rating is not None:
        conds.append("rating >= %s")
        params.append(float(min_rating))
    if search:
        conds.append("title LIKE %s")
        params.append(f"%{search}%")

    where_sql = " AND ".join(conds) if conds else "1"
    sk = (sort_key or "rank").strip().lower()
    if sk not in _MOVIE_LIST_SORT_COLS:
        sk = "rank"
    so = (sort_order or "asc").strip().lower()
    if so not in ("asc", "desc"):
        so = "asc"
    order_col = _MOVIE_LIST_SORT_COLS[sk]
    order_dir = so.upper()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if limit is None:
                cur.execute(
                    f"""
                    SELECT id, subject_id, title, release_year, region, tags, rating, votes,
                           chart_rank, page_url, poster_url, poster_local, summary, directors,
                           casts, aliases, duration, imdb_id, source_label, crawled_at
                    FROM douban_chart_movies
                    WHERE {where_sql}
                    ORDER BY {order_col} {order_dir}, id DESC
                    """,
                    tuple(params),
                )
            else:
                lim = max(1, min(int(limit), 50000))
                cur.execute(
                    f"""
                    SELECT id, subject_id, title, release_year, region, tags, rating, votes,
                           chart_rank, page_url, poster_url, poster_local, summary, directors,
                           casts, aliases, duration, imdb_id, source_label, crawled_at
                    FROM douban_chart_movies
                    WHERE {where_sql}
                    ORDER BY {order_col} {order_dir}, id DESC
                    LIMIT %s
                    """,
                    tuple(params) + (lim,),
                )
            return cur.fetchall()
    finally:
        conn.close()


def distinct_years():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT release_year AS y
                FROM douban_chart_movies
                WHERE release_year IS NOT NULL
                ORDER BY y DESC
                """
            )
            return [r["y"] for r in cur.fetchall()]
    finally:
        conn.close()


def distinct_regions():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT region AS r
                FROM douban_chart_movies
                WHERE region IS NOT NULL AND region <> ''
                ORDER BY r ASC
                """
            )
            return [row["r"] for row in cur.fetchall()]
    finally:
        conn.close()


def distinct_tags_split():
    """从 tags 字段拆出所有出现过的标签。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tags FROM douban_chart_movies WHERE tags IS NOT NULL AND tags <> ''"
            )
            seen = set()
            for row in cur.fetchall():
                for part in (row["tags"] or "").split(","):
                    t = part.strip()
                    if t:
                        seen.add(t)
            return sorted(seen)
    finally:
        conn.close()


def fetch_latest_for_home(limit=8):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT subject_id, title, rating, votes, chart_rank, crawled_at,
                       release_year, region, tags, poster_url, poster_local
                FROM douban_chart_movies
                ORDER BY crawled_at DESC, chart_rank ASC
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def count_movies():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM douban_chart_movies")
            return cur.fetchone()["c"]
    finally:
        conn.close()


def fetch_latest_crawl_info():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ran_at, rows_inserted, message FROM crawl_log ORDER BY id DESC LIMIT 1"
            )
            return cur.fetchone()
    finally:
        conn.close()


def _stats_filter_where(
    filter_tags: Optional[List[str]] = None,
    filter_years: Optional[List[int]] = None,
) -> Tuple[str, list]:
    """
    与数据分析页筛选一致。
    - 多个标签：OR（tags 命中任一关键词即入选）。
    - 多个年份：IN（上映年份为所选年份之一）。
    """
    conds = []
    params: list = []
    if filter_tags:
        seen: List[str] = []
        for t in filter_tags:
            ts = str(t).strip()
            if ts and ts not in seen:
                seen.append(ts)
        if seen:
            parts = ["tags LIKE %s" for _ in seen]
            params.extend(f"%{t}%" for t in seen)
            conds.append("(" + " OR ".join(parts) + ")")
    if filter_years:
        ys = sorted(set(int(y) for y in filter_years))
        ph = ",".join(["%s"] * len(ys))
        conds.append(f"release_year IN ({ph})")
        params.extend(ys)
    where_sql = " AND ".join(conds) if conds else "1"
    return where_sql, params


def aggregate_chart_ratings(filter_tags=None, filter_years=None):
    """柱状图：可按标签、年份缩小范围（支持多选）。"""
    where_sql, params = _stats_filter_where(filter_tags, filter_years)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT title, rating, chart_rank
                FROM douban_chart_movies
                WHERE rating IS NOT NULL AND ({where_sql})
                ORDER BY chart_rank ASC
                LIMIT 15
                """,
                tuple(params),
            )
            return cur.fetchall()
    finally:
        conn.close()


def analytics_kpis(filter_tags=None, filter_years=None):
    """样本量、均值、年份跨度（用于统计看板）。"""
    where_sql, params = _stats_filter_where(filter_tags, filter_years)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                  COUNT(*) AS n,
                  AVG(rating) AS avg_rating,
                  AVG(votes) AS avg_votes,
                  MIN(release_year) AS year_min,
                  MAX(release_year) AS year_max,
                  SUM(CASE WHEN rating IS NOT NULL THEN 1 ELSE 0 END) AS n_rated,
                  SUM(CASE WHEN votes IS NOT NULL THEN 1 ELSE 0 END) AS n_with_votes
                FROM douban_chart_movies
                WHERE ({where_sql})
                """,
                tuple(params),
            )
            return cur.fetchone()
    finally:
        conn.close()


def fetch_ratings_for_median(filter_tags=None, filter_years=None):
    """有序评分列表，用于中位数。"""
    where_sql, params = _stats_filter_where(filter_tags, filter_years)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT rating
                FROM douban_chart_movies
                WHERE rating IS NOT NULL AND ({where_sql})
                ORDER BY rating ASC
                """,
                tuple(params),
            )
            return [float(r["rating"]) for r in cur.fetchall()]
    finally:
        conn.close()


def stats_count_by_release_year(filter_tags=None, filter_years=None):
    """按上映年份计数。"""
    where_sql, params = _stats_filter_where(filter_tags, filter_years)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT release_year AS y, COUNT(*) AS c
                FROM douban_chart_movies
                WHERE release_year IS NOT NULL AND ({where_sql})
                GROUP BY release_year
                ORDER BY release_year ASC
                """,
                tuple(params),
            )
            return cur.fetchall()
    finally:
        conn.close()


def stats_top_regions(filter_tags=None, filter_years=None, limit=10):
    """制片地区 Top N。"""
    where_sql, params = _stats_filter_where(filter_tags, filter_years)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT region AS r, COUNT(*) AS c
                FROM douban_chart_movies
                WHERE region IS NOT NULL AND TRIM(region) <> ''
                  AND ({where_sql})
                GROUP BY region
                ORDER BY c DESC
                LIMIT %s
                """,
                tuple(params) + (limit,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def stats_top_tags(filter_tags=None, filter_years=None, top_n=15):
    """从 tags 逗号字段拆分后的标签频次 Top N。"""
    where_sql, params = _stats_filter_where(filter_tags, filter_years)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT tags
                FROM douban_chart_movies
                WHERE tags IS NOT NULL AND TRIM(tags) <> ''
                  AND ({where_sql})
                """,
                tuple(params),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    cnt: Counter = Counter()
    for row in rows:
        for part in (row["tags"] or "").split(","):
            t = part.strip()
            if t:
                cnt[t] += 1
    return cnt.most_common(top_n)


def fetch_for_analysis_export(filter_tags=None, filter_years=None, limit=50000):
    """导出 CSV：与统计筛选一致，含便于分析的字段。"""
    where_sql, params = _stats_filter_where(filter_tags, filter_years)
    lim = max(1, min(int(limit), 100000))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT subject_id, title, release_year, region, tags,
                       rating, votes, directors, casts, duration, imdb_id,
                       chart_rank, crawled_at, source_label
                FROM douban_chart_movies
                WHERE ({where_sql})
                ORDER BY chart_rank ASC, id DESC
                LIMIT %s
                """,
                tuple(params) + (lim,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def fetch_rating_votes_pairs(filter_tags=None, filter_years=None, limit=4000):
    """用于散点图：同时有评分与评价人数的 (rating, votes) 行。"""
    where_sql, params = _stats_filter_where(filter_tags, filter_years)
    lim = max(10, min(int(limit), 20000))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT rating, votes
                FROM douban_chart_movies
                WHERE rating IS NOT NULL AND votes IS NOT NULL AND ({where_sql})
                ORDER BY id DESC
                LIMIT %s
                """,
                tuple(params) + (lim,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def stats_count_by_source_label(filter_tags=None, filter_years=None):
    """按抓取来源 URL（source_label）分组计数。"""
    where_sql, params = _stats_filter_where(filter_tags, filter_years)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COALESCE(NULLIF(TRIM(source_label), ''), '(未标注)') AS src, COUNT(*) AS c
                FROM douban_chart_movies
                WHERE ({where_sql})
                GROUP BY src
                ORDER BY c DESC
                """,
                tuple(params),
            )
            return cur.fetchall()
    finally:
        conn.close()


def fetch_numeric_xy_pairs(
    x_key: str,
    y_key: str,
    filter_tags=None,
    filter_years=None,
    limit=6000,
):
    """
    自选横纵轴的数值散点数据。键仅限 rating / votes / release_year / chart_rank（防 SQL 注入）。
    """
    numeric_axis_sql = {
        "rating": "rating",
        "votes": "votes",
        "release_year": "release_year",
        "chart_rank": "chart_rank",
    }
    x_key = (x_key or "").strip().lower()
    y_key = (y_key or "").strip().lower()
    if x_key not in numeric_axis_sql or y_key not in numeric_axis_sql:
        return []
    if x_key == y_key:
        return []
    xc = numeric_axis_sql[x_key]
    yc = numeric_axis_sql[y_key]
    where_sql, params = _stats_filter_where(filter_tags, filter_years)
    lim = max(10, min(int(limit), 25000))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {xc} AS xv, {yc} AS yv
                FROM douban_chart_movies
                WHERE {xc} IS NOT NULL AND {yc} IS NOT NULL AND ({where_sql})
                ORDER BY id DESC
                LIMIT %s
                """,
                tuple(params) + (lim,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def votes_rating_correlation(filter_tags=None, filter_years=None):
    """返回 (n, pearson_r)；样本过少时 r 为 None。用于分析报告。"""
    where_sql, params = _stats_filter_where(filter_tags, filter_years)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT rating, votes
                FROM douban_chart_movies
                WHERE rating IS NOT NULL AND votes IS NOT NULL AND ({where_sql})
                """,
                tuple(params),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    if len(rows) < 5:
        return len(rows), None
    xs = [float(r["rating"]) for r in rows]
    ys = [float(r["votes"]) for r in rows]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(len(xs)))
    denx = sum((x - mx) ** 2 for x in xs) ** 0.5
    deny = sum((y - my) ** 2 for y in ys) ** 0.5
    if denx < 1e-9 or deny < 1e-9:
        return len(rows), None
    r = num / (denx * deny)
    return len(rows), round(r, 4)


def fetch_movie_by_subject(subject_id: str):
    """单条条目（详情页）。"""
    if not subject_id:
        return None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, subject_id, title, release_year, region, tags, rating, votes,
                       chart_rank, page_url, poster_url, poster_local, summary, directors,
                       casts, aliases, duration, imdb_id, source_label, crawled_at
                FROM douban_chart_movies
                WHERE subject_id = %s
                LIMIT 1
                """,
                (subject_id.strip(),),
            )
            return cur.fetchone()
    finally:
        conn.close()


def delete_movie_by_subject(subject_id: str) -> int:
    """按豆瓣 subject_id 删除一条；返回删除行数（0 或 1）。顺带删除本地海报文件。"""
    sid = (subject_id or "").strip()
    if not sid or not sid.isdigit() or len(sid) > 24:
        return 0
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT poster_local FROM douban_chart_movies WHERE subject_id = %s LIMIT 1",
                (sid,),
            )
            row = cur.fetchone()
            pl = (row or {}).get("poster_local") if row else None
            cur.execute("DELETE FROM douban_chart_movies WHERE subject_id = %s", (sid,))
            n = cur.rowcount
        conn.commit()
        if pl and isinstance(pl, str) and pl.strip():
            rel = pl.replace("\\", "/").strip().lstrip("/")
            if rel.startswith("posters/"):
                try:
                    fp = (_POSTERS_DIR.parent / rel).resolve()
                    if (
                        fp.is_file()
                        and fp.parent.resolve() == _POSTERS_DIR.resolve()
                    ):
                        fp.unlink()
                except OSError:
                    pass
        return int(n)
    finally:
        conn.close()


def clear_library_data():
    """清空影片表与抓取日志（不可恢复）；删除本地已下载封面文件。"""
    if _POSTERS_DIR.is_dir():
        for p in _POSTERS_DIR.iterdir():
            if p.is_file():
                try:
                    p.unlink()
                except OSError:
                    pass
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE crawl_log")
            cur.execute("TRUNCATE TABLE douban_chart_movies")
        conn.commit()
    finally:
        conn.close()
