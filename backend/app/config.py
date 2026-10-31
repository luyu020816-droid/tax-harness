import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("TAX_HARNESS_ENV_FILE", ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(default="", repr=False)
    admin_database_url: str = Field(default="", repr=False)
    test_database_url: str = Field(default="", repr=False)
    ingest_database_url: str = Field(default="", repr=False)
    model_base_url: str = Field(default="", repr=False)
    model_id: str = ""
    model_api_key: str = Field(default="", repr=False)
    model_context_limit: int = Field(default=0, ge=0)
    model_extra_body: dict = Field(default_factory=dict)
    token_counter: str = "conservative_utf8"
    output_reserve: int = Field(default=2048, ge=256)
    input_budget: int = Field(default=16000, ge=512)
    max_model_steps: int = Field(default=8, ge=1, le=30)
    max_tool_calls: int = Field(default=16, ge=1, le=60)
    turn_timeout: int = Field(default=180, ge=10)
    demo_mode: bool = True
    worker_count: int = Field(default=2, ge=1, le=8)
    lease_seconds: int = Field(default=90, ge=30)
    cookie_secure: bool = False
    knowledge_enabled: bool = True
    knowledge_model_directory: Path = ROOT / ".local/retrieval-models"
    knowledge_selection_policy: Path = ROOT / "evals/retrieval-v2/selection-policy.json"
    knowledge_max_file_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    knowledge_parser_timeout: int = Field(default=60, ge=1, le=120)
    knowledge_ocr_enabled: bool = True
    knowledge_ocr_runtime: Path = ROOT / ".local/ocr-runtime"
    knowledge_ocr_model_directory: Path = ROOT / ".local/ocr-models"
    knowledge_ocr_timeout: int = Field(default=600, ge=60, le=600)
    knowledge_ocr_max_pages: int = Field(default=50, ge=1, le=50)
    knowledge_ocr_max_pixels: int = Field(default=20_000_000, ge=1_000_000, le=20_000_000)
    knowledge_ocr_min_confidence: float = Field(default=0.75, ge=0.5, le=0.99)
    knowledge_lease_seconds: int = Field(default=90, ge=30)
    history_enabled: bool = True
    history_selection_policy: Path = ROOT / "evals/history-v1/selection-policy.json"

    @property
    def model_configured(self) -> bool:
        return bool(
            self.model_base_url
            and self.model_id
            and self.model_api_key
            and self.model_context_limit > self.output_reserve
        )

    @property
    def context_budget(self) -> int:
        available = int(self.model_context_limit * 0.95) - self.output_reserve
        return min(self.input_budget, max(0, available))
