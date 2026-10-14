import pytest
from pathlib import Path
from workflows.base import Workflow, WorkflowRegistry
from workflows.worldheroes.workflow import WorldHeroesWorkflow

def test_workflow_registry_and_discovery():
    reg = WorkflowRegistry()
    wf = WorldHeroesWorkflow()
    reg.register(wf)

    assert reg.get("worldheroes") == wf
    assert len(reg.list_all()) == 1

def test_worldheroes_schemas():
    wf = WorldHeroesWorkflow()
    in_schema = wf.get_input_schema()
    rev_schema = wf.get_review_schema()

    assert "subject_name" in in_schema["properties"]
    assert "work_date" in in_schema["properties"]
    assert "name" in rev_schema["properties"]
    assert "category" in rev_schema["properties"]
    assert "story" in rev_schema["properties"]

def test_category_inference():
    wf = WorldHeroesWorkflow()
    assert wf._infer_category("Nobel Prize winning physicist and chemist") == "scientists"
    assert wf._infer_category("Dedicated humanitarian and civil rights activist") == "humanitarians"
    assert wf._infer_category("Famous renaissance painter and sculptor") == "artists"
    assert wf._infer_category("Prime minister who led the coalition") == "leaders"
    assert wf._infer_category("Olympic sprinter and world champion") == "athletes"
