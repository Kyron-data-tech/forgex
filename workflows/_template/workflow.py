from pathlib import Path
from typing import Any
from workflows.base import Workflow

class TemplateWorkflow(Workflow):
    name: str = "template_workflow"
    title: str = "Template Workflow"
    description: str = "Starter template for building a new RPA publisher"
    content_types: list[str] = ["Article", "Post"]
    selectors_file: Path = Path(__file__).parent / "selectors.yaml"

    def get_input_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["title", "topic"],
            "properties": {
                "title": {"type": "string", "title": "Article Title", "description": "Working title"},
                "topic": {"type": "string", "title": "Topic / Subject", "description": "Subject query for research"}
            }
        }

    def get_review_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["title", "content"],
            "properties": {
                "title": {"type": "string", "title": "Final Title"},
                "content": {"type": "string", "title": "Content Body", "format": "textarea"}
            }
        }

    async def prepare(self, inputs: dict) -> dict:
        # Research, API querying, candidate data construction
        return {
            "review_data": {
                "title": inputs.get("title", ""),
                "content": f"Draft content for {inputs.get('topic', '')}."
            },
            "candidate_images": [],
            "research_raw": {}
        }

    async def dry_run(self, data: dict, page: Any) -> dict:
        # Simulate filling form in browser and capture screenshot
        return {
            "success": True,
            "screenshot_path": "",
            "filled_fields": data,
            "logs": ["Dry run executed successfully for template"]
        }

    async def publish(self, data: dict, page: Any) -> dict:
        # Live submission execution
        return {
            "success": True,
            "published_url": "https://example.com/posts/1",
            "timestamp": "2026-10-12T00:00:00Z",
            "details": {}
        }
