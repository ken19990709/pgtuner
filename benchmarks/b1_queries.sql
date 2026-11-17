-- B1: shared_buffers × 多表连接负载(模板 bench_tpl:pgbench -s 10 + history 装载 1M 行)
-- 设计目标:三档工作集横跨 shared_buffers 候选(128M/512M/1G)
-- 教训注:不 join pgbench_tellers(tellers×accounts 会把行数放大 10 倍,语义噪声)

-- Q1 两表 join + 聚合,命中 ~20 万 accounts(~26MB 工作集)
SELECT b.bid, b.bbalance, count(*) AS n, sum(a.abalance) AS total
FROM pgbench_branches b
JOIN pgbench_accounts a ON a.bid = b.bid
WHERE a.aid BETWEEN 1 AND 200000
GROUP BY 1, 2 ORDER BY 1;

-- Q2 history × accounts join(history 由模板装载 1M 行,aid 命中 ~40 万)
SELECT count(*) AS n, sum(h.delta) AS total
FROM pgbench_history h
JOIN pgbench_accounts a ON a.aid = h.aid
WHERE h.aid BETWEEN 400000 AND 800000;

-- Q3 全表聚合扫描(1M 行,~130MB)
SELECT a.bid, avg(a.abalance), min(a.abalance), max(a.abalance)
FROM pgbench_accounts a
WHERE a.aid % 10 = 0
GROUP BY 1 ORDER BY 1;
