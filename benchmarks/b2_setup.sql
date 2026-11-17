-- B2: UNION ALL 代价估算准确性实验的数据与查询载荷
-- 用法:克隆 bench_tpl → 连上后执行本文件(或并入模板)→ pgtuner planbench b2_queries.sql --accuracy

CREATE TABLE IF NOT EXISTS b2_t (
    id  int PRIMARY KEY,
    k   int NOT NULL,
    pad text NOT NULL DEFAULT repeat('x', 64)
);

-- 20 万行,k 与 id 同分布(1..200000 连续,便于精确推算各分支真实行数)
INSERT INTO b2_t (id, k)
SELECT g, g
FROM generate_series(1, 200000) AS g
ON CONFLICT DO NOTHING;

-- 注意:刻意不在这里 ANALYZE——"缺统计"本身是本实验的对照组之一(post_analyze 变体)
