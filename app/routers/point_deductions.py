"""赛务争议扣分账本接口。

- POST /tournaments/{id}/participants/{participant_id}/deductions：
  为参赛者登记一笔半分整数倍的扣分（携带 request_id 幂等键与
  expected_event_seq 序号快照、分值与理由）。重复请求返回首次
  结果；快照冲突不落库。
- POST /tournaments/{id}/point-deductions/{deduction_id}/revoke：
  用一条新的撤销记录撤销一笔仍生效的扣分（快照冲突或重复撤销
  均 409 不落库）。
- GET /tournaments/{id}/participants/{participant_id}/deductions：
  按参赛者查询完整账本（扣分、撤销全部记录与有效扣分净额）。
- GET /tournaments/{id}/point-deductions：赛事完整账本。
- GET /point-deductions/{entry_id}：单条账本记录。

扣分/撤销仅允许在赛事所有已生成轮次结束、且不存在待处理赛果
复核时进行；账本行、幂等记录与 point_deduction.* 事件在同一事务
提交。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..events.service import commit_and_publish
from ..locking import writer_lock
from ..models import Participant, PointDeductionEntry
from ..schemas import (
    PointDeductionCreate,
    PointDeductionEntryResponse,
    PointDeductionLedgerResponse,
    PointDeductionRevokeRequest,
)
from ..services.point_deductions import (
    get_participant_ledger,
    list_entries_for_tournament,
    register_deduction,
    revoke_deduction,
    serialize_entry,
)
from .tournaments import get_tournament_or_404

router = APIRouter()


def _get_tournament_participant(
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


@router.post(
    "/tournaments/{tournament_id}/participants/{participant_id}/deductions",
    response_model=PointDeductionEntryResponse,
    status_code=201,
)
def create_deduction(
    tournament_id: int,
    participant_id: int,
    payload: PointDeductionCreate,
    db: Session = Depends(get_db),
):
    # 存在性先在锁外给出 404；锁内会以最新状态重新读取并校验
    get_tournament_or_404(db, tournament_id)
    _get_tournament_participant(db, tournament_id, participant_id)

    # 写请求进程级串行：窗口校验、快照确认、账本落库、事件序号与
    # 「提交→发布」全程持锁
    with writer_lock():
        db.expire_all()
        response, event = register_deduction(
            db, tournament_id, participant_id, payload
        )
        # 幂等重放（event 为 None）没有任何写入，直接返回首次结果
        if event is not None:
            commit_and_publish(db, [event])
    return response


@router.post(
    "/tournaments/{tournament_id}/point-deductions/{deduction_id}/revoke",
    response_model=PointDeductionEntryResponse,
    status_code=201,
)
def revoke_point_deduction(
    tournament_id: int,
    deduction_id: int,
    payload: PointDeductionRevokeRequest,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)

    with writer_lock():
        db.expire_all()
        response, event = revoke_deduction(db, tournament_id, deduction_id, payload)
        if event is not None:
            commit_and_publish(db, [event])
    return response


@router.get(
    "/tournaments/{tournament_id}/participants/{participant_id}/deductions",
    response_model=PointDeductionLedgerResponse,
)
def get_participant_deductions(
    tournament_id: int,
    participant_id: int,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    participant = _get_tournament_participant(db, tournament_id, participant_id)
    return get_participant_ledger(db, tournament_id, participant)


@router.get(
    "/tournaments/{tournament_id}/point-deductions",
    response_model=list[PointDeductionEntryResponse],
)
def list_tournament_deductions(tournament_id: int, db: Session = Depends(get_db)):
    get_tournament_or_404(db, tournament_id)
    entries = list_entries_for_tournament(db, tournament_id)
    return [serialize_entry(e) for e in entries]


@router.get(
    "/point-deductions/{entry_id}",
    response_model=PointDeductionEntryResponse,
)
def get_point_deduction_entry(entry_id: int, db: Session = Depends(get_db)):
    # 单条查询不带赛事路径：记录存在即可（记录本身含 tournament_id）
    entry = db.get(PointDeductionEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="扣分账本记录不存在")
    return serialize_entry(entry)
