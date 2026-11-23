-- 水泵限供：当日滴灌分钟上限与候补队列
CREATE TABLE IF NOT EXISTS daily_quota (
    day date PRIMARY KEY,
    cap_minutes integer NOT NULL CHECK (cap_minutes BETWEEN 1 AND 1440),
    actor text NOT NULL REFERENCES account(username),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 每个标签版本记录生效模型给出的暖干后验概率，作为限供排队依据
ALTER TABLE climate_label ADD COLUMN IF NOT EXISTS warm_probability double precision
    CHECK (warm_probability BETWEEN 0 AND 1);

-- 轮次携带限供排队信息：所属限供日、申请时暖干概率、申请与领额时间
ALTER TABLE irrigation_round ADD COLUMN IF NOT EXISTS quota_day date;
ALTER TABLE irrigation_round ADD COLUMN IF NOT EXISTS warm_probability double precision
    CHECK (warm_probability BETWEEN 0 AND 1);
ALTER TABLE irrigation_round ADD COLUMN IF NOT EXISTS applied_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE irrigation_round ADD COLUMN IF NOT EXISTS granted_at timestamptz;

-- 轮次状态增加候补
ALTER TABLE irrigation_round DROP CONSTRAINT IF EXISTS irrigation_round_state_check;
ALTER TABLE irrigation_round ADD CONSTRAINT irrigation_round_state_check
    CHECK (state IN ('scheduled', 'needs_replan', 'completed', 'cancelled', 'waitlisted'));

-- 存量数据回填：限供日按中国标准时间归属，排队分取所依据标签的暖干概率
UPDATE irrigation_round SET quota_day = (starts_at AT TIME ZONE 'Asia/Shanghai')::date
WHERE quota_day IS NULL;
UPDATE irrigation_round r SET warm_probability = l.warm_probability
FROM climate_label l WHERE l.id = r.label_id AND r.warm_probability IS NULL;

CREATE INDEX IF NOT EXISTS irrigation_round_quota_day ON irrigation_round (quota_day)
    WHERE state <> 'cancelled';
