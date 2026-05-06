-- MySQL：豆瓣电影（含年份 / 地区 / 类型标签）
CREATE DATABASE IF NOT EXISTS movie_douban DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE movie_douban;

DROP TABLE IF EXISTS douban_chart_movies;
CREATE TABLE douban_chart_movies (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  subject_id VARCHAR(32) NOT NULL,
  title VARCHAR(512) NOT NULL,
  release_year SMALLINT UNSIGNED DEFAULT NULL COMMENT '上映年份',
  region VARCHAR(64) DEFAULT NULL COMMENT '国家/地区',
  tags VARCHAR(512) DEFAULT NULL COMMENT '类型标签，逗号分隔',
  rating DECIMAL(4,1) DEFAULT NULL,
  votes INT DEFAULT NULL,
  chart_rank SMALLINT UNSIGNED DEFAULT NULL,
  page_url VARCHAR(512) DEFAULT '',
  poster_url VARCHAR(512) DEFAULT NULL COMMENT '海报 URL',
  poster_local VARCHAR(512) DEFAULT NULL COMMENT '本地海报路径',
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
  KEY idx_year (release_year),
  KEY idx_region (region),
  KEY idx_crawled (crawled_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

DROP TABLE IF EXISTS crawl_log;
CREATE TABLE crawl_log (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  rows_inserted INT DEFAULT 0,
  message VARCHAR(512) DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
