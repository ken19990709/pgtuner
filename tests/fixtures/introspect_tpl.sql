-- sql/schema 真库验收模板:覆盖 表/分区表/视图/物化视图/多种索引/约束/触发器/注释/怪类型
-- 基准数据:branches 50 行 / accounts 20000 行(触发器联动 audit_log 20000 行) /
--          events 分区父表 8000 行(两个叶子 5000+3000) / empty_probe 0 行
CREATE TABLE branches (
  bid int PRIMARY KEY,
  bbalance int NOT NULL DEFAULT 0,
  filler char(88)
);
COMMENT ON TABLE branches IS 'pgbench 网点';

CREATE TABLE accounts (
  aid int PRIMARY KEY,
  bid int NOT NULL REFERENCES branches(bid),
  abalance int NOT NULL DEFAULT 0 CHECK (abalance >= -1000000),
  kind text NOT NULL DEFAULT 'std',
  tags text[],
  meta jsonb,
  filler char(84)
);
COMMENT ON COLUMN accounts.aid IS '账户主键';
CREATE INDEX idx_accounts_bid ON accounts(bid);
CREATE UNIQUE INDEX idx_accounts_kind_abalance ON accounts(kind, abalance) WHERE abalance <> 0;
CREATE INDEX idx_accounts_meta_gin ON accounts USING gin(meta);

CREATE TABLE events (
  id bigint GENERATED ALWAYS AS IDENTITY,
  ts timestamptz NOT NULL,
  payload text
) PARTITION BY RANGE (ts);
CREATE TABLE events_2026_01 PARTITION OF events FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
CREATE TABLE events_2026_02 PARTITION OF events FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
CREATE INDEX idx_events_ts ON events(ts);

CREATE TABLE audit_log (
  id int PRIMARY KEY,
  note text NOT NULL,
  created timestamptz DEFAULT now()
);
CREATE FUNCTION accounts_audit() RETURNS trigger AS $$
BEGIN
  INSERT INTO audit_log(id, note) VALUES (NEW.aid, 'changed; semicolons; everywhere');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_accounts_audit AFTER INSERT OR UPDATE ON accounts
  FOR EACH ROW EXECUTE FUNCTION accounts_audit();

CREATE VIEW v_branch_balance AS
  SELECT b.bid, sum(a.abalance) AS total
  FROM branches b JOIN accounts a ON a.bid = b.bid GROUP BY b.bid;
CREATE MATERIALIZED VIEW mv_kind_count AS SELECT kind, count(*) AS n FROM accounts GROUP BY kind;

CREATE TABLE empty_probe (id int PRIMARY KEY);

INSERT INTO branches SELECT g, 0, 'x' FROM generate_series(1, 50) g;
INSERT INTO accounts
  SELECT g, ((g - 1) % 50) + 1, (g * 37) % 200000 - 100,
         (ARRAY['std', 'vip'])[1 + g % 2],
         ARRAY['t' || g],
         jsonb_build_object('k', g, 'nested', jsonb_build_object('deep', true)),
         'f'
  FROM generate_series(1, 20000) g;
INSERT INTO events (ts, payload)
  SELECT timestamptz '2026-01-15' + (g || ' minutes')::interval, 'p' || g FROM generate_series(1, 5000) g;
INSERT INTO events (ts, payload)
  SELECT timestamptz '2026-02-15' + (g || ' minutes')::interval, 'p' || g FROM generate_series(1, 3000) g;
ANALYZE;
