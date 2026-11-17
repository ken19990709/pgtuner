-- B4: stats-drift(统计漂移)场景数据 —— 模板 stale_tpl(benchmark 任务 stats-drift 用)
-- 确定性设计(无时钟/无随机函数):
--   批1 50万行:kind 99% 'noise' / 1% 'rare' → ANALYZE(规划器学会 rare≈5000/50万)
--   批2 50万行:rare/noise 逐行交错(模拟线上持续写入)→ 刻意不再 ANALYZE
--   表级关 autovacuum → 漂移冻结进模板,克隆原样继承
-- 受害查询(events JOIN users WHERE kind='rare'):估算 ~500 行 / 实际 25.3万 →
-- 规划器选 nested loop(外层 events 走 kind 索引,内层逐行探 users 主键),实际
-- 25.3万次随机内层探测;ANALYZE 后估算回真 → hash join(两侧顺序扫)。这是生产
-- "突慢"的头号成因(估算差驱动 join 方法翻转),时差远大于单表扫描翻转。
-- 三个关键参数(改动前先想清楚,都是实测调出来的):
--   users 50万行宽表:hash 建表侧代价大,估算 ~500 行时 NL 稳赢(确定性翻转);
--   events.user_id = 素数乘子伪随机铺满 users:g % 小数 会让引用局部化,NL 实测
--     探测全落缓存,时差被抹平;
--   批1 rare 密度 0.1%(est ~500):密度 1% 时估算 ~1 万行,hash 仍更便宜,不翻转。
CREATE TABLE users (id int PRIMARY KEY, region text NOT NULL, note text NOT NULL);
INSERT INTO users SELECT g, 'region-' || (g % 8), repeat('u', 150) || g
FROM generate_series(1, 500000) g;
CREATE TABLE events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id int NOT NULL,
  kind text NOT NULL,
  payload text NOT NULL,
  created_at timestamptz NOT NULL
);
CREATE INDEX events_kind_idx ON events (kind);
INSERT INTO events (user_id, kind, payload, created_at)
SELECT 1 + (g::bigint * 7919) % 500000,
       CASE WHEN g % 1000 = 0 THEN 'rare' ELSE 'noise' END,
       repeat('x', 150) || g,
       timestamptz '2026-01-01' + make_interval(secs => g % 86400)
FROM generate_series(1, 500000) g;
ANALYZE events;
INSERT INTO events (user_id, kind, payload, created_at)
SELECT 1 + (g::bigint * 7919) % 500000,
       CASE WHEN g % 2 = 0 THEN 'rare' ELSE 'noise' END,
       repeat('y', 150) || g,
       timestamptz '2026-02-01' + make_interval(secs => g % 86400)
FROM generate_series(1, 500000) g;
ALTER TABLE events SET (autovacuum_enabled = false);
