"""
离线版入口：Tkinter Notebook — 数据浏览 / 趋势分析 / 对比统计 / 扩展可视化。
运行：在项目根目录执行  python -m offline_app.main
"""
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

import pandas as pd

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from offline_app.charts import (
    plot_genre_rating_trend,
    plot_genre_share_pie,
    plot_rating_histogram,
    plot_rating_votes_scatter,
    plot_region_share_pie,
    plot_top_movies_bar,
    plot_xy_scatter,
)
from offline_app.data_io import (
    DEFAULT_CSV,
    all_distinct_tag_labels,
    filter_dataframe,
    load_movies_csv,
    mask_matches_genre,
    numeric_plot_columns,
    save_movies_csv,
    summary_stats,
)

# 表格中按整数显示的列（避免从 CSV 读入后呈 2023.0、70550.0）
_INT_TREE_COLS = frozenset({"year", "release_year", "votes", "votes_thousands", "chart_rank"})


def _format_tree_cell(column: str, val):
    if pd.isna(val):
        return ""
    if column in _INT_TREE_COLS:
        try:
            return str(int(round(float(val))))
        except (TypeError, ValueError):
            return str(val)
    return val


def _parse_opt_int(s, default=None):
    s = str(s).strip() if s is not None else ""
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        return default


def _parse_opt_float(s, default=None):
    s = str(s).strip() if s is not None else ""
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _default_genre_multi_year(df: pd.DataFrame):
    """在当前表中选出第一个「至少跨两个不同年份」的类型，用于趋势图默认选项。"""
    if df is None or df.empty or "genre" not in df.columns:
        return None
    for g in all_distinct_tag_labels(df):
        sub = df.loc[mask_matches_genre(df, g)]
        if len(sub.groupby("year")) >= 2:
            return g
    return None


def _configure_ui_fonts(root: tk.Tk) -> None:
    """放大默认字号与表格行高，避免小窗口里文字过小。"""
    ui_pt = 14
    for fname in ("TkDefaultFont", "TkTextFont", "TkFixedFont", "TkHeadingFont", "TkMenuFont"):
        try:
            tkfont.nametofont(fname).configure(size=ui_pt)
        except tk.TclError:
            pass

    try:
        fam = tkfont.nametofont("TkDefaultFont").cget("family")
    except tk.TclError:
        fam = "Segoe UI"
    body = (fam, ui_pt)
    body_bold = (fam, ui_pt, "bold")

    style = ttk.Style(root)
    for w in ("TLabel", "TButton", "TEntry", "TCombobox"):
        style.configure(w, font=body)
    style.configure("Treeview", font=body, rowheight=max(34, ui_pt + 20))
    style.configure("Treeview.Heading", font=body_bold)
    style.configure("TNotebook.Tab", font=body)


class OfflineMovieApp:
    def __init__(self):
        self._csv_path = None
        self.df = load_movies_csv()
        self.filtered_df = self.df.copy()
        self.root = tk.Tk()
        self.root.title("离线版 · 影片档案统计（本地数据集）")
        _configure_ui_fonts(self.root)
        self.root.geometry("1320x880")

        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self._build_tab_table(nb)
        self._build_tab_trend(nb)
        self._build_tab_compare(nb)
        self._build_tab_extra(nb)

        self._refresh_dependent_controls()
        self._update_summary_label()
        self._refresh_table()
        self._draw_extra()

    def _extra_chart_option_pairs(self):
        opts = [
            ("评分 × 人数（散点）", "scatter_votes"),
            ("评分分布（直方图）", "histogram"),
            ("地区占比（饼图）", "pie_region"),
            ("自选横纵轴（散点）", "xy"),
        ]
        return opts

    def _sync_extra_chart_combo(self):
        pairs = self._extra_chart_option_pairs()
        labels = [p[0] for p in pairs]
        self._extra_kind_map = {p[0]: p[1] for p in pairs}
        self.extra_type_cb.configure(values=labels)
        cur = self.extra_display.get()
        if cur not in labels:
            self.extra_display.set(labels[0])

    def _get_filtered_df(self):
        return filter_dataframe(
            self.df,
            genre=self.genre_var.get() or None,
            region=self.region_var.get() or None,
            min_rating=_parse_opt_float(self.rmin_var.get()),
            max_rating=_parse_opt_float(self.rmax_var.get()),
            year_min=_parse_opt_int(self.ymin_var.get()),
            year_max=_parse_opt_int(self.ymax_var.get()),
            title_contains=self.title_var.get() or None,
        )

    def _update_summary_label(self):
        info = summary_stats(self.filtered_df)
        extra = f" | 评分中位 {info['rating_median']}"
        if info.get("votes_median") is not None:
            extra += f" | 人数中位 {info['votes_median']}"
        self.summary_label.config(
            text=(
                f"记录 {info['rows']} 条 | 类型数 {info['genres']} | 年份 {info['years']} "
                f"| 平均评分 ≈ {info['rating_mean']}{extra}"
            ),
        )

    def _refresh_dependent_controls(self):
        g_sorted = all_distinct_tag_labels(self.df)
        self.trend_genre_cb.configure(values=g_sorted)
        cur = self.trend_genre.get()
        if not g_sorted:
            pass
        elif cur not in g_sorted:
            pick = _default_genre_multi_year(self.filtered_df)
            self.trend_genre.set(pick or g_sorted[0])
        years = sorted(self.filtered_df["year"].unique().tolist())
        self.year_cb.configure(values=years or [0])
        if years:
            try:
                cur = int(self.year_var.get())
            except (tk.TclError, ValueError, TypeError):
                cur = None
            if cur is None or cur not in years:
                self.year_var.set(int(max(years)))

        nums = numeric_plot_columns(self.filtered_df)
        self.xy_x_cb.configure(values=nums)
        self.xy_y_cb.configure(values=nums)
        if len(nums) >= 2:
            if self.xy_x.get() not in nums:
                self.xy_x.set(nums[0])
            if self.xy_y.get() not in nums:
                self.xy_y.set(nums[1] if nums[1] != nums[0] else nums[-1])

        self._sync_extra_chart_combo()
        self._sync_sort_choices()

    def _sync_sort_choices(self):
        pairs = [("默认（年份、片名）", "__default__")]
        candidates = [
            ("年份", "year"),
            ("评分", "rating"),
            ("评价人数", "votes"),
            ("片名", "title"),
            ("类型", "genre"),
            ("地区", "region"),
            ("榜单名次", "chart_rank"),
        ]
        for zh, k in candidates:
            if k in self.df.columns:
                pairs.append((zh, k))
        self._sort_zh_to_key = {p[0]: p[1] for p in pairs}
        labels = [p[0] for p in pairs]
        self._sort_cb.configure(values=labels)
        cur = self.sort_col_display_var.get()
        if cur not in labels:
            self.sort_col_display_var.set(labels[0])

    def _on_sort_changed(self):
        if getattr(self, "tree", None) is None:
            return
        self._refresh_table()

    def _sorted_display_df(self):
        sub = self.filtered_df.copy()
        zh = self.sort_col_display_var.get()
        key = self._sort_zh_to_key.get(zh, "__default__")
        asc = self.sort_order_var.get() == "升序"
        if key == "__default__":
            return sub.sort_values(["year", "title"], ascending=[True, True], na_position="last")
        if key not in sub.columns:
            return sub.sort_values(["year", "title"], ascending=[True, True], na_position="last")
        return sub.sort_values(by=key, ascending=asc, na_position="last")

    def _build_tab_table(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="① 数据浏览")

        self.summary_label = ttk.Label(tab, text="")
        self.summary_label.pack(anchor="w", padx=8, pady=4)

        row_f = ttk.Frame(tab)
        row_f.pack(fill=tk.X, padx=8)
        ttk.Label(row_f, text="类型：").pack(side=tk.LEFT)
        self.genre_var = tk.StringVar()
        genres = [""] + all_distinct_tag_labels(self.df)
        self._genre_cb = ttk.Combobox(row_f, textvariable=self.genre_var, values=genres, width=12)
        self._genre_cb.pack(side=tk.LEFT, padx=4)
        ttk.Label(row_f, text="地区：").pack(side=tk.LEFT)
        self.region_var = tk.StringVar()
        regions = [""] + sorted(self.df["region"].unique().tolist())
        self._region_cb = ttk.Combobox(row_f, textvariable=self.region_var, values=regions, width=14)
        self._region_cb.pack(side=tk.LEFT, padx=4)

        row2 = ttk.Frame(tab)
        row2.pack(fill=tk.X, padx=8, pady=2)
        ttk.Label(row2, text="年份≥").pack(side=tk.LEFT)
        self.ymin_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.ymin_var, width=7).pack(side=tk.LEFT, padx=2)
        ttk.Label(row2, text="年份≤").pack(side=tk.LEFT)
        self.ymax_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.ymax_var, width=7).pack(side=tk.LEFT, padx=2)
        ttk.Label(row2, text="评分≥").pack(side=tk.LEFT)
        self.rmin_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.rmin_var, width=5).pack(side=tk.LEFT, padx=2)
        ttk.Label(row2, text="评分≤").pack(side=tk.LEFT)
        self.rmax_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.rmax_var, width=5).pack(side=tk.LEFT, padx=2)
        ttk.Label(row2, text="片名包含").pack(side=tk.LEFT)
        self.title_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.title_var, width=18).pack(side=tk.LEFT, padx=4)

        row_sort = ttk.Frame(tab)
        row_sort.pack(fill=tk.X, padx=8, pady=2)
        ttk.Label(row_sort, text="排序：").pack(side=tk.LEFT)
        self.sort_col_display_var = tk.StringVar(value="默认（年份、片名）")
        self._sort_cb = ttk.Combobox(
            row_sort,
            textvariable=self.sort_col_display_var,
            values=("默认（年份、片名）",),
            width=20,
            state="readonly",
        )
        self._sort_cb.pack(side=tk.LEFT, padx=4)
        ttk.Label(row_sort, text="顺序：").pack(side=tk.LEFT)
        self.sort_order_var = tk.StringVar(value="升序")
        self._sort_order_cb = ttk.Combobox(
            row_sort,
            textvariable=self.sort_order_var,
            values=("升序", "降序"),
            width=6,
            state="readonly",
        )
        self._sort_order_cb.pack(side=tk.LEFT, padx=4)
        self._sort_zh_to_key = {"默认（年份、片名）": "__default__"}

        row3 = ttk.Frame(tab)
        row3.pack(fill=tk.X, padx=8, pady=4)
        ttk.Button(row3, text="应用筛选", command=self._on_apply_filter).pack(side=tk.LEFT)
        ttk.Button(row3, text="从 CSV 加载…", command=self._load_csv_dialog).pack(side=tk.LEFT, padx=8)
        ttk.Button(row3, text="删除选中行", command=self._delete_selected_rows).pack(side=tk.LEFT, padx=4)
        ttk.Button(row3, text="保存到 CSV…", command=self._save_csv_dialog).pack(side=tk.LEFT, padx=4)
        self.path_label = ttk.Label(row3, text="", foreground="gray")
        self.path_label.pack(side=tk.LEFT, padx=6)

        tree_fr = ttk.Frame(tab)
        tree_fr.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self._tree_container = tree_fr
        self._tree_cols = None
        self._build_tree_widgets(tree_fr)
        self.sort_col_display_var.trace_add("write", lambda *_: self._on_sort_changed())
        self.sort_order_var.trace_add("write", lambda *_: self._on_sort_changed())

    def _build_tree_widgets(self, parent):
        cols = list(self.df.columns)
        self._tree_cols = cols
        self.tree = ttk.Treeview(
            parent,
            columns=cols,
            show="headings",
            height=16,
            selectmode="extended",
        )
        for c in cols:
            self.tree.heading(c, text=c)
            w = 188 if c in ("title", "tags", "directors") else 132
            self.tree.column(c, width=min(w, 230), anchor="center")
        vsb = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(parent, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        self._vsb = vsb
        self._hsb = hsb

    def _rebuild_tree_if_needed(self):
        cols = list(self.df.columns)
        if cols == self._tree_cols:
            return
        self.tree.destroy()
        self._vsb.destroy()
        self._hsb.destroy()
        self._build_tree_widgets(self._tree_container)

    def _on_apply_filter(self):
        self.filtered_df = self._get_filtered_df()
        self._refresh_dependent_controls()
        self._update_summary_label()
        self._refresh_table()

    def _refresh_table(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        sub = self._sorted_display_df()
        cols = list(self.df.columns)
        for idx, row in sub.head(800).iterrows():
            vals = []
            for c in cols:
                v = row[c]
                vals.append(_format_tree_cell(c, v))
            # iid 使用 DataFrame 行索引，便于与网页版一致按条删除
            self.tree.insert("", tk.END, iid=str(idx), values=vals)

    @staticmethod
    def _index_from_tree_iid(iid: str):
        s = str(iid).strip()
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            return int(s)
        return s

    def _delete_selected_rows(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先在表格中单击选中一行（可按住 Ctrl 多选）。")
            return
        if not messagebox.askyesno(
            "确认删除",
            "确定从当前数据中删除所选记录？\n（仅内存；若要写回文件请随后点击「保存到 CSV」）",
        ):
            return
        labels = [self._index_from_tree_iid(iid) for iid in sel]
        try:
            self.df = self.df.drop(index=labels).reset_index(drop=True)
        except (KeyError, ValueError) as e:
            messagebox.showerror("删除失败", str(e))
            return
        self.filtered_df = self._get_filtered_df()
        self._sync_sort_choices()
        self._refresh_dependent_controls()
        self._update_summary_label()
        self._refresh_table()
        self._draw_trend(warn=False)
        self._draw_bar()
        self._draw_extra()

    def _save_csv_dialog(self):
        initial = self._csv_path if self._csv_path else str(DEFAULT_CSV)
        out = filedialog.asksaveasfilename(
            title="保存当前数据为 CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")],
            initialfile=Path(initial).name,
        )
        if not out:
            return
        try:
            save_movies_csv(self.df, Path(out))
            self._csv_path = out
            self.path_label.config(text=str(Path(out).name))
            messagebox.showinfo("已保存", f"已写入：\n{out}")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _load_csv_dialog(self):
        path = filedialog.askopenfilename(
            title="选择影片 CSV",
            filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            new_df = load_movies_csv(Path(path))
        except Exception as e:
            messagebox.showerror("加载失败", str(e))
            return
        self.df = new_df
        self.filtered_df = self.df.copy()
        self._csv_path = path
        self.path_label.config(text=str(Path(path).name))
        self.genre_var.set("")
        self.region_var.set("")
        self.ymin_var.set("")
        self.ymax_var.set("")
        self.rmin_var.set("")
        self.rmax_var.set("")
        self.title_var.set("")
        genres = [""] + all_distinct_tag_labels(self.df)
        regions = [""] + sorted(self.df["region"].unique().tolist())
        self._genre_cb.configure(values=genres)
        self._region_cb.configure(values=regions)
        self._rebuild_tree_if_needed()
        self._refresh_dependent_controls()
        pick = _default_genre_multi_year(self.filtered_df)
        if pick:
            self.trend_genre.set(pick)
        self._update_summary_label()
        self._refresh_table()
        self._draw_trend(warn=False)
        self._draw_bar()
        self._draw_extra()

    def _build_tab_trend(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="② 趋势分析")

        bar = ttk.Frame(tab)
        bar.pack(fill=tk.X, padx=8, pady=6)
        ttk.Label(bar, text="类型：").pack(side=tk.LEFT)
        genres_list = all_distinct_tag_labels(self.df)
        pick = _default_genre_multi_year(self.filtered_df) or (genres_list[0] if genres_list else "")
        self.trend_genre = tk.StringVar(value=pick)
        self.trend_genre_cb = ttk.Combobox(
            bar,
            textvariable=self.trend_genre,
            values=genres_list,
            width=14,
            state="readonly",
        )
        self.trend_genre_cb.pack(side=tk.LEFT)
        ttk.Button(bar, text="绘制年度平均评分", command=lambda: self._draw_trend(warn=True)).pack(
            side=tk.LEFT, padx=10
        )

        self.trend_frame = ttk.Frame(tab)
        self.trend_frame.pack(fill=tk.BOTH, expand=True)
        self._trend_canvas = None
        self._trend_msg_label = None

        self._draw_trend(warn=False)

    def _draw_trend(self, warn=True):
        name = (self.trend_genre.get() or "").strip()
        sub = self.filtered_df.loc[mask_matches_genre(self.filtered_df, name)]
        if self._trend_canvas is not None:
            self._trend_canvas.get_tk_widget().destroy()
            self._trend_canvas = None
        if self._trend_msg_label is not None:
            self._trend_msg_label.destroy()
            self._trend_msg_label = None

        if sub.empty or len(sub.groupby("year")) < 2:
            msg = (
                "该类型在当前筛选下数据不足或仅含一个年份，无法绘制「按年的平均评分」折线。"
                "请更换类型、调整「数据浏览」中的筛选，或换用含多年份的 CSV。"
            )
            if warn:
                messagebox.showwarning("提示", msg)
            self._trend_msg_label = ttk.Label(
                self.trend_frame, text=msg, foreground="gray", wraplength=520, justify="left"
            )
            self._trend_msg_label.pack(padx=16, pady=24, anchor="w")
            return
        fig = plot_genre_rating_trend(sub, genre_label=name)
        self._trend_canvas = FigureCanvasTkAgg(fig, master=self.trend_frame)
        self._trend_canvas.draw()
        self._trend_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def _build_tab_compare(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="③ 对比统计")

        bar = ttk.Frame(tab)
        bar.pack(fill=tk.X, padx=8, pady=6)
        ttk.Label(bar, text="年份：").pack(side=tk.LEFT)
        ymax = int(self.filtered_df["year"].max()) if len(self.filtered_df) else 0
        self.year_var = tk.IntVar(value=ymax)
        years = sorted(self.filtered_df["year"].unique().tolist())
        self.year_cb = ttk.Combobox(bar, textvariable=self.year_var, values=years, width=8)
        self.year_cb.pack(side=tk.LEFT)
        ttk.Button(bar, text="柱状 Top 影片", command=self._draw_bar).pack(side=tk.LEFT, padx=8)
        ttk.Button(bar, text="类型占比饼图", command=self._draw_pie).pack(side=tk.LEFT, padx=4)

        self.cmp_frame = ttk.Frame(tab)
        self.cmp_frame.pack(fill=tk.BOTH, expand=True)
        self._cmp_canvas = None

        self._draw_bar()

    def _subset_year(self):
        y = int(self.year_var.get())
        sub = self.filtered_df[self.filtered_df["year"] == y].copy()
        if sub.empty:
            messagebox.showwarning("提示", f"{y} 年在当前筛选下无数据")
            return None
        return sub

    def _draw_bar(self):
        sub = self._subset_year()
        if sub is None:
            return
        fig = plot_top_movies_bar(sub, top_n=min(8, len(sub)))
        self._show_cmp(fig)

    def _draw_pie(self):
        sub = self._subset_year()
        if sub is None:
            return
        fig = plot_genre_share_pie(sub)
        self._show_cmp(fig)

    def _show_cmp(self, fig):
        if self._cmp_canvas:
            self._cmp_canvas.get_tk_widget().destroy()
        self._cmp_canvas = FigureCanvasTkAgg(fig, master=self.cmp_frame)
        self._cmp_canvas.draw()
        self._cmp_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def _build_tab_extra(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="④ 扩展可视化")

        bar = ttk.Frame(tab)
        bar.pack(fill=tk.X, padx=8, pady=6)
        ttk.Label(bar, text="图表：").pack(side=tk.LEFT)
        self.extra_display = tk.StringVar(value="评分 × 人数（散点）")
        self.extra_type_cb = ttk.Combobox(
            bar,
            textvariable=self.extra_display,
            values=("评分 × 人数（散点）",),
            width=22,
            state="readonly",
        )
        self.extra_type_cb.pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="绘制", command=self._draw_extra).pack(side=tk.LEFT, padx=8)

        xy_fr = ttk.Frame(tab)
        xy_fr.pack(fill=tk.X, padx=8)
        ttk.Label(xy_fr, text="横轴：").pack(side=tk.LEFT)
        self.xy_x = tk.StringVar(value="year")
        self.xy_x_cb = ttk.Combobox(xy_fr, textvariable=self.xy_x, width=12)
        self.xy_x_cb.pack(side=tk.LEFT, padx=4)
        ttk.Label(xy_fr, text="纵轴：").pack(side=tk.LEFT)
        self.xy_y = tk.StringVar(value="rating")
        self.xy_y_cb = ttk.Combobox(xy_fr, textvariable=self.xy_y, width=12)
        self.xy_y_cb.pack(side=tk.LEFT, padx=4)
        ttk.Label(
            xy_fr,
            text="（散点、直方图、地区占比饼图、自选坐标散点）",
            foreground="gray",
        ).pack(side=tk.LEFT, padx=12)

        self.extra_frame = ttk.Frame(tab)
        self.extra_frame.pack(fill=tk.BOTH, expand=True)
        self._extra_canvas = None

    def _draw_extra(self):
        kind = self._extra_kind_map.get(self.extra_display.get(), "scatter_votes")
        df = self.filtered_df
        fig = None
        if kind == "scatter_votes":
            fig = plot_rating_votes_scatter(df)
            if fig is None:
                messagebox.showwarning("提示", "当前数据缺少有效 votes 或人数为 0，无法绘制散点。")
                return
        elif kind == "histogram":
            fig = plot_rating_histogram(df)
            if fig is None:
                messagebox.showwarning("提示", "无有效评分数据。")
                return
        elif kind == "pie_region":
            fig = plot_region_share_pie(df)
            if fig is None:
                messagebox.showwarning("提示", "无地区数据。")
                return
        elif kind == "xy":
            fig = plot_xy_scatter(df, self.xy_x.get(), self.xy_y.get())
            if fig is None:
                messagebox.showwarning("提示", "所选列无效或有效点数不足。")
                return
        if fig is None:
            return
        if self._extra_canvas:
            self._extra_canvas.get_tk_widget().destroy()
        self._extra_canvas = FigureCanvasTkAgg(fig, master=self.extra_frame)
        self._extra_canvas.draw()
        self._extra_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    OfflineMovieApp().run()
