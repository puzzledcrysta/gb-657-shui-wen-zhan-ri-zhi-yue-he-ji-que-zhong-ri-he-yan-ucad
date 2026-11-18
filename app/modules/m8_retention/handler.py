"""M8 入口：留档、校验、清理原始日值，以及清理后的凭证回取。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from ...web import Ctx, get_ctx
from . import service

router = APIRouter(tags=["M8 留档清理"])


def _json_response(payload: dict) -> dict:
    return payload


@router.post("/api/batches/{batch_id}/archive")
def api_archive(batch_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.archive(ctx.settings, ctx.store, batch_id)


@router.get("/api/batches/{batch_id}/archive/verify")
def api_verify(batch_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.verify(ctx.settings, ctx.store, batch_id)


@router.post("/api/batches/{batch_id}/cleanup")
def api_cleanup(batch_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.cleanup(ctx.settings, ctx.store, batch_id)


@router.get("/api/batches/{batch_id}/archive/files/{name}")
def api_archived_file(batch_id: str, name: str,
                      ctx: Ctx = Depends(get_ctx)) -> Response:
    data, filename = service.read_archived_file(
        ctx.settings, ctx.store, batch_id, name)
    media_type = ("application/json; charset=utf-8"
                  if name.endswith(".json") else "text/csv; charset=utf-8")
    return Response(content=data, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{batch_id}-{filename}"'})
