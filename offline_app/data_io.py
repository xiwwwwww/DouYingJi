"""离线端：读取本地「影片档案」CSV 与简单清洗（兼容示例档案与 Web 导出的列名）。"""
from pathlib import Path

import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 默认接入 Web 影片库导出的抓取数据（可用「从 CSV 加载」切换回 movies_archive 等文件）
DEFAULT_CSV = _PROJECT_ROOT / "data" / "offline" / "movies_library_20260506_1542.csv"

# 上映年份合理区间；缺失、不可解析或越界的行在归一化时直接剔除（不使用 0 占位）
YEAR_VALID_MIN = 1895
YEAR_VALID_MAX = 2035

# 从 Web/其它 CSV 读入时只保留这些列（若存在），忽略 summary、导演、海报 URL 等宽字段
LOAD_COLUMNS = (
    "subject_id",
    "title",
    "release_year",
    "year",
    "region",
    "tags",
    "genre",
    "rating",
    "votes",
    "votes_thousands",
    "chart_rank",
    "source_label",
    "crawled_at",
)


def _subset_for_offline(df: pd.DataFrame) -> pd.DataFrame:
    use = [c for c in LOAD_COLUMNS if c in df.columns]
    if "title" not in use:
        raise ValueError("CSV 必须包含列 title（片名）")
    return df[use].copy()


def _series_year(df: pd.DataFrame) -> pd.Series:
    if "year" in df.columns:
        y = pd.to_numeric(df["year"], errors="coerce")
    elif "release_year" in df.columns:
        y = pd.to_numeric(df["release_year"], errors="coerce")
    else:
        y = pd.Series([pd.NA] * len(df), index=df.index)
    return y


def _series_genre(df: pd.DataFrame) -> pd.Series:
    if "genre" in df.columns and df["genre"].notna().any():
        return df["genre"].fillna("未分类").astype(str)
    if "tags" in df.columns:
        return (
            df["tags"]
            .fillna("")
            .astype(str)
            .apply(lambda s: (s.split(",")[0].strip() or "未分类"))
        )
    return pd.Series(["未分类"] * len(df), index=df.index, dtype=object)


def _series_votes(df: pd.DataFrame) -> pd.Series:
    if "votes" in df.columns:
        v = pd.to_numeric(df["votes"], errors="coerce")
        if v.notna().any():
            return v
    if "votes_thousands" in df.columns:
        return pd.to_numeric(df["votes_thousands"], errors="coerce") * 1000.0
    return pd.Series([pd.NA] * len(df), index=df.index, dtype=float)


def _as_nullable_int_series(s: pd.Series) -> pd.Series:
    """人数、名次等显示为整数（pandas 可空 Int64）。"""
    x = pd.to_numeric(s, errors="coerce")
    return x.round().astype("Int64")


def normalize_movie_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """统一内部使用的列：year / genre / votes / region，便于筛选与作图。"""
    out = df.copy()
    if "title" not in out.columns:
        raise ValueError("CSV 必须包含列 title（片名）")

    out["year"] = pd.to_numeric(_series_year(out), errors="coerce")
    ok_year = out["year"].notna() & (out["year"] >= YEAR_VALID_MIN) & (out["year"] <= YEAR_VALID_MAX)
    out = out.loc[ok_year].copy()
    out["year"] = out["year"].astype(int)

    out["genre"] = _series_genre(out)
    out["rating"] = pd.to_numeric(out.get("rating"), errors="coerce")
    out["votes"] = _as_nullable_int_series(_series_votes(out))

    if "region" not in out.columns:
        out["region"] = "未知"
    else:
        out["region"] = out["region"].fillna("未知").astype(str)

    for col in ("votes_thousands", "chart_rank", "release_year"):
        if col in out.columns:
            out[col] = _as_nullable_int_series(out[col])

    return out.reset_index(drop=True)


def load_movies_csv(path=None):
    path = Path(path) if path else DEFAULT_CSV
    # utf-8-sig：兼容 Web/Excel 导出带 BOM；字段中含换行时保持引号内解析
    raw = pd.read_csv(path, encoding="utf-8-sig")
    raw = _subset_for_offline(raw)
    return normalize_movie_dataframe(raw)


def save_movies_csv(df: pd.DataFrame, path) -> None:
    """将当前 DataFrame 写入 UTF-8 CSV（与网页导出类似，便于备份）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8")


def all_distinct_tag_labels(df: pd.DataFrame) -> list[str]:
    """汇总「类型」筛选下拉里可选的标签：来自 tags 逗号拆分 + genre 列（含仅有单列 genre 的 CSV）。"""
    seen: set[str] = set()
    if "tags" in df.columns:
        for cell in df["tags"].dropna().astype(str):
            for x in str(cell).split(","):
                t = x.strip()
                if t:
                    seen.add(t)
    if "genre" in df.columns:
        for cell in df["genre"].dropna().astype(str):
            t = str(cell).strip()
            if t:
                seen.add(t)
    return sorted(seen)


def mask_matches_genre(df: pd.DataFrame, genre: str) -> pd.Series:
    """所选类型是否与该行匹配：genre 列相等，或 tags 逗号分隔中含该标签。"""
    g = (genre or "").strip()
    if not g:
        return pd.Series(True, index=df.index)
    eq = df["genre"].astype(str) == g
    if "tags" not in df.columns:
        return eq

    def hit(cell: object) -> bool:
        if pd.isna(cell):
            return False
        parts = [x.strip() for x in str(cell).split(",") if x.strip()]
        return g in parts

    return eq | df["tags"].apply(hit)


def filter_dataframe(
    df,
    genre=None,
    region=None,
    min_rating=None,
    max_rating=None,
    year_min=None,
    year_max=None,
    title_contains=None,
):
    out = df.copy()
    if genre:
        out = out.loc[mask_matches_genre(out, genre)].copy()
    if region:
        out = out[out["region"] == region]
    if min_rating is not None:
        out = out[out["rating"] >= float(min_rating)]
    if max_rating is not None:
        out = out[out["rating"] <= float(max_rating)]
    if year_min is not None:
        out = out[out["year"] >= int(year_min)]
    if year_max is not None:
        out = out[out["year"] <= int(year_max)]
    if title_contains:
        t = str(title_contains).strip()
        if t:
            out = out[out["title"].astype(str).str.contains(t, case=False, na=False)]
    return out.sort_values(["year", "title"])


def summary_stats(df):
    if df.empty:
        return {
            "rows": 0,
            "genres": 0,
            "years": "—",
            "rating_mean": 0.0,
            "rating_median": 0.0,
            "votes_median": None,
        }
    y = df["year"]
    tag_n = len(all_distinct_tag_labels(df))
    st = {
        "rows": len(df),
        "genres": tag_n if tag_n else int(df["genre"].nunique()),
        "years": f"{int(y.min())}–{int(y.max())}",
        "rating_mean": round(float(df["rating"].mean()), 2),
        "rating_median": round(float(df["rating"].median()), 2),
        "votes_median": None,
    }
    if "votes" in df.columns and df["votes"].notna().any():
        st["votes_median"] = int(df["votes"].median())
    return st


def numeric_plot_columns(df) -> list[str]:
    """可用于「多维散点」的数值列（排除明显非度量列）。"""
    skip = {"subject_id"}
    cols = []
    for c in df.columns:
        if c in skip:
            continue
        s = pd.to_numeric(df[c], errors="coerce")
        if s.notna().sum() >= 2:
            cols.append(c)
    preferred = ["year", "rating", "votes", "votes_thousands", "chart_rank"]
    front = [c for c in preferred if c in cols]
    rest = [c for c in cols if c not in front]
    return front + sorted(rest)
