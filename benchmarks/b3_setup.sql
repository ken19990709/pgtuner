-- B3: 触发器嵌套场景库(每个场景在独立 clone 中执行,用完 release)
-- 观察:嵌套深度(pg_trigger_depth():外层语句为 0,每层 +1)、自引用递归、异常吞噬、约束违反传播
-- 修订(B3 验收 F1/F2/F3):tg_nest_level 不存在 → pg_trigger_depth();S4 改用独立表 t_sw
--   避免 t1 上三触发器互相污染;b3_rec 补基线使"二分最大深度"可行

CREATE TABLE IF NOT EXISTS t1 (id serial PRIMARY KEY, v int);
CREATE TABLE IF NOT EXISTS t2 (id serial PRIMARY KEY, v int);
CREATE TABLE IF NOT EXISTS t3 (id serial PRIMARY KEY, v int);
CREATE TABLE IF NOT EXISTS t_log (ts timestamptz DEFAULT now(), msg text, depth int);

-- S1 三级链:t1 → t2 → t3 → log(预期 depth 1/2/3)
CREATE OR REPLACE FUNCTION b3_c1() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO t2 (v) VALUES (NEW.v + 1);
  RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION b3_c2() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO t3 (v) VALUES (NEW.v + 1);
  RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION b3_c3() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO t_log (msg, depth) VALUES ('t3 hit', pg_trigger_depth());
  RETURN NEW;
END $$;
CREATE TRIGGER tr1 AFTER INSERT ON t1 FOR EACH ROW EXECUTE FUNCTION b3_c1();
CREATE TRIGGER tr2 AFTER INSERT ON t2 FOR EACH ROW EXECUTE FUNCTION b3_c2();
CREATE TRIGGER tr3 AFTER INSERT ON t3 FOR EACH ROW EXECUTE FUNCTION b3_c3();

-- S2 自引用递归:触发器向本表插入(观察递归到什么程度、报什么错)
CREATE TABLE IF NOT EXISTS t_self (id serial PRIMARY KEY, depth int);
CREATE OR REPLACE FUNCTION b3_self() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO t_self (depth) VALUES (NEW.depth + 1);
  RETURN NEW;
END $$;
CREATE TRIGGER tr_self AFTER INSERT ON t_self FOR EACH ROW EXECUTE FUNCTION b3_self();

-- S3 函数递归(与 S2 对照:PL/pgSQL 栈深 vs 触发器嵌套;有基线,可二分最大 n)
CREATE OR REPLACE FUNCTION b3_rec(n int) RETURNS int LANGUAGE plpgsql AS $$
BEGIN
  IF n <= 0 THEN
    RETURN 0;
  END IF;
  RETURN 1 + b3_rec(n - 1);
END $$;

-- S4 异常吞噬(独立表 t_sw,不与 t1 链混):嵌套触发器里违反约束但被 EXCEPTION 吃掉,外层是否感知
CREATE TABLE IF NOT EXISTS t_sw (id serial PRIMARY KEY, v int);
CREATE TABLE IF NOT EXISTS t_guard (id int PRIMARY KEY CHECK (id < 10));
CREATE OR REPLACE FUNCTION b3_swallow() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  BEGIN
    INSERT INTO t_guard VALUES (999);  -- 违反 CHECK
  EXCEPTION WHEN others THEN
    INSERT INTO t_log (msg, depth) VALUES ('swallowed in nested trigger', pg_trigger_depth());
  END;
  RETURN NEW;
END $$;
CREATE TRIGGER tr_swallow AFTER INSERT ON t_sw FOR EACH ROW EXECUTE FUNCTION b3_swallow();

-- S5 行级+语句级混合触发:语句级触发器的 depth 观测
CREATE OR REPLACE FUNCTION b3_stmt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO t_log (msg, depth) VALUES (concat('stmt trigger on ', TG_TABLE_NAME), pg_trigger_depth());
  RETURN NULL;
END $$;
CREATE TRIGGER tr1_stmt AFTER INSERT ON t1 FOR EACH STATEMENT EXECUTE FUNCTION b3_stmt();
