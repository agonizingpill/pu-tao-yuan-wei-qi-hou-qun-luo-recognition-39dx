from datetime import timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from .db import event
from .domain import GRANT_STATES, MINUTES, Profile, RoundState
from .fieldbook import one

CST = ZoneInfo('Asia/Shanghai')
GRANT_LIST = ','.join(f"'{state}'" for state in GRANT_STATES)

ROUND_VIEW = '''
    SELECT r.*,b.code AS block_code,b.name AS block_name,l.profile,l.revision,
           a.profile AS active_profile, a.revision AS active_revision,
           p.profile AS proposed_profile, p.run_id AS pending_run_id,
           coalesce(p.profile<>l.profile AND r.state='scheduled', false) AS review_hold,
           q.queue_pos
    FROM irrigation_round r JOIN vine_block b ON r.block_id=b.id
    JOIN climate_label l ON r.label_id=l.id
    LEFT JOIN climate_label a ON a.block_id=b.id AND a.state='active'
    LEFT JOIN climate_label p ON p.block_id=b.id AND p.state='proposed'
    LEFT JOIN (
        SELECT id, row_number() OVER (PARTITION BY quota_day
                                      ORDER BY warm_probability DESC NULLS LAST, applied_at, id) AS queue_pos
        FROM irrigation_round WHERE state='waitlisted'
    ) q ON q.id=r.id
'''


def rounds(db):
    return db.execute(ROUND_VIEW + ' ORDER BY r.starts_at,r.code').fetchall()


def existing_round(db, round_id):
    return one(db, ROUND_VIEW + ' WHERE r.id=%s', (round_id,), '灌溉轮次')


def scheduling_basis(db, block_id):
    block = one(db, '''
        SELECT b.code,l.id AS label_id,l.profile,l.warm_probability,p.profile AS proposed_profile
        FROM vine_block b LEFT JOIN climate_label l ON b.id=l.block_id AND l.state='active'
        LEFT JOIN climate_label p ON p.block_id=b.id AND p.state='proposed' WHERE b.id=%s
    ''', (block_id,), '地块')
    if block['proposed_profile'] and block['proposed_profile'] != block['profile']:
        raise HTTPException(409, '新标签需要主管确认，暂缓编排该地块')
    if not block['label_id']:
        raise HTTPException(409, '请先为该地块生成生效群落标签')
    return block


def require_free_slot(db, block_id, begins, minutes, excluding=0):
    ends = begins + timedelta(minutes=minutes)
    if db.execute('''
        SELECT 1 FROM irrigation_round
        WHERE block_id=%s AND id<>%s AND state<>'cancelled'
          AND starts_at < %s AND starts_at + minutes * interval '1 minute' > %s
    ''', (block_id, excluding, ends, begins)).fetchone():
        raise HTTPException(409, '同一地块的灌溉时段重叠')


def quota_day(starts_at):
    return starts_at.astimezone(CST).date()


def used_minutes(db, day):
    return db.execute(f'''
        SELECT coalesce(sum(minutes),0) AS used FROM irrigation_round
        WHERE quota_day=%s AND state IN ({GRANT_LIST})
    ''', (day,)).fetchone()['used']


def quota_status(db, day):
    quota = db.execute('SELECT * FROM daily_quota WHERE day=%s', (day,)).fetchone()
    waiting = db.execute('''
        SELECT count(*) AS n FROM irrigation_round WHERE quota_day=%s AND state='waitlisted'
    ''', (day,)).fetchone()['n']
    return dict(day=day, cap_minutes=quota and quota['cap_minutes'], used_minutes=used_minutes(db, day),
                waitlisted=waiting, actor=quota and quota['actor'],
                updated_at=quota and quota['updated_at'])


def quota_list(db):
    return db.execute(f'''
        SELECT q.day,q.cap_minutes,q.actor,q.updated_at,
               coalesce(sum(r.minutes) FILTER (WHERE r.state IN ({GRANT_LIST})),0) AS used_minutes,
               count(r.id) FILTER (WHERE r.state='waitlisted') AS waitlisted
        FROM daily_quota q LEFT JOIN irrigation_round r ON r.quota_day=q.day
        GROUP BY q.day,q.cap_minutes,q.actor,q.updated_at ORDER BY q.day DESC
    ''').fetchall()


def promote(db, day, actor):
    """按候补顺序递补当天额度。行级状态守卫保证递补失败重试不会重复占用。"""
    quota = db.execute('SELECT cap_minutes FROM daily_quota WHERE day=%s FOR UPDATE', (day,)).fetchone()
    if not quota:
        return []
    cap, used = quota['cap_minutes'], used_minutes(db, day)
    waiting = db.execute('''
        SELECT id,code,minutes FROM irrigation_round
        WHERE quota_day=%s AND state='waitlisted'
        ORDER BY warm_probability DESC NULLS LAST, applied_at, id
    ''', (day,)).fetchall()
    promoted = []
    for row in waiting:
        if used + row['minutes'] > cap:
            break  # 严格按队列顺序递补，队首凑不齐时后续不插队
        granted = db.execute('''
            UPDATE irrigation_round SET state='scheduled', granted_at=now()
            WHERE id=%s AND state='waitlisted' RETURNING id,code,minutes
        ''', (row['id'],)).fetchone()
        if granted:
            used += granted['minutes']
            promoted.append(granted)
    if promoted:
        event(db, actor, 'quota_promoted', str(day), codes=[row['code'] for row in promoted],
              used_minutes=used, cap_minutes=cap)
    return promoted


def set_quota(db, day, cap_minutes, actor):
    previous = db.execute('SELECT cap_minutes FROM daily_quota WHERE day=%s FOR UPDATE', (day,)).fetchone()
    db.execute('''
        INSERT INTO daily_quota(day,cap_minutes,actor) VALUES (%s,%s,%s)
        ON CONFLICT (day) DO UPDATE SET cap_minutes=EXCLUDED.cap_minutes,actor=EXCLUDED.actor,updated_at=now()
    ''', (day, cap_minutes, actor))
    event(db, actor, 'quota_set', str(day), cap_minutes=cap_minutes,
          previous_cap=previous and previous['cap_minutes'])
    promoted = promote(db, day, actor)
    return dict(quota_status(db, day), promoted=[row['code'] for row in promoted])


def schedule(db, data, actor):
    basis = scheduling_basis(db, data.block_id)
    minutes = MINUTES[Profile(basis['profile'])]
    require_free_slot(db, data.block_id, data.starts_at, minutes)
    day = quota_day(data.starts_at)
    warm = basis['warm_probability']
    state, granted = 'scheduled', 'now()'
    quota = db.execute('SELECT cap_minutes FROM daily_quota WHERE day=%s FOR UPDATE', (day,)).fetchone()
    used = None
    if quota:
        used = used_minutes(db, day)
        if used + minutes > quota['cap_minutes']:
            state, granted = 'waitlisted', 'NULL'
    row = db.execute(f'''
        INSERT INTO irrigation_round(code,block_id,original_label_id,label_id,starts_at,minutes,state,
                                     quota_day,warm_probability,granted_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,{granted}) RETURNING id
    ''', (data.code, data.block_id, basis['label_id'], basis['label_id'], data.starts_at, minutes,
          state, day, warm)).fetchone()
    event(db, actor, 'irrigation_waitlisted' if state == 'waitlisted' else 'irrigation_scheduled', data.code,
          label_id=basis['label_id'], minutes=minutes, starts_at=data.starts_at.isoformat(),
          quota_day=day.isoformat(), warm_probability=warm,
          cap_minutes=quota and quota['cap_minutes'], used_minutes=used)
    return existing_round(db, row['id'])


def replan(db, round_id, data, actor):
    row = existing_round(db, round_id)
    if row['state'] != RoundState.NEEDS_REPLAN:
        raise HTTPException(409, '只有确认受影响的轮次可重新排灌')
    basis = scheduling_basis(db, row['block_id'])
    minutes = MINUTES[Profile(basis['profile'])]
    require_free_slot(db, row['block_id'], data.starts_at, minutes, round_id)
    old_day, new_day = row['quota_day'], quota_day(data.starts_at)
    db.execute('''
        UPDATE irrigation_round SET label_id=%s, starts_at=%s,minutes=%s,state='scheduled',quota_day=%s WHERE id=%s
    ''', (basis['label_id'], data.starts_at, minutes, new_day, round_id))
    quota = db.execute('SELECT cap_minutes FROM daily_quota WHERE day=%s FOR UPDATE', (new_day,)).fetchone()
    if quota and used_minutes(db, new_day) > quota['cap_minutes']:
        raise HTTPException(409, '改排后超出当天限供上限，请先调整上限或取消其他轮次')
    event(db, actor, 'irrigation_replanned', row['code'], reason=data.reason,
          previous_label_id=row['label_id'], previous_minutes=row['minutes'], previous_start=row['starts_at'].isoformat(),
          label_id=basis['label_id'], minutes=minutes, starts_at=data.starts_at.isoformat())
    for day in {old_day, new_day} - {None}:
        promote(db, day, actor)
    return existing_round(db, round_id)


def finish(db, round_id, completed_at, actor):
    row = existing_round(db, round_id)
    if row['state'] != RoundState.SCHEDULED or row['review_hold']:
        raise HTTPException(409, '轮次须处于可执行的已排状态，待确认或改排轮次不能登记完灌')
    if completed_at < row['starts_at'] + timedelta(minutes=row['minutes']):
        raise HTTPException(409, '完灌时间早于排定结束时间')
    db.execute("UPDATE irrigation_round SET state='completed',completed_at=%s WHERE id=%s",
               (completed_at, round_id))
    event(db, actor, 'irrigation_completed', row['code'], completed_at=completed_at.isoformat())
    return existing_round(db, round_id)


def cancel(db, round_id, reason, actor):
    row = existing_round(db, round_id)
    if row['state'] not in (RoundState.SCHEDULED, RoundState.NEEDS_REPLAN, RoundState.WAITLISTED):
        raise HTTPException(409, '该轮次已完灌或已取消')
    held_quota = row['state'] in GRANT_STATES
    db.execute("UPDATE irrigation_round SET state='cancelled' WHERE id=%s", (round_id,))
    event(db, actor, 'irrigation_cancelled', row['code'], reason=reason,
          freed_minutes=row['minutes'] if held_quota else 0, quota_day=row['quota_day'] and row['quota_day'].isoformat())
    if held_quota and row['quota_day']:
        promote(db, row['quota_day'], actor)  # 取消腾出的分钟按当前候补顺序递补
    return existing_round(db, round_id)


def observe(db, data, actor):
    basis = existing_round(db, data.round_id)
    if basis['state'] != RoundState.COMPLETED or data.observed_at < basis['completed_at']:
        raise HTTPException(409, '采收观察需要关联已完灌轮次，且观察时间不能早于完灌')
    row = db.execute('''
        INSERT INTO harvest_note(round_id,observed_at,brix,berry_count,note,actor)
        VALUES (%s,%s,%s,%s,%s,%s) RETURNING *
    ''', (data.round_id, data.observed_at, data.brix, data.berry_count, data.note, actor)).fetchone()
    event(db, actor, 'harvest_observed', basis['code'], note_id=row['id'], label_id=basis['label_id'])
    return row


def observations(db):
    return db.execute('''
        SELECT h.*,r.code AS round_code,r.completed_at,b.code AS block_code,b.name AS block_name,
               l.profile,l.revision
        FROM harvest_note h JOIN irrigation_round r ON h.round_id=r.id
        JOIN vine_block b ON r.block_id=b.id JOIN climate_label l ON r.label_id=l.id
        ORDER BY h.observed_at DESC,h.id DESC
    ''').fetchall()
