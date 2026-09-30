"""赛务争议扣分账本：登记扣分、撤销仍生效的扣分与账本查询。

账本 append-only：扣分登记为一行 effective 的 deduction；撤销不
修改金额，而是追加一行 effective 的 revocation（复制原扣分值、
指向原扣分行）并把原行置为 revoked。每位参赛者的有效扣分 = 扣分
金额之和 − 撤销金额之和；排名积分 = 对局积分 − 有效扣分。

操作窗口：仅当赛事所有已生成轮次均已结束（没有 open 轮）且
不存在待处理赛果复核时允许扣分/撤销，否则 409 不落库。

乐观快照：写事务内确认赛事当前最大事件序号等于
expected_event_seq，否则 409 不落库、不占事件序号。
request_id 提供赛事内幂等重放，同键不同参数返回 409。重复撤销
（原扣分已被撤销）返回 409 且不落库。

注意：本模块的写函数只在调用方事务内写入并 flush，不提交；
账本行、幂等记录与 point_deduction.* 事件由路由层在同一事务中
统一提交。所有调用均处于进程级 writer_lock 内。
"""
from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..events.service import (
    POINT_DEDUCTION_REGISTERED,
    POINT_DEDUCTION_REVOKED,
    append_event,
)
from ..models import (
    Event,
    Participant,
    PointDeductionAction,
    PointDeductionEntry,
    Round,
)
from ..schemas import (
    PointDeductionCreate,
    PointDeductionEntryResponse,
    PointDeductionLedgerResponse,
    PointDeductionRevokeRequest,
)
from .announcements import current_event_seq
from .result_reviews import has_pending_review_for_tournament
from .scoring import (
    DEDUCTION,
    EFFECTIVE,
    REVOCATION,
    REVOKED,
    half_to_float,
)


def ensure_deductions_open(db: Session, tournament_id: int) -> None:
    """扣分/撤销窗口：所有已生成轮次已结束，且无待处理赛果复核。"""
    open_round = (
        db.query(Round.id)
        .filter(Round.tournament_id == tournament_id, Round.status == "open")
        .first()
    )
    if open_round is not None:
        raise HTTPException(
            status_code=409,
            detail="赛事存在尚未结束的轮次，所有轮次结束后才能登记扣分",
        )
    if has_pending_review_for_tournament(db, tournament_id):
        raise HTTPException(
            status_code=409,
            detail="赛事存在待处理的赛果复核，需待复核关闭后才能登记扣分",
        )


def _assert_snapshot(db: Session, tournament_id: int, expected_seq: int) -> None:
    actual = current_event_seq(db, tournament_id)
    if expected_seq != actual:
        raise HTTPException(
            status_code=409,
            detail=(
                f"赛事快照已变化：当前最大事件序号为 {actual}，"
                f"请求期望 {expected_seq}"
            ),
        )


def _find_action(
    db: Session,
    tournament_id: int,
    request_id: str,
    fingerprint: dict,
) -> PointDeductionEntryResponse | None:
    """同 request_id 的已成功请求：参数一致返回首次响应，否则 409。"""
    record = (
        db.query(PointDeductionAction)
        .filter(
            PointDeductionAction.tournament_id == tournament_id,
            PointDeductionAction.request_id == request_id,
        )
        .first()
    )
    if record is None:
        return None
    if record.params == fingerprint:
        return PointDeductionEntryResponse(**record.response)
    raise HTTPException(
        status_code=409,
        detail="request_id 已被用于该赛事的其他扣分请求，请更换 request_id",
    )


def serialize_entry(entry: PointDeductionEntry) -> PointDeductionEntryResponse:
    return PointDeductionEntryResponse(
        id=entry.id,
        tournament_id=entry.tournament_id,
        participant_id=entry.participant_id,
        entry_type=entry.entry_type,
        amount=half_to_float(entry.amount_halves),
        reason=entry.reason,
        status=entry.status,
        reversed_entry_id=entry.reversed_entry_id,
        expected_event_seq=entry.expected_event_seq,
        created_at=entry.created_at,
    )


def _get_participant(
    db: Session, tournament_id: int, participant_id: int
) -> Participant:
    participant = (
        db.query(Participant)
        .filter(
            Participant.id == participant_id,
            Participant.tournament_id == tournament_id,
        )
        .first()
    )
    if participant is None:
        raise HTTPException(status_code=404, detail="参赛者不存在或不属于本赛事")
    return participant


def register_deduction(
    db: Session,
    tournament_id: int,
    participant_id: int,
    req: PointDeductionCreate,
) -> tuple[PointDeductionEntryResponse, Event | None]:
    """登记一笔扣分（flush，不提交），返回（响应, 事件）。

    幂等重放时返回（首次响应, None），不产生任何写入；操作窗口
    未开放或事件序号快照冲突抛 409，且在任何写入之前抛出。
    """
    amount_halves = round(req.amount * 2)
    fingerprint = {
        "action": "deduct",
        "participant_id": participant_id,
        "amount_halves": amount_halves,
        "reason": req.reason,
        "expected_event_seq": req.expected_event_seq,
    }
    replay = _find_action(db, tournament_id, req.request_id, fingerprint)
    if replay is not None:
        return replay, None

    _get_participant(db, tournament_id, participant_id)
    ensure_deductions_open(db, tournament_id)
    _assert_snapshot(db, tournament_id, req.expected_event_seq)

    entry = PointDeductionEntry(
        tournament_id=tournament_id,
        participant_id=participant_id,
        entry_type=DEDUCTION,
        amount_halves=amount_halves,
        reason=req.reason,
        status=EFFECTIVE,
        reversed_entry_id=None,
        expected_event_seq=req.expected_event_seq,
    )
    db.add(entry)
    db.flush()  # 取得 entry.id，供幂等记录与事件使用

    event = append_event(
        db,
        tournament_id,
        POINT_DEDUCTION_REGISTERED,
        {
            "entry_id": entry.id,
            "participant_id": participant_id,
            "amount": half_to_float(amount_halves),
            "reason": req.reason,
            "expected_event_seq": req.expected_event_seq,
        },
    )

    response = serialize_entry(entry)
    db.add(
        PointDeductionAction(
            tournament_id=tournament_id,
            entry_id=entry.id,
            request_id=req.request_id,
            action="deduct",
            params=fingerprint,
            response=response.model_dump(mode="json"),
        )
    )
    db.flush()
    return response, event


def revoke_deduction(
    db: Session,
    tournament_id: int,
    deduction_id: int,
    req: PointDeductionRevokeRequest,
) -> tuple[PointDeductionEntryResponse, Event | None]:
    """用新记录撤销一笔仍生效的扣分（flush，不提交）。

    幂等重放返回（首次响应, None）；目标不存在、不是扣分、已被
    撤销、属于其他赛事/参赛者，或操作窗口未开放、快照冲突时
    抛 404/409，且在任何写入之前抛出（不落库、不占号）。
    """
    fingerprint = {
        "action": "revoke",
        "reversed_entry_id": deduction_id,
        "reason": req.reason,
        "expected_event_seq": req.expected_event_seq,
    }
    replay = _find_action(db, tournament_id, req.request_id, fingerprint)
    if replay is not None:
        return replay, None

    target = db.get(PointDeductionEntry, deduction_id)
    if target is None or target.tournament_id != tournament_id:
        raise HTTPException(status_code=404, detail="扣分记录不存在")
    if target.entry_type != DEDUCTION:
        raise HTTPException(status_code=409, detail="只能撤销扣分记录")
    if target.status != EFFECTIVE:
        raise HTTPException(
            status_code=409,
            detail="该扣分已被撤销，不能重复撤销",
        )

    ensure_deductions_open(db, tournament_id)
    _assert_snapshot(db, tournament_id, req.expected_event_seq)

    # 追加撤销行（分值与原扣分相同），并把原行置为 revoked
    revocation = PointDeductionEntry(
        tournament_id=tournament_id,
        participant_id=target.participant_id,
        entry_type=REVOCATION,
        amount_halves=target.amount_halves,
        reason=req.reason,
        status=EFFECTIVE,
        reversed_entry_id=target.id,
        expected_event_seq=req.expected_event_seq,
    )
    db.add(revocation)
    target.status = REVOKED
    db.flush()  # 取得 revocation.id

    event = append_event(
        db,
        tournament_id,
        POINT_DEDUCTION_REVOKED,
        {
            "entry_id": revocation.id,
            "reversed_entry_id": target.id,
            "participant_id": target.participant_id,
            "amount": half_to_float(target.amount_halves),
            "reason": req.reason,
            "expected_event_seq": req.expected_event_seq,
        },
    )

    response = serialize_entry(revocation)
    db.add(
        PointDeductionAction(
            tournament_id=tournament_id,
            entry_id=revocation.id,
            request_id=req.request_id,
            action="revoke",
            params=fingerprint,
            response=response.model_dump(mode="json"),
        )
    )
    db.flush()
    return response, event


def list_entries_for_tournament(
    db: Session, tournament_id: int
) -> list[PointDeductionEntry]:
    """赛事完整账本（扣分与撤销全部记录，按登记顺序）。"""
    return list(
        db.query(PointDeductionEntry)
        .filter(PointDeductionEntry.tournament_id == tournament_id)
        .order_by(PointDeductionEntry.id)
        .all()
    )


def get_participant_ledger(
    db: Session,
    tournament_id: int,
    participant: Participant,
) -> PointDeductionLedgerResponse:
    """按参赛者查询完整账本：全部记录与当前有效扣分净额。"""
    entries = (
        db.query(PointDeductionEntry)
        .filter(
            PointDeductionEntry.tournament_id == tournament_id,
            PointDeductionEntry.participant_id == participant.id,
        )
        .order_by(PointDeductionEntry.id)
        .all()
    )
    net = 0
    for e in entries:
        if e.entry_type == DEDUCTION:
            net += e.amount_halves
        elif e.entry_type == REVOCATION:
            net -= e.amount_halves
    return PointDeductionLedgerResponse(
        tournament_id=tournament_id,
        participant_id=participant.id,
        number=participant.number,
        name=participant.name,
        effective_deduction=half_to_float(net),
        entries=[serialize_entry(e) for e in entries],
    )
