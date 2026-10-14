from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional
import importlib
import inspect
import yaml

class Workflow(ABC):
    """
    Abstract Base Class for ForgeX Workflow Plug-ins.
    Each plug-in defines its inputs, review schema, research preparation,
    Playwright dry run, and live publish automation.
    """
    name: str = "base_workflow"
    title: str = "Base Workflow"
    description: str = "Base RPA workflow"
    content_types: list[str] = ["Default"]
    selectors_file: Optional[Path] = None

    def __init__(self):
        self._selectors: dict = {}
        if self.selectors_file and self.selectors_file.exists():
            self._load_selectors()

    def _load_selectors(self):
        try:
            with open(self.selectors_file, "r", encoding="utf-8") as f:
                self._selectors = yaml.safe_load(f) or {}
        except Exception as e:
            self._selectors = {}

    def get_selector(self, key: str, default: str = "") -> str:
        return self._selectors.get(key, default)

    @abstractmethod
    def get_input_schema(self) -> dict:
        """
        Returns JSON schema defining fields needed for the 'New Job' form.
        Used to dynamically render inputs in the UI.
        """
        pass

    @abstractmethod
    def get_review_schema(self) -> dict:
        """
        Returns JSON schema defining editable fields on the 'Review' page.
        """
        pass

    @abstractmethod
    async def prepare(self, inputs: dict) -> dict:
        """
        Gathers background intelligence (e.g. Wikipedia facts, Wikimedia images)
        and populates the candidate review data.
        Returns: {
            "review_data": dict,
            "candidate_images": list[dict],
            "research_raw": dict,
            "disambiguation_options": list[str] (optional)
        }
        """
        pass

    @abstractmethod
    async def dry_run(self, data: dict, page: Any) -> dict:
        """
        Executes safe Playwright automation simulating the workflow up to
        (but never clicking) the final submit button.
        Takes a full-page verification screenshot.
        Returns: {
            "success": bool,
            "screenshot_path": str,
            "filled_fields": dict,
            "logs": list[str]
        }
        """
        pass

    @abstractmethod
    async def publish(self, data: dict, page: Any) -> dict:
        """
        Executes authorized submission using user's active session.
        Verifies resulting URL and logs audit metadata.
        Returns: {
            "success": bool,
            "published_url": str,
            "timestamp": str,
            "details": dict
        }
        """
        pass

    async def run_playbook(self, inputs: dict, executor: Any) -> dict:
        """
        Optional Operator playbook execution hook:
        Performs visible multi-tab research and ChatGPT extraction via Pointer,
        falling back to prepare() when visible mode is disabled.
        """
        return await self.prepare(inputs)




class WorkflowRegistry:
    """Auto-discovery and registration for ForgeX workflow plugins."""
    def __init__(self):
        self._workflows: dict[str, Workflow] = {}

    def register(self, workflow_instance: Workflow):
        self._workflows[workflow_instance.name] = workflow_instance

    def get(self, name: str) -> Optional[Workflow]:
        return self._workflows.get(name)

    def list_all(self) -> list[Workflow]:
        return list(self._workflows.values())

    def discover(self, workflows_dir: Path):
        """Discovers workflows from subfolders in workflows/ (skipping _* directories)."""
        if not workflows_dir.exists():
            return

        for item in workflows_dir.iterdir():
            if item.is_dir() and not item.name.startswith("_") and not item.name.startswith("."):
                workflow_file = item / "workflow.py"
                if workflow_file.exists():
                    module_name = f"workflows.{item.name}.workflow"
                    try:
                        module = importlib.import_module(module_name)
                        for attr_name in dir(module):
                            attr = getattr(module, attr_name)
                            if (
                                inspect.isclass(attr)
                                and issubclass(attr, Workflow)
                                and attr is not Workflow
                            ):
                                instance = attr()
                                self.register(instance)
                    except Exception as e:
                        print(f"Warning: Failed to discover workflow in {item.name}: {e}")

registry = WorkflowRegistry()
