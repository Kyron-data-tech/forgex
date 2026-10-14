import json
from datetime import datetime
from typing import Optional, Any
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    workflow_name = Column(String(64), nullable=False, default="worldheroes")
    content_type = Column(String(32), nullable=False, default="Hero")
    subject_name = Column(String(255), nullable=False)
    person_slug = Column(String(255), nullable=False)
    work_date = Column(String(32), nullable=False)
    notes = Column(Text, nullable=True)
    
    # Lifecycle Status:
    # DRAFT -> PREPARING -> READY_FOR_REVIEW -> DRY_RUNNING -> DRY_RUN_PASSED -> PUBLISHING -> PUBLISHED / FAILED / ARCHIVED
    status = Column(String(32), nullable=False, default="DRAFT")
    
    # Research and candidate data (JSON encoded)
    research_json = Column(Text, nullable=True)
    candidate_images_json = Column(Text, nullable=True)
    
    # Human Review Form (JSON encoded edited fields)
    review_json = Column(Text, nullable=True)
    
    # Image Selection
    selected_image_index = Column(Integer, nullable=True)
    selected_image_path = Column(String(512), nullable=True)
    selected_image_sidecar = Column(String(512), nullable=True)
    custom_image_path = Column(String(512), nullable=True)
    
    # Dry Run & Publishing Artifacts
    dry_run_screenshot = Column(String(512), nullable=True)
    published_url = Column(String(512), nullable=True)
    publish_result_json = Column(Text, nullable=True)
    
    # Diagnostics
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    logs = relationship("JobLog", back_populates="job", cascade="all, delete-orphan", order_by="JobLog.id")
    run = relationship("Run", back_populates="job", foreign_keys=[run_id])

    def get_research(self) -> dict:
        return json.loads(self.research_json) if self.research_json else {}

    def set_research(self, data: dict):
        self.research_json = json.dumps(data, ensure_ascii=False)

    def get_candidate_images(self) -> list:
        return json.loads(self.candidate_images_json) if self.candidate_images_json else []

    def set_candidate_images(self, data: list):
        self.candidate_images_json = json.dumps(data, ensure_ascii=False)

    def get_review(self) -> dict:
        return json.loads(self.review_json) if self.review_json else {}

    def set_review(self, data: dict):
        self.review_json = json.dumps(data, ensure_ascii=False)


class JobLog(Base):
    __tablename__ = "job_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    step_name = Column(String(64), nullable=False)
    level = Column(String(16), nullable=False, default="INFO")
    message = Column(Text, nullable=False)
    screenshot_path = Column(String(512), nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

    job = relationship("Job", back_populates="logs")


class Run(Base):
    __tablename__ = "runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task = Column(Text, nullable=False)
    subject_name = Column(String(255), nullable=True)
    work_date = Column(String(64), nullable=True)
    content_type = Column(String(32), nullable=False, default="Hero")
    workflow_name = Column(String(64), nullable=False, default="worldheroes")
    status = Column(String(32), nullable=False, default="QUEUED")
    command = Column(String(32), nullable=True)  # PAUSE, RESUME, STOP, APPROVE, REJECT
    research_bundle = Column(Text, nullable=True)
    result_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    steps = relationship("RunStep", back_populates="run", cascade="all, delete-orphan", order_by="RunStep.step_no")
    job = relationship("Job", back_populates="run", uselist=False)

    def get_result(self) -> dict:
        return json.loads(self.result_json) if self.result_json else {}

    def set_result(self, data: dict):
        self.result_json = json.dumps(data, ensure_ascii=False)


class RunStep(Base):
    __tablename__ = "run_steps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    step_no = Column(Integer, nullable=False)
    tab_id = Column(String(32), nullable=True)
    thought = Column(Text, nullable=True)
    action = Column(String(64), nullable=False)
    args_json = Column(Text, nullable=True)
    url = Column(String(512), nullable=True)
    screenshot_path = Column(String(512), nullable=True)
    status = Column(String(32), nullable=False, default="SUCCESS")
    error = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

    run = relationship("Run", back_populates="steps")

    def get_args(self) -> dict:
        return json.loads(self.args_json) if self.args_json else {}

    def set_args(self, data: dict):
        self.args_json = json.dumps(data, ensure_ascii=False)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
