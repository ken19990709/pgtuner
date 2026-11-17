-- B2 查询集:UNION ALL 分支选择度三档 + 单扫描对照
-- 期望:ANALYZE 后各分支 Plan Rows ≈ Actual Rows(log2 误差 < 1);ANALYZE 前误差显著
-- 用法:pgtuner planbench 本文件 --accuracy --dsn ... 
--      再跑变体:--variant post_analyze="ANALYZE b2_t"(对比缺统计 vs 有统计)

-- 窄分支:每支 100 行
SELECT count(*), sum(k) FROM (
  SELECT k FROM b2_t WHERE k BETWEEN 1000 AND 1099
  UNION ALL
  SELECT k FROM b2_t WHERE k BETWEEN 100000 AND 100099
) u;

-- 中分支:每支 1000 行
SELECT count(*), sum(k) FROM (
  SELECT k FROM b2_t WHERE k BETWEEN 1000 AND 1999
  UNION ALL
  SELECT k FROM b2_t WHERE k BETWEEN 100000 AND 100999
) u;

-- 宽分支:每支 10000 行
SELECT count(*), sum(k) FROM (
  SELECT k FROM b2_t WHERE k BETWEEN 1 AND 10000
  UNION ALL
  SELECT k FROM b2_t WHERE k BETWEEN 50001 AND 60000
) u;

-- 对照:同总行数的 OR 单扫描(plan 形态应与 UNION ALL 不同)
SELECT count(*), sum(k) FROM b2_t
WHERE (k BETWEEN 1 AND 10000) OR (k BETWEEN 50001 AND 60000);
