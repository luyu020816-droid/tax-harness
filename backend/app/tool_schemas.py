from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.values import INVOICE_ID_PATTERN


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=16000)


class SourceRef(StrictModel):
    source_id: UUID
    quote: str = Field(min_length=1, max_length=6000)


class Scope(StrictModel):
    company_id: str = Field(pattern=r"^C\d+$")


class Monthly(Scope):
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


class QueryMonth(Monthly):
    metrics: list[Literal["invoice_total", "bank_income", "bank_expense"]] | None = None


class FindInvoices(Scope):
    period: str | None = None
    ids: list[str] | None = Field(default=None, max_length=50)
    seller: str | None = Field(default=None, max_length=100)
    item: str | None = Field(default=None, max_length=100)
    cursor: str | None = None


class GetRules(Monthly):
    topics: list[Literal["amounts", "purpose", "bank"]] = Field(min_length=1, max_length=3)


class CheckPurposes(Monthly):
    cursor: str | None = None


class InvoicePatch(StrictModel):
    purpose: str | None = Field(default=None, max_length=2000)
    note: str | None = Field(default=None, max_length=2000)


class SelectionRef(StrictModel):
    view_id: UUID
    row_index: int = Field(ge=1, le=10)


class UpdateInvoice(Scope):
    invoice_id: str = Field(pattern="^" + INVOICE_ID_PATTERN + "$")
    patch: InvoicePatch
    expected_revision: int = Field(ge=1)
    source_refs: list[SourceRef] = Field(min_length=1, max_length=6)
    selection_ref: SelectionRef | None = None


class ChangeTask(Scope):
    action: Literal["start", "update", "replace", "cancel", "delete"]
    intent: Literal["query_month", "explain_rules", "complete_purposes"] | None = None
    period: str | None = None
    target_id: UUID | None = None
    expected_revision: int | None = None
    unresolved: dict = Field(default_factory=dict)
    source_refs: list[SourceRef] = Field(min_length=1, max_length=6)


class ManagePreference(StrictModel):
    action: Literal["list", "remember", "update", "delete"]
    scope: str = "@user"
    key: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        description="稳定英文键，例如 response_style、result_order、list_format；不能用中文。",
    )
    value: str | None = Field(default=None, max_length=1000)
    target_id: UUID | None = Field(
        default=None,
        description="从当前正式偏好或 list 结果逐字复制 preference_id，不得自行生成或改写。",
    )
    expected_revision: int | None = None
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=6)


class SearchHistory(StrictModel):
    company_id: str | None = None
    query: str = Field(max_length=300)
    entity: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    cursor: str | None = None


class Clarification(StrictModel):
    intent: Literal["query_month", "explain_rules", "complete_purposes", "update_invoice", "other"]
    company_id: str | None = None
    period: str | None = None
    missing_fields: list[str] = Field(min_length=1, max_length=8)
    task_id: UUID | None = None
    task_generation: int | None = None
    summary: str = Field(max_length=500)


class FinalReply(StrictModel):
    message: str = Field(min_length=1, max_length=12000)
    status: Literal["completed", "waiting_user"] = "completed"
    clarification: Clarification | None = None
    resume_turn_id: UUID | None = None
    displayed_view_ids: list[UUID] = Field(default_factory=list, max_length=4)
    preference_action: Literal["remember", "update", "delete"] | None
    preference_status: Literal["not_requested", "committed", "not_committed", "needs_clarification"]

    @model_validator(mode="after")
    def consistent_state(self):
        if self.status == "waiting_user" and self.clarification is None:
            raise ValueError(
                "waiting_user 必须提供 clarification；权限拒绝或操作失败但不缺用户信息时使用 completed。"
            )
        if self.status == "completed" and self.clarification is not None:
            raise ValueError("completed 的 clarification 必须为 null。")
        if (self.preference_action is None) != (self.preference_status == "not_requested"):
            raise ValueError("偏好操作必须同时声明 preference_action 和 preference_status。")
        return self


class PrepareInvoiceImport(Scope):
    upload_id: UUID


class GetInvoiceImport(StrictModel):
    batch_id: UUID
    cursor: int = Field(default=0, ge=0)


TOOLS = {
    "prepare_invoice_import": (
        PrepareInvoiceImport,
        "为本轮附件生成发票追加预览，不执行入库；说明用户需要在界面确认。",
    ),
    "get_invoice_import": (
        GetInvoiceImport,
        "读取本人导入批次的真实状态、逐行原因和回执，不将预览说成已提交。",
    ),
    "query_month": (
        QueryMonth,
        "查询当前保存数据的月度发票总额、银行收入和支出；三项独立计算，并返回规则依据。",
    ),
    "find_invoices": (
        FindInvoices,
        "按企业、月份、编号、销售方或票面项目定位发票。不能据票面猜用途。返回修改所需 revision。",
    ),
    "get_rules": (GetRules, "按企业、月份和主题读取当前适用的完整规则及版本。"),
    "check_purposes": (
        CheckPurposes,
        "检查2026-09当前必填未填用途，区分两企业规则，返回有序列表与真实总数。",
    ),
    "update_invoice": (
        UpdateInvoice,
        "按本轮明确用户要求修改唯一发票用途/备注。先查询 revision，同票字段合并 patch；null表示明确清空。必须引用当前用户来源。",
    ),
    "get_task": (Scope, "精确读取当前用户在该企业的未完成任务。新会话续办仍须查询当前资料。"),
    "change_task": (
        ChangeTask,
        "创建、更正、明确替换、取消或删除任务；每用户每企业最多一项未完成，不能凭口头声明完成。",
    ),
    "manage_preference": (
        ManagePreference,
        "查看或按明确长期意图记住/更正/删除偏好。临时要求不持久化，不能据此授权业务写入。",
    ),
    "search_history": (
        SearchHistory,
        "在授权范围查找历史原文，不以历史助手的声明代替当前数据。无证据时说明未找到。",
    ),
}


def tool_definitions() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": schema.model_json_schema(),
            },
        }
        for name, (schema, description) in TOOLS.items()
    ]
