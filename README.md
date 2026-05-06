# 豆映集 · 豆瓣电影数据分析

离线统计（Tkinter + pandas + matplotlib）与 Web 站点（Flask + MySQL + 爬虫）双端项目：离线侧处理本地 CSV；Web 侧抓取豆瓣榜单等信息入库，并提供列表、详情与统计图。

---

## 功能概览

| 模块 | 说明 |
|------|------|
| **离线 `offline_app`** | 多标签页：① 数据浏览（筛选、排序、表格）、② 趋势分析（按类型 × 年份折线）、③ 对比统计（年度 Top 柱状、类型/地区饼图）、④ 扩展可视化（散点、直方图、自选坐标轴等）。类型筛选支持 `tags` 多标签匹配。可删除行、保存为 CSV。 |
| **Web `web_app`** | 首页快捷入口；影片库（筛选、排序）；影片详情；数据分析（KPI + 四宫格图）；可视化扩展（散点、直方图、数据来源占比、自选轴等）；抓取设置（多数据源、异步进度）；影片库/分析页可导出 CSV。 |

---

## 环境要求

- Python **3.10+**（建议 3.12）
- **MySQL 8.x**（仅运行 Web 端时需要）
- 抓取时需能访问豆瓣电影相关网页；请适度请求频率并遵守对方站点规则。

---

## 安装依赖

在项目根目录执行：

```bash
python -m pip install -r requirements.txt
```

---

## MySQL（Web 端）

1. 用 MySQL 客户端执行 **`web_app/schema.sql`**，创建数据库 **`movie_douban`** 及表（含 `douban_chart_movies`、`crawl_log` 等）。
2. 连接参数通过**环境变量**覆盖默认值即可（以下为可选变量及默认）：

| 变量 | 默认 |
|------|------|
| `MYSQL_HOST` | `127.0.0.1` |
| `MYSQL_PORT` | `3306` |
| `MYSQL_USER` | `root` |
| `MYSQL_PASSWORD` | 空 |
| `MYSQL_DB` | `movie_douban` |

PowerShell 示例（当前会话有效；若数据库需要认证，再设置 `MYSQL_PASSWORD`）：

```powershell
$env:MYSQL_USER = "root"
$env:MYSQL_DB = "movie_douban"
```

Linux / macOS：

```bash
export MYSQL_USER=root
export MYSQL_DB=movie_douban
```

---

## 运行方式

均在**项目根目录**执行。

### 离线版

```bash
python -m offline_app.main
```

- 默认读取 `offline_app/data_io.py` 里 **`DEFAULT_CSV`** 指向的 CSV；也可用界面 **「从 CSV 加载」** 换文件。
- CSV 需含 **`title`**；支持 Web 导出列名（如 `release_year`、`tags`）与简易档案列名（如 `year`、`genre`）。

### Web 版

```bash
python -m web_app.app
```

浏览器打开：**http://127.0.0.1:5000**。

**建议流程**：配置好 MySQL 并执行 `schema.sql` → 设置连接相关环境变量 → 启动应用 → 打开 **「抓取设置」** 执行一次入库 → 再使用 **「影片库」「数据分析」「可视化扩展」**。

**主要页面（路径）**

| 页面 | 路径 | 作用 |
|------|------|------|
| 首页 | `/` | 库内条数、快捷入口、最新入库预览 |
| 影片库 | `/movies` | 筛选、排序、海报卡片、进详情 |
| 影片详情 | `/movie/<subject_id>` | 单条字段展示 |
| 数据分析 | `/stats` | 多选年份/标签筛选，KPI + 四宫格图，可导出 CSV |
| 可视化扩展 | `/viz` | 入口：散点、数据来源占比、评分直方图、自选 XY 等子页 |
| 抓取设置 | `/fetch` | 选择数据源与参数，提交后抓取入库（页面内进度提示） |

**抓取说明（摘要）**：数据源可选排行榜多版块、Top250、关键词搜索、探索 JSON 等；具体字段与筛选逻辑以表单为准。网络或解析异常时，程序可能写入内置备用数据以便界面非空。

---

## 目录结构（主要）

```
DouYingJi/
├── offline_app/           # 离线入口 main.py，数据 data_io.py，图表 charts.py
├── web_app/
│   ├── app.py             # Flask 路由
│   ├── db.py              # MySQL 访问
│   ├── crawler.py         # 抓取与解析
│   ├── config.py          # 连接配置（可被环境变量覆盖）
│   ├── schema.sql
│   ├── templates/
│   └── static/
├── data/offline/          # 离线示例或自备 CSV
├── requirements.txt
└── README.md
```

---

## 合规说明

本项目仅供学习与交流；请勿高频抓取或用于给对方站点造成压力的场景；影片与文案版权归权利人及提供方所有。
