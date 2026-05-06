"""
Web 版：Flask + MySQL + 豆瓣榜单抓取 + 统计图。
运行前：创建数据库 movie_douban（或改 config），保证账号可连。
启动：在项目根目录  python -m web_app.app
"""
import base64
import csv
import io
import statistics
import threading
import uuid
from datetime import datetime
from typing import List, Optional, Tuple
from pathlib import Path
from urllib.parse import urlencode

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.ticker as mticker
import matplotlib.pyplot as plt
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from web_app import crawler
from web_app.db import (
    aggregate_chart_ratings,
    analytics_kpis,
    clear_library_data,
    count_movies,
    delete_movie_by_subject,
    distinct_regions,
    distinct_tags_split,
    distinct_years,
    ensure_crawl_log_message_width,
    ensure_extra_columns,
    fetch_for_analysis_export,
    fetch_latest_for_home,
    fetch_movie_by_subject,
    fetch_movies_filtered,
    fetch_numeric_xy_pairs,
    fetch_rating_votes_pairs,
    fetch_ratings_for_median,
    init_tables,
    stats_count_by_release_year,
    stats_count_by_source_label,
    stats_top_regions,
    stats_top_tags,
    votes_rating_correlation,
)

_BASE = Path(__file__).resolve().parent
app = Flask(
    __name__,
    template_folder=str(_BASE / "templates"),
    static_folder=str(_BASE / "static"),
)

# 异步抓取任务进度（进程内内存；单 worker 场景下足够）
_crawl_jobs: dict = {}
_crawl_jobs_lock = threading.Lock()


@app.template_filter("fmt_num")
def _fmt_num(n):
    if n is None:
        return ""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


@app.before_request
def _ensure_tables():
    if not getattr(app, "_db_ready", False):
        try:
            init_tables()
            ensure_extra_columns()
            ensure_crawl_log_message_width()
            app._db_ready = True
        except Exception:
            app._db_ready = False


@app.route("/")
def index():
    err = None
    stats = {"records": 0}
    rows = []
    try:
        stats["records"] = count_movies()
        rows = fetch_latest_for_home(8)
    except Exception as e:
        err = str(e)
    show_cleared = request.args.get("cleared") == "1"
    return render_template(
        "index.html",
        db_error=err,
        stats=stats,
        sample_rows=rows,
        show_cleared=show_cleared,
    )


@app.route("/movies")
def movies():
    err = None
    rows = []
    choices = {"years": [], "regions": [], "tags": []}
    year = request.args.get("year", type=int)
    region = (request.args.get("region") or "").strip() or None
    tag = (request.args.get("tag") or "").strip() or None
    min_rating = _opt_float("min_rating")
    search = (request.args.get("q") or "").strip() or None
    sort_key = (request.args.get("sort") or "rank").strip().lower()
    if sort_key not in ("rank", "year", "rating", "votes", "title"):
        sort_key = "rank"
    sort_order = (request.args.get("order") or "asc").strip().lower()
    if sort_order not in ("asc", "desc"):
        sort_order = "asc"
    try:
        rows = fetch_movies_filtered(
            limit=400,
            year=year,
            region=region,
            tag=tag,
            min_rating=min_rating,
            search=search,
            sort_key=sort_key,
            sort_order=sort_order,
        )
        choices["years"] = distinct_years()
        choices["regions"] = distinct_regions()
        choices["tags"] = distinct_tags_split()
    except Exception as e:
        err = str(e)
    qs = request.query_string
    qs_s = qs.decode("utf-8") if isinstance(qs, (bytes, bytearray)) else (qs or "")
    export_csv_href = url_for("movies_export_csv")
    if qs_s:
        export_csv_href = export_csv_href + "?" + qs_s
    return render_template(
        "movies.html",
        rows=rows,
        db_error=err,
        choices=choices,
        filter_year=year,
        filter_region=region,
        filter_tag=tag,
        filter_min_rating=request.args.get("min_rating") or "",
        filter_q=request.args.get("q") or "",
        filter_sort=sort_key,
        filter_order=sort_order,
        export_csv_href=export_csv_href,
    )


@app.route("/movies/export.csv")
def movies_export_csv():
    """按影片库当前筛选条件导出 CSV（列与库表字段对齐，便于离线分析）。"""
    year = request.args.get("year", type=int)
    region = (request.args.get("region") or "").strip() or None
    tag = (request.args.get("tag") or "").strip() or None
    min_rating = _opt_float("min_rating")
    search = (request.args.get("q") or "").strip() or None
    sort_key = (request.args.get("sort") or "rank").strip().lower()
    if sort_key not in ("rank", "year", "rating", "votes", "title"):
        sort_key = "rank"
    sort_order = (request.args.get("order") or "asc").strip().lower()
    if sort_order not in ("asc", "desc"):
        sort_order = "asc"
    try:
        rows = fetch_movies_filtered(
            limit=None,
            year=year,
            region=region,
            tag=tag,
            min_rating=min_rating,
            search=search,
            sort_key=sort_key,
            sort_order=sort_order,
        )
    except Exception as e:
        return Response(
            f"导出失败：{e}",
            status=500,
            mimetype="text/plain; charset=utf-8",
        )

    header = [
        "subject_id",
        "title",
        "release_year",
        "region",
        "tags",
        "rating",
        "votes",
        "chart_rank",
        "page_url",
        "poster_url",
        "poster_local",
        "directors",
        "casts",
        "aliases",
        "duration",
        "imdb_id",
        "source_label",
        "crawled_at",
        "summary",
    ]

    def esc_cell(r, key):
        v = r.get(key)
        if v is None:
            return ""
        if key == "crawled_at" and hasattr(v, "strftime"):
            return v.strftime("%Y-%m-%d %H:%M:%S")
        s = str(v)
        if key == "summary" and len(s) > 32000:
            return s[:32000]
        return s

    si = io.StringIO()
    w = csv.writer(si)
    w.writerow(header)
    for r in rows:
        w.writerow([esc_cell(r, k) for k in header])

    payload = "\ufeff" + si.getvalue()
    fname = f"movies_library_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename={fname}",
            "Cache-Control": "no-store",
        },
    )


@app.route("/movie/<subject_id>")
def movie_detail(subject_id):
    err = None
    row = None
    try:
        row = fetch_movie_by_subject(subject_id)
    except Exception as e:
        err = str(e)
    if err:
        return render_template("movie_detail.html", db_error=err, r=None)
    if not row:
        abort(404)
    return render_template("movie_detail.html", db_error=None, r=row)


@app.route("/movie/<subject_id>/delete", methods=["POST"])
def movie_delete(subject_id):
    """从库中删除一条影片（仅本地 MySQL，不影响豆瓣）。"""
    sid = (subject_id or "").strip()
    if not sid.isdigit() or len(sid) > 24:
        abort(400)
    raw_next = (request.form.get("next") or "").strip()
    if raw_next.startswith("/") and not raw_next.startswith("//"):
        dest = raw_next
    else:
        dest = url_for("movies")
    try:
        delete_movie_by_subject(sid)
    except Exception:
        pass
    return redirect(dest)


def _parse_stats_filters():
    """从 GET 解析多年份、多标签；全不选表示不设条件（全库）。"""
    ys = []
    for x in request.args.getlist("year"):
        xs = str(x).strip()
        if xs.isdigit():
            ys.append(int(xs))
    filter_years = sorted(set(ys)) if ys else None

    ts = []
    for t in request.args.getlist("tag"):
        t = (t or "").strip()
        if t:
            ts.append(t)
    filter_tags = list(dict.fromkeys(ts)) if ts else None
    return filter_tags, filter_years


def _rating_kde_density(ratings):
    """
    一维高斯核密度估计（连续曲线），不预先分档。
    bandwidth：Silverman 经验规则，样本过少或方差为 0 时退化处理。
    """
    v = np.asarray(ratings, dtype=float)
    if v.size == 0:
        return None, None
    lo = max(0.0, float(np.min(v)) - 0.35)
    hi = min(10.0, float(np.max(v)) + 0.35)
    if hi <= lo:
        hi = lo + 0.2
    x_grid = np.linspace(lo, hi, 320)
    n = int(v.size)
    if n == 1:
        bw = 0.22
        dens = np.exp(-0.5 * ((x_grid - v[0]) / bw) ** 2) / (bw * np.sqrt(2 * np.pi))
        return x_grid, dens
    std = float(np.std(v, ddof=1))
    if std < 1e-9:
        std = 0.18
    bw = 1.06 * std * (n ** (-0.2))
    bw = float(max(min(bw, 0.48), 0.07))
    diff = (x_grid[:, None] - v[None, :]) / bw
    dens = np.exp(-0.5 * diff**2).sum(axis=1) / (n * bw * np.sqrt(2 * np.pi))
    return x_grid, dens


def _build_stats_dashboard_figure(filter_tags, filter_years, ratings_precomputed=None):
    """四宫格：评分 KDE、年份分布、Top 影片、地区 Top。"""
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    ratings_sample = (
        ratings_precomputed
        if ratings_precomputed is not None
        else fetch_ratings_for_median(filter_tags, filter_years)
    )
    year_rows = stats_count_by_release_year(filter_tags, filter_years)
    agg = aggregate_chart_ratings(filter_tags, filter_years)
    reg_rows = stats_top_regions(filter_tags, filter_years, 10)

    fig, axes = plt.subplots(2, 2, figsize=(13, 9), dpi=100)
    ax00, ax01, ax10, ax11 = axes[0, 0], axes[0, 1], axes[1, 0], axes[1, 1]
    subtitle = []
    if filter_years:
        subtitle.append("年份:" + "、".join(str(y) for y in filter_years))
    if filter_tags:
        shown = filter_tags[:8]
        extra = len(filter_tags) - len(shown)
        tag_txt = "、".join(shown) + ("…" if extra > 0 else "")
        subtitle.append(f"标签(OR):「{tag_txt}」")
    supt = "库内数据分析" + (" · " + " ".join(subtitle) if subtitle else "")
    fig.suptitle(supt, fontsize=13, y=1.02)

    # 评分连续分布（核密度）
    x_kde, y_kde = _rating_kde_density(ratings_sample)
    if x_kde is not None and y_kde is not None:
        ax00.fill_between(x_kde, y_kde, alpha=0.38, color="#2a9d8f")
        ax00.plot(x_kde, y_kde, color="#1d7065", linewidth=2.0)
        ax00.set_xlabel("评分（0～10）")
        ax00.set_ylabel("概率密度")
        ax00.set_title("评分分布（核密度 KDE）")
        ax00.set_xlim(0, 10)
        ax00.set_ylim(bottom=0)
    else:
        ax00.text(0.5, 0.5, "无评分数据", ha="center", va="center", transform=ax00.transAxes)

    # 上映年份（柱仍为逐年，横轴刻度仅每 10 年显示，避免标签挤在一起）
    if year_rows:
        xi = [int(r["y"]) for r in year_rows]
        yy = [int(r["c"]) for r in year_rows]
        ax01.bar(
            xi,
            yy,
            width=0.82,
            align="center",
            color="#e9c46a",
            edgecolor="#c77d2a",
            linewidth=0.5,
        )
        ax01.set_xlabel("上映年份")
        ax01.set_ylabel("部数")
        ax01.set_title("年份分布")
        ax01.xaxis.set_major_locator(mticker.MultipleLocator(10))
        ax01.xaxis.set_minor_locator(mticker.NullLocator())
        ax01.set_xlim(min(xi) - 1.0, max(xi) + 1.0)
        plt.setp(ax01.xaxis.get_majorticklabels(), rotation=0, ha="center")
    else:
        ax01.text(0.5, 0.5, "无年份字段", ha="center", va="center", transform=ax01.transAxes)

    # Top15 条目标分
    if agg:
        names = []
        for a in agg:
            t = (a["title"] or "")[:12]
            if len(a["title"] or "") > 12:
                t += "…"
            names.append(t)
        vals = [float(a["rating"] or 0) for a in agg]
        ax10.bar(names, vals, color="#264653")
        ax10.set_ylabel("评分")
        ax10.set_title("榜单序号靠前 · 评分 Top15")
        plt.setp(ax10.xaxis.get_majorticklabels(), rotation=30, ha="right")
    else:
        ax10.text(0.5, 0.5, "无条目", ha="center", va="center", transform=ax10.transAxes)

    # 地区
    if reg_rows:
        labels = [(r["r"] or "")[:10] for r in reg_rows]
        counts = [int(r["c"]) for r in reg_rows]
        ax11.barh(labels[::-1], counts[::-1], color="#f4a261")
        ax11.set_xlabel("部数")
        ax11.set_title("制片地区 Top10")
    else:
        ax11.text(0.5, 0.5, "无地区字段", ha="center", va="center", transform=ax11.transAxes)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _figure_to_b64_single(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _short_source_display(raw: str) -> str:
    s = (raw or "").strip()
    if not s or s == "(未标注)":
        return "未标注"
    if "movie.douban.com/chart" in s:
        if "一周口碑" in s:
            return "排行榜·一周口碑"
        if "北美票房" in s:
            return "排行榜·北美票房"
        if "新片" in s:
            return "排行榜·新片榜"
        return "豆瓣排行榜"
    if "top250" in s:
        return "Top250"
    if "subject_search" in s or "search.douban" in s:
        return "关键词搜索"
    if "new_search_subjects" in s or "j/new_search" in s:
        return "探索 / JSON 接口"
    if len(s) > 36:
        return s[:34] + "…"
    return s


def _build_viz_scatter_figure(pairs, pearson_r, n_all: int):
    """评分 × 评价人数：对人数取对数刻度便于观察。"""
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(9.2, 6.2), dpi=100)
    if not pairs:
        ax.text(0.5, 0.5, "无同时具备评分与评价人数的记录", ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
        return _figure_to_b64_single(fig)
    xs = [float(p["rating"]) for p in pairs]
    ys = [max(1.0, float(p["votes"])) for p in pairs]
    ax.scatter(xs, ys, alpha=0.38, s=32, c="#3a86ff", edgecolors="none", zorder=2)
    ax.set_yscale("log")
    ax.set_xlabel("评分", fontsize=11)
    ax.set_ylabel("评价人数（对数轴）", fontsize=11)
    ax.set_title("探索性分析 · 评分与热度（人数）", fontsize=13, pad=12)
    ax.set_xlim(0, 10)
    ax.grid(True, alpha=0.28, linestyle="--")
    note = f"散点条数：{len(pairs)}（至多取样近期入库记录）"
    if pearson_r is not None:
        note += f" · 全样本 Pearson r（评分×人数）≈ {pearson_r:.3f}"
    fig.text(0.5, 0.02, note, ha="center", fontsize=9.5, color="#445566")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    return _figure_to_b64_single(fig)


def _extended_chart_colors(n: int) -> list:
    """饼图等分类多时拼接 tab20/tab20b/Set3，避免颜色循环过快重复。"""
    if n <= 0:
        return []
    out = []
    for name in ("tab20", "tab20b", "Set3"):
        try:
            cmap = plt.colormaps[name]
        except (AttributeError, KeyError, TypeError):
            cmap = matplotlib.cm.get_cmap(name)
        ncol = cmap.N
        for i in range(ncol):
            out.append(mcolors.to_hex(cmap(i / max(ncol - 1, 1))))
            if len(out) >= n:
                return out[:n]
    base = out or ["#888888"]
    while len(out) < n:
        out.append(base[len(out) % len(base)])
    return out[:n]


def _build_viz_sources_figure(rows):
    """数据来源 URL（抓取任务写入的 source_label）占比。"""
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(8.5, 8), dpi=100)
    if not rows:
        ax.text(0.5, 0.5, "无样本", ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
        return _figure_to_b64_single(fig)
    labels = [_short_source_display(str(r["src"])) for r in rows]
    sizes = [int(r["c"]) for r in rows]
    colors = _extended_chart_colors(len(rows))
    wedges, texts, autotexts = ax.pie(
        sizes,
        labels=labels,
        autopct=lambda pct: f"{pct:.1f}%" if pct > 5 else "",
        colors=colors,
        pctdistance=0.72,
        labeldistance=1.08,
        textprops={"fontsize": 10},
        wedgeprops={"linewidth": 0.6, "edgecolor": "#1a2332"},
    )
    for t in autotexts:
        t.set_fontsize(9)
        t.set_color("#222")
    ax.set_title("入库影片 · 数据来源占比（抓取入口）", fontsize=13, pad=14)
    fig.tight_layout()
    return _figure_to_b64_single(fig)


def _build_viz_rating_hist_figure(ratings):
    """评分直方图（0.5 分一档），与数据分析页的 KDE 曲线形成互补。"""
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(9.2, 5.4), dpi=100)
    if not ratings:
        ax.text(0.5, 0.5, "无评分数据", ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
        return _figure_to_b64_single(fig)
    bins = np.linspace(0, 10, 21)
    ax.hist(ratings, bins=bins, color="#2a9d8f", edgecolor="#1d7065", linewidth=0.6, alpha=0.88)
    ax.set_xlabel("评分", fontsize=11)
    ax.set_ylabel("影片部数", fontsize=11)
    ax.set_title("评分离散分布（直方图 · 每柱宽 0.5 分）", fontsize=13, pad=12)
    ax.set_xlim(0, 10)
    ax.yaxis.get_major_locator().set_params(integer=True)
    fig.tight_layout()
    return _figure_to_b64_single(fig)


VIZ_NUMERIC_AXES: List[Tuple[str, str]] = [
    ("rating", "评分"),
    ("votes", "评价人数"),
    ("release_year", "上映年份"),
    ("chart_rank", "榜单名次"),
]


def _axis_title_cn(key: str) -> str:
    for k, lab in VIZ_NUMERIC_AXES:
        if k == key:
            return lab
    return key


def _pearson_xy(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) < 5 or len(xs) != len(ys):
        return None
    a = np.asarray(xs, dtype=float)
    b = np.asarray(ys, dtype=float)
    if np.nanstd(a) < 1e-12 or np.nanstd(b) < 1e-12:
        return None
    r = np.corrcoef(a, b)[0, 1]
    if np.isnan(r):
        return None
    return float(round(float(r), 4))


def _parse_viz_xy_params():
    """解析自选坐标轴；首次打开页面时对「人数」纵轴默认对数刻度。"""
    allowed = {k for k, _ in VIZ_NUMERIC_AXES}
    ax_x = (request.values.get("axis_x") or "rating").strip().lower()
    ax_y = (request.values.get("axis_y") or "votes").strip().lower()
    if ax_x not in allowed:
        ax_x = "rating"
    if ax_y not in allowed:
        ax_y = "votes"
    submitted = request.values.get("viz_submit") == "1"
    if submitted:
        log_x = request.values.get("log_x") == "1"
        log_y = request.values.get("log_y") == "1"
    else:
        log_x = False
        log_y = ax_y == "votes"
    return ax_x, ax_y, log_x, log_y


def _viz_xy_plot_values(raw: float, key: str, log_scale: bool) -> float:
    v = float(raw)
    if log_scale and key == "votes":
        return max(1.0, v)
    return v


def _build_viz_xy_figure(
    rows,
    ax_x: str,
    ax_y: str,
    log_x: bool,
    log_y: bool,
    pearson_r: Optional[float],
):
    """自选横纵字段的二维散点图。"""
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(9.2, 6.3), dpi=100)
    if not rows or ax_x == ax_y:
        msg = "横轴与纵轴不能相同" if ax_x == ax_y else "无同时具有两维数值字段的记录"
        ax.text(0.5, 0.5, msg, ha="center", va="center", transform=ax.transAxes, fontsize=12)
        ax.set_axis_off()
        return _figure_to_b64_single(fig)
    xs = []
    ys = []
    for r in rows:
        try:
            xs.append(_viz_xy_plot_values(float(r["xv"]), ax_x, log_x))
            ys.append(_viz_xy_plot_values(float(r["yv"]), ax_y, log_y))
        except (TypeError, ValueError):
            continue
    if len(xs) < 3:
        ax.text(0.5, 0.5, "有效点过少", ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
        return _figure_to_b64_single(fig)
    ax.scatter(
        xs,
        ys,
        alpha=0.88,
        s=46,
        c="#ea580c",
        edgecolors="#7c2d12",
        linewidths=0.45,
        zorder=2,
    )
    if log_x:
        ax.set_xscale("log")
    if log_y:
        ax.set_yscale("log")
    xl = _axis_title_cn(ax_x) + (" · 对数刻度" if log_x else "")
    yl = _axis_title_cn(ax_y) + (" · 对数刻度" if log_y else "")
    ax.set_xlabel(f"横轴：{xl}", fontsize=11)
    ax.set_ylabel(f"纵轴：{yl}", fontsize=11)
    ax.set_title(f"多维散点 · {_axis_title_cn(ax_y)} vs {_axis_title_cn(ax_x)}", fontsize=13, pad=12)
    ax.grid(True, alpha=0.38, linestyle="--", color="#94a3b8")
    note = f"点数：{len(xs)}（至多取样近期入库）"
    if pearson_r is not None:
        note += f" · Pearson r ≈ {pearson_r:.4f}"
    fig.text(0.5, 0.02, note, ha="center", fontsize=9.5, color="#445566")
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    return _figure_to_b64_single(fig)


def _viz_filter_template_kwargs(endpoint_name: str):
    """共用模板变量：多选年份 / 标签筛选；endpoint_name 供模板 url_for。"""
    filter_tags, filter_years = _parse_stats_filters()
    st_choices = {"years": [], "tags": []}
    try:
        st_choices["years"] = distinct_years()
        st_choices["tags"] = distinct_tags_split()
    except Exception:
        pass
    return {
        "filter_tags_sel": filter_tags or [],
        "filter_years_sel": filter_years or [],
        "st_choices": st_choices,
        "form_endpoint": endpoint_name,
    }


@app.route("/viz")
def viz_index():
    """扩展可视化入口（多页面汇总）。"""
    return render_template("viz_index.html")


@app.route("/viz/scatter")
def viz_scatter_page():
    chart_b64 = None
    err = None
    kpis = None
    corr_r = None
    corr_n = None
    vf = _viz_filter_template_kwargs("viz_scatter_page")
    ft = vf["filter_tags_sel"] or None
    fy = vf["filter_years_sel"] or None

    try:
        kpis = analytics_kpis(ft, fy)
        pairs = fetch_rating_votes_pairs(ft, fy, limit=4500)
        corr_n, corr_r = votes_rating_correlation(ft, fy)
        if kpis and (kpis.get("n") or 0) > 0:
            chart_b64 = _build_viz_scatter_figure(pairs, corr_r, corr_n or 0)
    except Exception as e:
        err = str(e)

    return render_template(
        "viz_scatter.html",
        db_error=err,
        chart_b64=chart_b64,
        kpis=kpis,
        corr_r=corr_r,
        corr_n=corr_n,
        **vf,
    )


@app.route("/viz/sources")
def viz_sources_page():
    chart_b64 = None
    err = None
    kpis = None
    rows_src = []
    vf = _viz_filter_template_kwargs("viz_sources_page")
    ft = vf["filter_tags_sel"] or None
    fy = vf["filter_years_sel"] or None

    try:
        kpis = analytics_kpis(ft, fy)
        rows_src = stats_count_by_source_label(ft, fy)
        if kpis and (kpis.get("n") or 0) > 0 and rows_src:
            chart_b64 = _build_viz_sources_figure(rows_src)
    except Exception as e:
        err = str(e)

    return render_template(
        "viz_sources.html",
        db_error=err,
        chart_b64=chart_b64,
        kpis=kpis,
        source_rows=rows_src,
        **vf,
    )


@app.route("/viz/rating-hist")
def viz_rating_hist_page():
    chart_b64 = None
    err = None
    kpis = None
    vf = _viz_filter_template_kwargs("viz_rating_hist_page")
    ft = vf["filter_tags_sel"] or None
    fy = vf["filter_years_sel"] or None

    try:
        kpis = analytics_kpis(ft, fy)
        ratings = fetch_ratings_for_median(ft, fy)
        if kpis and (kpis.get("n") or 0) > 0:
            chart_b64 = _build_viz_rating_hist_figure(ratings)
    except Exception as e:
        err = str(e)

    return render_template(
        "viz_rating_hist.html",
        db_error=err,
        chart_b64=chart_b64,
        kpis=kpis,
        **vf,
    )


@app.route("/viz/xy")
def viz_xy_page():
    """自选横轴 / 纵轴数值字段的二维散点（评分、人数、年份、名次）。"""
    chart_b64 = None
    err = None
    kpis = None
    axis_x, axis_y, log_x, log_y = _parse_viz_xy_params()
    vf = _viz_filter_template_kwargs("viz_xy_page")
    ft = vf["filter_tags_sel"] or None
    fy = vf["filter_years_sel"] or None
    pearson_r = None
    pair_count = 0

    try:
        kpis = analytics_kpis(ft, fy)
        if axis_x == axis_y:
            rows = []
        else:
            rows = fetch_numeric_xy_pairs(axis_x, axis_y, ft, fy, limit=7000)
        pair_count = len(rows)
        xs_raw = []
        ys_raw = []
        for r in rows:
            try:
                xs_raw.append(float(r["xv"]))
                ys_raw.append(float(r["yv"]))
            except (TypeError, ValueError):
                continue
        pearson_r = _pearson_xy(xs_raw, ys_raw)
        if (
            kpis
            and (kpis.get("n") or 0) > 0
            and axis_x != axis_y
        ):
            chart_b64 = _build_viz_xy_figure(rows, axis_x, axis_y, log_x, log_y, pearson_r)
    except Exception as e:
        err = str(e)

    return render_template(
        "viz_xy.html",
        db_error=err,
        chart_b64=chart_b64,
        kpis=kpis,
        axis_x=axis_x,
        axis_y=axis_y,
        log_x=log_x,
        log_y=log_y,
        axis_options=VIZ_NUMERIC_AXES,
        pair_count=pair_count,
        pearson_r=pearson_r,
        **vf,
    )


@app.route("/stats")
def stats_page():
    err = None
    chart_b64 = None
    kpis = None
    median_rating = None
    tag_top = []
    corr_n = None
    corr_r = None
    filter_tags, filter_years = _parse_stats_filters()
    ratings = None

    try:
        kpis = analytics_kpis(filter_tags, filter_years)
        ratings = fetch_ratings_for_median(filter_tags, filter_years)
        median_rating = statistics.median(ratings) if ratings else None
        tag_top = stats_top_tags(filter_tags, filter_years, 18)
        corr_n, corr_r = votes_rating_correlation(filter_tags, filter_years)
    except Exception as e:
        err = str(e)

    if not err and kpis and (kpis.get("n") or 0) > 0:
        try:
            chart_b64 = _build_stats_dashboard_figure(
                filter_tags, filter_years, ratings_precomputed=ratings or []
            )
        except Exception:
            chart_b64 = None

    st_choices = {"years": [], "tags": []}
    try:
        st_choices["years"] = distinct_years()
        st_choices["tags"] = distinct_tags_split()
    except Exception:
        pass

    export_pairs = []
    if filter_years:
        for y in filter_years:
            export_pairs.append(("year", str(y)))
    if filter_tags:
        for t in filter_tags:
            export_pairs.append(("tag", t))
    export_query = urlencode(export_pairs)

    return render_template(
        "stats.html",
        db_error=err,
        chart_b64=chart_b64,
        kpis=kpis,
        median_rating=median_rating,
        tag_top=tag_top,
        corr_n=corr_n,
        corr_r=corr_r,
        filter_tags_sel=filter_tags or [],
        filter_years_sel=filter_years or [],
        st_choices=st_choices,
        export_query=export_query,
    )


@app.route("/stats/export.csv")
def stats_export_csv():
    """按当前筛选导出 CSV，便于 Excel / pandas 再分析。"""
    filter_tags, filter_years = _parse_stats_filters()
    try:
        rows = fetch_for_analysis_export(filter_tags, filter_years, limit=50000)
    except Exception as e:
        return Response(f"导出失败：{e}", status=500, mimetype="text/plain; charset=utf-8")

    header = [
        "subject_id",
        "title",
        "release_year",
        "region",
        "tags",
        "rating",
        "votes",
        "directors",
        "casts",
        "duration",
        "imdb_id",
        "chart_rank",
        "crawled_at",
        "source_label",
    ]

    def row_tuple(r):
        return [
            r.get("subject_id") or "",
            r.get("title") or "",
            r.get("release_year") if r.get("release_year") is not None else "",
            r.get("region") or "",
            r.get("tags") or "",
            r.get("rating") if r.get("rating") is not None else "",
            r.get("votes") if r.get("votes") is not None else "",
            r.get("directors") or "",
            r.get("casts") or "",
            r.get("duration") or "",
            r.get("imdb_id") or "",
            r.get("chart_rank") if r.get("chart_rank") is not None else "",
            r.get("crawled_at") or "",
            r.get("source_label") or "",
        ]

    si = io.StringIO()
    w = csv.writer(si)
    w.writerow(header)
    for r in rows:
        w.writerow(row_tuple(r))

    payload = "\ufeff" + si.getvalue()
    fname = f"douban_movies_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename={fname}",
            "Cache-Control": "no-store",
        },
    )


def _opt_float(name):
    raw = (request.values.get(name) or "").strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _opt_int(name):
    raw = (request.values.get(name) or "").strip()
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _opt_nonneg_int(name: str, *, default: int = 0, hi: int = 10**9) -> int:
    raw = (request.values.get(name) or "").strip()
    if raw == "":
        return default
    try:
        return max(0, min(int(raw), hi))
    except ValueError:
        return default


def _build_crawl_kwargs():
    """
    从当前请求的查询参数或表单构造 run_crawl_and_store 的参数。
    返回 (kwargs_dict, None) 或 (None, error_tag)。
    """
    source = (request.values.get("source") or "chart").strip().lower()
    if source not in ("chart", "top250", "explore", "search"):
        source = "chart"

    if source == "search" and not (request.values.get("search_query") or "").strip():
        return None, "empty_search"

    limit = request.values.get("limit", default=20, type=int)
    if limit is None:
        limit = 20
    limit = max(1, min(limit, crawler.ABS_MAX_LIMIT))

    min_rating = _opt_float("min_rating")
    max_rating = _opt_float("max_rating")
    title_contains = (request.values.get("title_contains") or "").strip() or None
    min_votes = _opt_int("min_votes")
    min_year = _opt_int("min_year")
    max_year = _opt_int("max_year")
    tag_contains = (request.values.get("tag_contains") or "").strip() or None

    explore_sort = (request.values.get("explore_sort") or "U").strip() or "U"
    explore_tag = (request.values.get("explore_tag") or "").strip()
    explore_range = (request.values.get("explore_range") or "0,10").strip() or "0,10"
    explore_year_range = (request.values.get("explore_year_range") or "").strip()
    exp_pages = request.values.get("explore_max_pages", type=int)
    if exp_pages is None:
        exp_pages = 50
    explore_max_pages = max(1, min(exp_pages, crawler.EXPLORE_MAX_PAGES_HARD))

    search_query = (request.values.get("search_query") or "").strip()
    smp = request.values.get("search_max_pages", type=int)
    if smp is None:
        smp = 30
    search_max_pages = max(1, min(smp, crawler.SEARCH_MAX_PAGES_HARD))

    search_start_offset = _opt_nonneg_int("search_start_offset", default=0, hi=50000)
    explore_start_offset = _opt_nonneg_int("explore_start_offset", default=0, hi=50000)
    top250_start = _opt_nonneg_int("top250_start", default=0, hi=249)

    chart_mode = (request.values.get("chart_mode") or "new").strip().lower()
    if chart_mode not in ("new", "weekly", "na_boxoffice"):
        chart_mode = "new"

    return (
        {
            "source": source,
            "limit": limit,
            "min_rating": min_rating,
            "max_rating": max_rating,
            "title_contains": title_contains,
            "min_votes": min_votes,
            "min_year": min_year,
            "max_year": max_year,
            "tag_contains": tag_contains,
            "explore_sort": explore_sort,
            "explore_tag": explore_tag,
            "explore_range": explore_range,
            "explore_year_range": explore_year_range,
            "explore_max_pages": explore_max_pages,
            "search_query": search_query,
            "search_max_pages": search_max_pages,
            "search_start_offset": search_start_offset,
            "explore_start_offset": explore_start_offset,
            "top250_start": top250_start,
            "chart_mode": chart_mode,
        },
        None,
    )


@app.route("/fetch")
def fetch_form():
    """抓取参数页（GET）。"""
    lim = request.args.get("limit", type=int)
    if lim is None:
        lim = 20
    defaults = {
        "source": request.args.get("source") or "search",
        "limit": lim,
        "min_rating": request.args.get("min_rating") or "",
        "max_rating": request.args.get("max_rating") or "",
        "title_contains": request.args.get("title_contains") or "",
        "min_votes": request.args.get("min_votes") or "",
        "min_year": request.args.get("min_year") or "",
        "max_year": request.args.get("max_year") or "",
        "tag_contains": request.args.get("tag_contains") or "",
        "explore_sort": request.args.get("explore_sort") or "U",
        "explore_tag": request.args.get("explore_tag") or "",
        "explore_range": request.args.get("explore_range") or "0,10",
        "explore_year_range": request.args.get("explore_year_range") or "",
        "explore_max_pages": request.args.get("explore_max_pages") or "50",
        "search_query": request.args.get("search_query") or "",
        "search_max_pages": request.args.get("search_max_pages") or "30",
        "search_start_offset": request.args.get("search_start_offset") or "",
        "explore_start_offset": request.args.get("explore_start_offset") or "",
        "top250_start": request.args.get("top250_start") or "",
        "chart_mode": request.args.get("chart_mode") or "new",
    }
    return render_template(
        "fetch.html",
        defaults=defaults,
        fetch_hint=request.args.get("hint") or "",
    )


@app.route("/fetch/run")
def fetch_run():
    """按查询参数同步执行抓取后回首页（快捷链接等仍可用）。"""
    kwargs, err = _build_crawl_kwargs()
    if err == "empty_search":
        return redirect(url_for("fetch_form", hint="empty_search"))
    try:
        init_tables()
        ensure_crawl_log_message_width()
        crawler.run_crawl_and_store(**kwargs)
    except Exception:
        pass
    return redirect(url_for("index"))


def _crawl_progress_snapshot(job_id: str):
    with _crawl_jobs_lock:
        row = _crawl_jobs.get(job_id)
        if not row:
            return None
        out = dict(row)
    phase = out.get("phase") or ""
    cur = int(out.get("current") or 0)
    tot = int(out.get("total") or 0)
    pct = None
    if out.get("status") == "done":
        pct = 100
    elif phase == "enrich" and tot > 0:
        pct = min(100, int(100 * cur / tot))
    elif phase == "list":
        # 候选列表阶段耗时不定，不推算百分比，避免条短暂跳到固定比例
        pct = None
    out["percent"] = pct
    return out


@app.route("/fetch/start", methods=["POST"])
def fetch_start():
    """异步启动抓取，返回 job_id 供轮询进度。"""
    kwargs, err = _build_crawl_kwargs()
    if err == "empty_search":
        return jsonify({"ok": False, "error": "empty_search"}), 400

    job_id = str(uuid.uuid4())
    with _crawl_jobs_lock:
        _crawl_jobs[job_id] = {
            "status": "running",
            "phase": "list",
            "current": 0,
            "total": 1,
            "message": "排队中…",
            "inserted": None,
            "rows_out": None,
            "src": None,
            "error": None,
        }

    def progress_cb(phase: str, current: int, total: int, detail: str) -> None:
        with _crawl_jobs_lock:
            if job_id in _crawl_jobs:
                _crawl_jobs[job_id].update(
                    {
                        "phase": phase,
                        "current": current,
                        "total": max(total, 1),
                        "message": detail or "",
                    }
                )

    def worker():
        try:
            init_tables()
            ensure_crawl_log_message_width()
            inserted, src, rows_len = crawler.run_crawl_and_store(
                **kwargs, progress_callback=progress_cb
            )
            with _crawl_jobs_lock:
                if job_id in _crawl_jobs:
                    _crawl_jobs[job_id].update(
                        {
                            "status": "done",
                            "phase": "done",
                            "current": rows_len,
                            "total": max(rows_len, 1),
                            "message": "完成",
                            "inserted": inserted,
                            "rows_out": rows_len,
                            "src": src,
                        }
                    )
        except Exception as ex:
            with _crawl_jobs_lock:
                if job_id in _crawl_jobs:
                    _crawl_jobs[job_id].update(
                        {
                            "status": "error",
                            "message": str(ex) or "抓取失败",
                            "error": str(ex),
                        }
                    )

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/fetch/progress/<job_id>")
def fetch_progress(job_id: str):
    snap = _crawl_progress_snapshot(job_id)
    if snap is None:
        return jsonify({"ok": False, "error": "unknown_job"}), 404
    return jsonify({"ok": True, **snap})


@app.route("/data/clear", methods=["POST"])
def clear_library():
    """清空库内全部影片与抓取日志（需勾选确认）。"""
    if request.form.get("confirm") != "yes":
        return redirect(url_for("fetch_form", hint="clear_need_confirm"))
    try:
        init_tables()
        clear_library_data()
    except Exception:
        return redirect(url_for("fetch_form", hint="clear_err"))
    return redirect(url_for("index", cleared=1))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
