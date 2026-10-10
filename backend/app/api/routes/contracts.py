"""合同接口：上传、解析、列表、详情、原文件流。"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import Settings, get_db, get_settings
from app.api.mappers import to_contract_brief, to_contract_detail
from app.models.contract import Contract
from app.models.enums import ContractKind, ContractStatus
from app.models.review import ContractReview
from app.schemas.common import PageMeta
from app.schemas.contract import (
    ContractDetail,
    ContractListResponse,
    ContractUpdateRequest,
)
from app.services.contract_service import infer_contract_kind, parse_contract

router = APIRouter(prefix="/contracts", tags=["contracts"])

#: 上传的合同属于敏感商业资料，落盘文件名与原名分离，仅按 UUID 寻址
_SAFE_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}

_DOC_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": _DOC_CONTENT_TYPE,
}


def _media_type(suffix: str) -> str:
    return _MEDIA_TYPES.get(suffix, "application/octet-stream")


@router.post("", response_model=ContractDetail, status_code=status.HTTP_201_CREATED)
async def upload_contract(
    file: UploadFile = File(description="合同文件（pdf/docx/txt/md）"),
    title: str | None = Form(default=None),
    contract_kind: ContractKind | None = Form(default=None),
    lender_name: str | None = Form(default=None),
    borrower_name: str | None = Form(default=None),
    guarantee_provider: str | None = Form(default=None),
    session: AsyncSession = Depends(get_db),
    app_settings: Settings = Depends(get_settings),
) -> ContractDetail:
    original_name = file.filename or "contract"
    suffix = Path(original_name).suffix.lower()
    if suffix not in _SAFE_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"不支持的文件类型 {suffix}，仅支持 {sorted(_SAFE_SUFFIXES)}",
        )

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件内容为空")
    if len(payload) > app_settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"文件超过 {app_settings.max_upload_mb}MB 上限",
        )

    contract_id = uuid.uuid4()
    app_settings.upload_dir.mkdir(parents=True, exist_ok=True)
    stored_path = app_settings.upload_dir / f"{contract_id}{suffix}"
    stored_path.write_bytes(payload)

    contract = Contract(
        id=contract_id,
        title=(title or Path(original_name).stem)[:255],
        contract_kind=(contract_kind or ContractKind.UNKNOWN).value,
        lender_name=lender_name,
        borrower_name=borrower_name,
        guarantee_provider=guarantee_provider,
        file_name=original_name[:255],
        file_path=str(stored_path),
        file_suffix=suffix,
        file_size_bytes=len(payload),
        status=ContractStatus.UPLOADED.value,
    )
    session.add(contract)
    await session.commit()

    await parse_contract(session, contract)

    # 未在表单指定种类时，按正文首屏特征推断，决定后续加载哪一组红线
    if contract.contract_kind == ContractKind.UNKNOWN.value:
        contract.contract_kind = infer_contract_kind(contract.content_text[:4000])
        await session.commit()

    return await load_contract_detail(contract_id, session)


@router.get("", response_model=ContractListResponse)
async def list_contracts(
    status_filter: ContractStatus | None = Query(default=None, alias="status"),
    contract_kind: ContractKind | None = None,
    keyword: str | None = Query(default=None, description="按标题/贷款人/借款人模糊搜索"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db),
) -> ContractListResponse:
    stmt = select(Contract).options(selectinload(Contract.reviews))

    if status_filter is not None:
        stmt = stmt.where(Contract.status == status_filter.value)
    if contract_kind is not None:
        stmt = stmt.where(Contract.contract_kind == contract_kind.value)
    if keyword:
        pattern = f"%{keyword}%"
        from sqlalchemy import or_

        stmt = stmt.where(
            or_(
                Contract.title.ilike(pattern),
                Contract.lender_name.ilike(pattern),
                Contract.borrower_name.ilike(pattern),
            )
        )

    total = await session.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    )
    rows = await session.execute(
        stmt.order_by(Contract.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    return ContractListResponse(
        items=[to_contract_brief(contract) for contract in rows.scalars().all()],
        meta=PageMeta(total=int(total or 0), page=page, page_size=page_size),
    )


@router.get("/{contract_id}", response_model=ContractDetail)
async def get_contract(
    contract_id: uuid.UUID, session: AsyncSession = Depends(get_db)
) -> ContractDetail:
    return await load_contract_detail(contract_id, session)


@router.get("/{contract_id}/file")
async def download_contract_file(
    contract_id: uuid.UUID, session: AsyncSession = Depends(get_db)
) -> FileResponse:
    """供前端 PDF.js 取原始 PDF 字节流。

    审查报告里的高亮坐标来自这份文件抽取出的文本，替换文件后必须重新解析。
    """
    contract = await _get_contract(session, contract_id)
    path = Path(contract.file_path)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="原文件已不存在")

    return FileResponse(
        path,
        media_type=_media_type(contract.file_suffix),
        filename=contract.file_name,
    )


@router.post("/{contract_id}/reparse", response_model=ContractDetail)
async def reparse_contract(
    contract_id: uuid.UUID, session: AsyncSession = Depends(get_db)
) -> ContractDetail:
    contract = await _get_contract(session, contract_id)
    await parse_contract(session, contract)
    if contract.status == ContractStatus.FAILED.value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=contract.parse_error or "解析失败",
        )
    return await load_contract_detail(contract_id, session)


@router.patch("/{contract_id}", response_model=ContractDetail)
async def update_contract(
    contract_id: uuid.UUID,
    payload: ContractUpdateRequest,
    session: AsyncSession = Depends(get_db),
) -> ContractDetail:
    contract = await _get_contract(session, contract_id)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(contract, field, value.value if isinstance(value, ContractKind) else value)
    await session.commit()
    return await load_contract_detail(contract_id, session)


@router.delete("/{contract_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contract(
    contract_id: uuid.UUID, session: AsyncSession = Depends(get_db)
) -> None:
    """删除合同连同其文件、结论与复核任务（数据库级级联）。"""
    contract = await _get_contract(session, contract_id)
    path = Path(contract.file_path)
    await session.delete(contract)
    await session.commit()
    path.unlink(missing_ok=True)


async def load_contract_detail(
    contract_id: uuid.UUID, session: AsyncSession
) -> ContractDetail:
    stmt = (
        select(Contract)
        .where(Contract.id == contract_id)
        .options(selectinload(Contract.reviews).selectinload(ContractReview.findings))
    )
    result = await session.execute(stmt)
    contract = result.scalars().first()
    if contract is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="合同不存在")
    return to_contract_detail(contract)


async def _get_contract(session: AsyncSession, contract_id: uuid.UUID) -> Contract:
    contract = await session.get(Contract, contract_id)
    if contract is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="合同不存在")
    return contract
