"""积分与 Buchholz 分计算。

积分以「半分」为单位的整数存储与计算，对外输出时除以 2。
- 胜 = 1，和 = 0.5，负 = 0；
- 轮空按胜场计 1 分，且轮空不计对手 Buchholz 贡献（0）；
- 赛务争议扣分（point_deduction_entries 中仍生效的扣分净额）从
  对局积分中扣除得到「排名积分」，排名积分允许为负；
- 排序、Buchholz（对手排名积分之和）以及下一轮积分分组均以
  排名积分为准。
"""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import Match, Participant, PointDeductionEntry, Round

# 账本行类型 / 状态
DEDUCTION = "deduction"
REVOCATION = "revocation"
EFFECTIVE = "effective"
REVOKED = "revoked"


@dataclass
class Record:
    participant_id: int
    number: int
    name: str
    # 排名积分 = 对局积分 - 有效扣分（半分单位，可为负）：排序、
    # Buchholz 与下一轮分组均以此为准
    points: int
    # 对局积分（胜/和/负/轮空累计，不含扣分）
    game_points: int
    # 有效扣分净额（半分单位，恒非负）
    deduction: int
    buchholz: int        # 对手排名积分之和（半分单位）
    played: int          # 实际对局数（不含轮空）
    byes: int            # 轮空次数


def effective_deductions(db: Session, tournament_id: int) -> dict[int, int]:
    """每位参赛者当前仍生效的扣分净额（半分单位）。

    deduction 金额之和减去 revocation 金额之和；正常账本下结果
    恒非负（撤销只能针对仍生效的扣分，且不能重复撤销）。
    """
    entries = (
        db.query(PointDeductionEntry)
        .filter(PointDeductionEntry.tournament_id == tournament_id)
        .all()
    )
    totals: dict[int, int] = {}
    for e in entries:
        totals.setdefault(e.participant_id, 0)
        if e.entry_type == DEDUCTION:
            totals[e.participant_id] += e.amount_halves
        elif e.entry_type == REVOCATION:
            totals[e.participant_id] -= e.amount_halves
    return {pid: amount for pid, amount in totals.items() if amount > 0}


def compute_records(
    db: Session,
    tournament_id: int,
    include_open: bool = False,
) -> list[Record]:
    """计算成绩。

    include_open=False（编排下一轮时使用）：只统计已结束轮次。
    include_open=True（排名查询使用）：同时实时计入进行中轮次里
    已经录入的对局与在生成轮次时即确定的轮空。
    """
    participants = (
        db.query(Participant)
        .filter(Participant.tournament_id == tournament_id)
        .order_by(Participant.number)
        .all()
    )
    rounds = (
        db.query(Round)
        .filter(Round.tournament_id == tournament_id)
        .all()
    )
    if include_open:
        round_ids = {r.id for r in rounds}
    else:
        round_ids = {r.id for r in rounds if r.status == "closed"}

    matches = (
        db.query(Match)
        .filter(Match.round_id.in_(round_ids))
        .all()
        if round_ids
        else []
    )

    game_points = {p.id: 0 for p in participants}
    played = {p.id: 0 for p in participants}
    byes = {p.id: 0 for p in participants}
    # 每位选手实际对阵过的对手编号（轮空不含）
    opponents: dict[int, list[int]] = {p.id: [] for p in participants}

    for m in matches:
        if m.player_black_id is None:
            # 轮空：轮空方得 1 分，无对手
            if m.player_white_id in game_points:
                game_points[m.player_white_id] += m.score_white or 0
                byes[m.player_white_id] += 1
            continue
        game_points[m.player_white_id] += m.score_white or 0
        game_points[m.player_black_id] += m.score_black or 0
        played[m.player_white_id] += 1
        played[m.player_black_id] += 1
        opponents[m.player_white_id].append(m.player_black_id)
        opponents[m.player_black_id].append(m.player_white_id)

    # 有效扣分与排名积分（可为负）
    deductions = effective_deductions(db, tournament_id)
    ranking_points = {
        p.id: game_points[p.id] - deductions.get(p.id, 0) for p in participants
    }

    # Buchholz：实际对手的当前排名积分之和（扣分后、可为负；轮空无
    # 对手，不计贡献）。点数组已固定，两遍计算。
    records: list[Record] = []
    for p in participants:
        buch = sum(ranking_points[o] for o in opponents[p.id])
        records.append(
            Record(
                participant_id=p.id,
                number=p.number,
                name=p.name,
                points=ranking_points[p.id],
                game_points=game_points[p.id],
                deduction=deductions.get(p.id, 0),
                buchholz=buch,
                played=played[p.id],
                byes=byes[p.id],
            )
        )

    # 排名：排名积分降序 -> Buchholz 降序 -> 编号升序
    records.sort(key=lambda r: (-r.points, -r.buchholz, r.number))
    return records


def half_to_float(v: int) -> float:
    return v / 2.0
