-- bench_tpl 模板装载脚本:在 pgbench -i -s 10 之后执行
-- ① 填充 pgbench_history(新版 pgbench -i 不填;确定性 1M 行,aid 均匀覆盖)
-- ② 装载 B2 实验表(b2_t,见 b2_setup.sql;同样不 ANALYZE——缺统计是对照变量)

TRUNCATE pgbench_history;
INSERT INTO pgbench_history (tid, bid, aid, delta, mtime)
SELECT (g % 100) + 1,
       (g % 10) + 1,
       ((g::bigint * 7919) % 1000000) + 1,        -- 确定性打散,覆盖全部 aid(int4 会溢出,须 bigint)
       (g * 13) % 1000 - 500,
       timestamp '2026-01-01' + (g % 86400) * interval '1 second'
FROM generate_series(1, 1000000) AS g;

CREATE TABLE IF NOT EXISTS b2_t (
    id  int PRIMARY KEY,
    k   int NOT NULL,
    pad text NOT NULL DEFAULT repeat('x', 64)
);
INSERT INTO b2_t (id, k)
SELECT g, g FROM generate_series(1, 200000) AS g
ON CONFLICT DO NOTHING;

-- B2 验收缺陷 D6:autovacuum 会给 b2_t 补统计,打破"缺统计"对照变量。
-- 关闭该表 autovacuum(reloption 随 TEMPLATE 克隆继承),使所有克隆保持"未 ANALYZE"状态。
ALTER TABLE b2_t SET (autovacuum_enabled = false);
