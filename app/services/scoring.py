"""积分与 Buchholz 分计算。

积分以「半分」为单位的整数存储与计算，对外输出时除以 2。
- 胜 = 1，和 = 0.5，负 = 0；
- 轮空按胜场计 1 分，且轮空不计对手 Buchholz 贡献（0）。
"""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import Match, Participant, Round
from .swiss import WIN


@dataclass
class Record:
    participant_id: int
    number: int
    name: str
    points: int          # 半分单位
    buchholz: int        # 对手累计积分之和（半分单位）
    played: int          # 实际对局数（不含轮空）
    byes: int            # 轮空次数


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

    points = {p.id: 0 for p in participants}
    played = {p.id: 0 for p in participants}
    byes = {p.id: 0 for p in participants}
    # 每位选手实际对阵过的对手编号（轮空不含）
    opponents: dict[int, list[int]] = {p.id: [] for p in participants}

    for m in matches:
        if m.player_black_id is None:
            # 轮空：轮空方得 1 分，无对手
            if m.player_white_id in points:
                points[m.player_white_id] += m.score_white or 0
                byes[m.player_white_id] += 1
            continue
        points[m.player_white_id] += m.score_white or 0
        points[m.player_black_id] += m.score_black or 0
        played[m.player_white_id] += 1
        played[m.player_black_id] += 1
        opponents[m.player_white_id].append(m.player_black_id)
        opponents[m.player_black_id].append(m.player_white_id)

    # Buchholz：实际对手的当前积分之和（两遍计算，点数组已固定）
    records: list[Record] = []
    for p in participants:
        buch = sum(points[o] for o in opponents[p.id])
        records.append(
            Record(
                participant_id=p.id,
                number=p.number,
                name=p.name,
                points=points[p.id],
                buchholz=buch,
                played=played[p.id],
                byes=byes[p.id],
            )
        )

    # 排名：积分降序 -> Buchholz 降序 -> 编号升序
    records.sort(key=lambda r: (-r.points, -r.buchholz, r.number))
    return records


def half_to_float(v: int) -> float:
    return v / 2.0
