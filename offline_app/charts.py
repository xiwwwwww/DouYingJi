"""离线端：matplotlib 统计图（与 Web 端可视化能力大致对齐）。"""
import matplotlib

matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import pandas as pd

# 略大于默认尺寸，配合放大后的主窗口
_FIG_BAR_LINE = (9.5, 5.4)
_FIG_PIE = (7.2, 7.2)
# 饼图仅展示计数前 N 名，其余并入「其他」，避免标签拥挤
_PIE_TOP_N = 10


def _extended_palette(n: int) -> list:
    """生成 n 种易区分颜色（拼接 tab20 / tab20b / Set3 等，避免类别多时重复使用少量默认色）。"""
    if n <= 0:
        return []
    out = []
    for name in ("tab20", "tab20b", "Set3", "Pastel1"):
        try:
            cmap = plt.colormaps[name]
        except (AttributeError, KeyError, TypeError):
            cmap = matplotlib.cm.get_cmap(name)
        ncol = getattr(cmap, "N", 12)
        for i in range(ncol):
            out.append(cmap(i / max(ncol - 1, 1)))
            if len(out) >= n:
                return out[:n]
    # 理论上很少触发：循环复用已收集的颜色
    base = out if out else [(0.5, 0.5, 0.8, 1.0)]
    for i in range(len(base), n):
        base.append(base[i % len(base)])
    return base[:n]


def _merge_small_slices(counts: pd.Series, top_n: int = _PIE_TOP_N, other_label: str = "其他"):
    """保留计数前 top_n 类，其余合并为「其他」。"""
    counts = counts.sort_values(ascending=False)
    if len(counts) <= top_n:
        return counts
    head = counts.iloc[:top_n]
    tail = counts.iloc[top_n:].sum()
    return pd.concat([head, pd.Series({other_label: tail})])


def _setup_chinese_font():
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    # 嵌入 Tk 窗口时放大，便于阅读
    plt.rcParams["font.size"] = 12
    plt.rcParams["axes.titlesize"] = 15
    plt.rcParams["axes.labelsize"] = 12
    plt.rcParams["xtick.labelsize"] = 11
    plt.rcParams["ytick.labelsize"] = 11


def plot_genre_rating_trend(df_genre, genre_label=None):
    """某类型：按年份的平均评分折线。genre_label 用于标题（与 genre 列首标签不必一致，例如按 tags 子标签筛选时）。"""
    _setup_chinese_font()
    g = df_genre.groupby("year", as_index=False)["rating"].mean()
    genre_name = genre_label if genre_label else df_genre["genre"].iloc[0]
    fig, ax = plt.subplots(figsize=_FIG_BAR_LINE, dpi=100)
    ax.plot(g["year"], g["rating"], marker="o", linewidth=2, color="darkred")
    ax.set_title(f"类型「{genre_name}」— 年度平均评分趋势")
    ax.set_xlabel("年份")
    ax.set_ylabel("平均评分")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    return fig


def plot_top_movies_bar(df_year, top_n=8):
    """某一年评分最高的若干部影片（柱状）。"""
    _setup_chinese_font()
    sub = df_year.sort_values("rating", ascending=False).head(top_n)
    fig, ax = plt.subplots(figsize=_FIG_BAR_LINE, dpi=100)
    labels = sub["title"]
    cols = _extended_palette(len(sub))
    ax.barh(labels, sub["rating"], color=cols)
    year = int(sub["year"].iloc[0])
    ax.set_title(f"{year} 年 · 档案中评分 Top {len(sub)}")
    ax.set_xlabel("评分")
    fig.tight_layout()
    return fig


def _type_counts_for_pie(df_year: pd.DataFrame) -> pd.Series:
    """统计各类型的计数：若存在 tags，则按逗号拆分，每条影片可对多个类型各计 1 次（与筛选逻辑一致）；否则只用 genre 列。"""
    if "tags" not in df_year.columns:
        return df_year.groupby("genre").size()
    labels: list[str] = []
    for _, r in df_year.iterrows():
        raw = r.get("tags")
        if pd.notna(raw) and str(raw).strip():
            for part in str(raw).split(","):
                t = part.strip()
                if t:
                    labels.append(t)
        else:
            g = str(r.get("genre", "未分类")).strip() or "未分类"
            labels.append(g)
    if not labels:
        return df_year.groupby("genre").size()
    return pd.Series(labels).value_counts()


def plot_genre_share_pie(df_year):
    """某一年各类型占比（饼图）；多标签 CSV 下按标签分别计数。"""
    _setup_chinese_font()
    g = _type_counts_for_pie(df_year).sort_values(ascending=False)
    g = _merge_small_slices(g)
    fig, ax = plt.subplots(figsize=(9.2, 7.2), dpi=100)
    cols = _extended_palette(len(g))
    wedges, _t, autotexts = ax.pie(
        g.values,
        labels=None,
        autopct="%1.1f%%",
        startangle=90,
        pctdistance=0.82,
        colors=cols,
        wedgeprops=dict(linewidth=0.6, edgecolor="white"),
    )
    for t in autotexts:
        t.set_fontsize(9)
    ax.legend(
        wedges,
        list(g.index),
        title="类型",
        loc="center left",
        bbox_to_anchor=(1.02, 0.5),
        fontsize=10,
        frameon=True,
    )
    year = int(df_year["year"].iloc[0])
    sub = "（按标签分别计数）" if "tags" in df_year.columns else "（按 genre 列）"
    ax.set_title(f"{year} 年 · 类型占比 {sub}")
    fig.subplots_adjust(right=0.68)
    return fig


def plot_rating_votes_scatter(df):
    """评分 × 评价人数散点（人数取对数坐标，与 Web 端一致思路）。"""
    _setup_chinese_font()
    if "votes" not in df.columns:
        return None
    sub = df.dropna(subset=["rating", "votes"]).copy()
    sub = sub[pd.to_numeric(sub["votes"], errors="coerce") > 0]
    if sub.empty:
        return None
    fig, ax = plt.subplots(figsize=_FIG_BAR_LINE, dpi=100)
    x = pd.to_numeric(sub["votes"], errors="coerce")
    y = pd.to_numeric(sub["rating"], errors="coerce")
    ax.scatter(x, y, alpha=0.65, s=36, c="teal", edgecolors="white", linewidths=0.4)
    ax.set_xscale("log")
    ax.set_xlabel("评价人数（对数坐标）")
    ax.set_ylabel("评分")
    ax.set_title("评分 × 评价人数")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    return fig


def plot_rating_histogram(df, bins=20):
    """评分分布直方图。"""
    _setup_chinese_font()
    r = pd.to_numeric(df["rating"], errors="coerce").dropna()
    if r.empty:
        return None
    fig, ax = plt.subplots(figsize=_FIG_BAR_LINE, dpi=100)
    ax.hist(r.values, bins=bins, range=(0, 10), color="coral", edgecolor="white", linewidth=0.6)
    ax.set_xlabel("评分")
    ax.set_ylabel("部数")
    ax.set_title("评分分布直方图")
    ax.grid(True, axis="y", alpha=0.25)
    fig.tight_layout()
    return fig


def plot_region_share_pie(df):
    """地区部数占比（当前筛选结果全集）。"""
    _setup_chinese_font()
    if "region" not in df.columns or df.empty:
        return None
    g = df.groupby("region").size().sort_values(ascending=False)
    if g.empty:
        return None
    g = _merge_small_slices(g)
    fig, ax = plt.subplots(figsize=(9.2, 7.2), dpi=100)
    cols = _extended_palette(len(g))
    wedges, _t, autotexts = ax.pie(
        g.values,
        labels=None,
        autopct="%1.1f%%",
        startangle=90,
        pctdistance=0.82,
        colors=cols,
        wedgeprops=dict(linewidth=0.6, edgecolor="white"),
    )
    for t in autotexts:
        t.set_fontsize(9)
    ax.legend(
        wedges,
        list(g.index),
        title="地区",
        loc="center left",
        bbox_to_anchor=(1.02, 0.5),
        fontsize=10,
        frameon=True,
    )
    ax.set_title("地区部数占比")
    fig.subplots_adjust(right=0.68)
    return fig


def plot_xy_scatter(df, xcol: str, ycol: str):
    """自选横纵轴的二维散点（多维探索）。"""
    _setup_chinese_font()
    if xcol not in df.columns or ycol not in df.columns:
        return None
    sub = df[[xcol, ycol]].copy()
    sub[xcol] = pd.to_numeric(sub[xcol], errors="coerce")
    sub[ycol] = pd.to_numeric(sub[ycol], errors="coerce")
    sub = sub.dropna()
    if len(sub) < 2:
        return None
    fig, ax = plt.subplots(figsize=_FIG_BAR_LINE, dpi=100)
    ax.scatter(sub[xcol], sub[ycol], alpha=0.65, s=36, c="#6a5acd", edgecolors="white", linewidths=0.4)
    ax.set_xlabel(xcol)
    ax.set_ylabel(ycol)
    ax.set_title(f"{xcol} × {ycol}")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    return fig
