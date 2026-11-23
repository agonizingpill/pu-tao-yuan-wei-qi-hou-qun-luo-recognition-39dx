from enum import StrEnum


class Role(StrEnum):
    OPERATOR = 'operator'
    SUPERVISOR = 'supervisor'
    VIEWER = 'viewer'


class Slot(StrEnum):
    DAY = 'day'
    NIGHT = 'night'


class Profile(StrEnum):
    COOL = 'cool'
    BALANCED = 'balanced'
    WARM = 'warm'


class RoundState(StrEnum):
    SCHEDULED = 'scheduled'
    WAITLISTED = 'waitlisted'
    NEEDS_REPLAN = 'needs_replan'
    COMPLETED = 'completed'
    CANCELLED = 'cancelled'


MINUTES = {Profile.COOL: 20, Profile.BALANCED: 35, Profile.WARM: 50}

# 占用当天限供额度的轮次状态：已排、待改排与已完灌都继续占额，取消才腾出分钟
GRANT_STATES = (RoundState.SCHEDULED, RoundState.NEEDS_REPLAN, RoundState.COMPLETED)
