from fastapi import APIRouter, HTTPException
from workflows.base import registry

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

@router.get("")
async def list_workflows():
    workflows = registry.list_all()
    return [
        {
            "name": w.name,
            "title": w.title,
            "description": w.description,
            "content_types": w.content_types
        }
        for w in workflows
    ]

@router.get("/{name}/schemas")
async def get_workflow_schemas(name: str):
    wf = registry.get(name)
    if not wf:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found.")
    return {
        "name": wf.name,
        "title": wf.title,
        "input_schema": wf.get_input_schema(),
        "review_schema": wf.get_review_schema()
    }
