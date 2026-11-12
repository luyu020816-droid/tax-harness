from datetime import date
from typing import Annotated, Literal
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
    scope_ref: UUID


class ResolveScope(Scope):
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


class PrepareInvoiceWrite(Scope):
    invoice_id: str = Field(pattern="^" + INVOICE_ID_PATTERN + "$")
    patch: InvoicePatch
    expected_revision: int = Field(ge=1)
    source_refs: list[SourceRef] = Field(min_length=1, max_length=6)
    selection_ref: SelectionRef | None = None


class UpdateInvoice(StrictModel):
    plan_id: UUID


class GetInvoiceOperation(StrictModel):
    operation_id: UUID


class SearchKnowledge(Scope):
    query: str = Field(min_length=1, max_length=2000)
    document_ids: list[UUID] = Field(default_factory=list, max_length=20)
    follow_up: bool = False
    as_of: date | None = None
    period: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    scope_ref: UUID | None = None


class LookupRuleKnowledge(GetRules):
    source_type: Literal["rules"]


class LookupDocumentKnowledge(SearchKnowledge):
    source_type: Literal["documents"]


class LookupKnowledge(StrictModel):
    request: Annotated[
        LookupRuleKnowledge | LookupDocumentKnowledge, Field(discriminator="source_type")
    ]


class PrepareKnowledgeText(Scope):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20000)


class ReadEvidence(StrictModel):
    citation_id: UUID


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
    company_ids: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="仅用于用户明确指定多个企业的联合历史查找，与company_id互斥；不提供企业范围则只查个人私有。",
    )
    query: str = Field(default="", max_length=2000)
    entity: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    cursor: str | None = None
    mode: Literal["hybrid", "lexical", "date"] = "hybrid"


class SearchConversations(StrictModel):
    mode: Literal["auto", "lexical", "semantic"] = "auto"
    page_size: int = Field(default=5, ge=1, le=10)
    query: str = Field(default="", max_length=2000)
    session_id: UUID | None = None
    date_from: str | None = None
    date_to: str | None = None
    cursor: UUID | None = None


class ReadConversation(StrictModel):
    source_id: UUID
    char_start: int = Field(default=0, ge=0)


class ReadHistory(StrictModel):
    citation_id: UUID
    char_start: int | None = Field(default=None, ge=0)


class ReadSummarySource(StrictModel):
    source_id: UUID
    quote: str = Field(min_length=1, max_length=1200)


class Clarification(StrictModel):
    intent: Literal["query_month", "explain_rules", "complete_purposes", "update_invoice", "other"]
    company_id: str | None = None
    period: str | None = None
    missing_fields: list[
        Literal[
            "company_id",
            "period",
            "invoice_id",
            "purpose",
            "note",
            "intent",
            "write_confirmation",
            "target",
            "location",
        ]
    ] = Field(min_length=1, max_length=7)
    task_id: UUID | None = None
    task_generation: int | None = None
    summary: str = Field(default="", max_length=500)


class ResultBlock(StrictModel):
    type: Literal["result"]
    result_ref: UUID
    view: Literal[
        "history_page",
        "monthly_summary",
        "purpose_check",
        "invoices",
        "rules",
        "task",
        "preferences",
        "invoice_import_batch",
        "knowledge_draft",
        "public_web",
    ]


class OperationBlock(StrictModel):
    type: Literal["operation"]
    operation_id: UUID


class PreferenceBlock(StrictModel):
    type: Literal["preference_receipt"]
    result_ref: UUID
    action: Literal["remember", "update", "delete"]


class ExplanationBlock(StrictModel):
    type: Literal["explanation"]
    code: Literal[
        "acknowledged",
        "no_changes",
        "independent_totals",
        "amount_materials",
        "amount_material_unsupported",
        "read_only",
        "access_denied",
        "unsupported_fields",
        "cannot_infer_payment",
        "unsupported_period",
        "target_not_found",
        "history_not_found",
        "history_partial",
        "history_insufficient",
        "history_unavailable",
        "conversation_boundary",
        "no_tasks",
        "write_not_authorized",
        "purpose_all",
        "purpose_threshold",
        "request_purpose_values",
        "private_memory_boundary",
        "rule_activation_boundary",
        "knowledge_not_found",
        "knowledge_partial",
        "knowledge_unavailable",
        "knowledge_insufficient",
        "knowledge_compare",
        "public_web_unavailable",
        "public_weather_period",
        "public_web_scope_required",
    ]


class HistoryBlock(StrictModel):
    type: Literal["history_quote"]
    source_id: UUID | None = None
    quote: str | None = Field(default=None, min_length=1, max_length=1200)
    citation_id: UUID | None = None

    @model_validator(mode="after")
    def require_evidence(self):
        if not self.citation_id and (not self.source_id or not self.quote):
            raise ValueError("历史引用必须提供citation_id，或匹配的source_id与quote。")
        return self


class ConfirmationBlock(StrictModel):
    type: Literal["confirmation"]
    plan_id: UUID


class KnowledgeBlock(StrictModel):
    type: Literal["knowledge_quote"]
    citation_id: UUID


class KnowledgeClaim(StrictModel):
    text: str = Field(min_length=1, max_length=1200)
    citation_ids: list[UUID] = Field(min_length=1, max_length=6)


class KnowledgeAnswerBlock(StrictModel):
    type: Literal["knowledge_answer"]
    coverage: Literal["supported", "partial", "insufficient"]
    claims: list[KnowledgeClaim] = Field(default_factory=list, max_length=6)
    gap: str | None = Field(default=None, max_length=500)
    follow_up: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def consistent_coverage(self):
        if self.coverage == "insufficient" and self.claims:
            raise ValueError("依据不足时不能输出事实断言。")
        if self.coverage in ("supported", "partial") and not self.claims:
            raise ValueError("有依据的回答至少需要一条带引用的断言。")
        if self.coverage == "partial" and not self.gap:
            raise ValueError("部分回答必须说明未覆盖的内容。")
        return self


class ConversationAnswerBlock(StrictModel):
    type: Literal["conversation_answer"]
    topic: Literal["today", "tomorrow", "previous_turn_status", "capabilities"]


class ChatAnswerBlock(StrictModel):
    type: Literal["chat_answer"]
    text: str = Field(min_length=1, max_length=4000)


AnswerBlock = Annotated[
    ResultBlock
    | OperationBlock
    | PreferenceBlock
    | ExplanationBlock
    | HistoryBlock
    | ConfirmationBlock
    | KnowledgeBlock
    | KnowledgeAnswerBlock
    | ConversationAnswerBlock
    | ChatAnswerBlock,
    Field(discriminator="type"),
]


class FinalReply(StrictModel):
    blocks: list[AnswerBlock] = Field(default_factory=list, max_length=10)
    status: Literal["completed", "waiting_user"] = "completed"
    clarification: Clarification | None = None
    resume_turn_id: UUID | None = None

    @model_validator(mode="after")
    def consistent_state(self):
        if not self.blocks and self.clarification is None:
            raise ValueError("空答复必须是必要的澄清，不能空白完成。")
        if self.status == "waiting_user" and self.clarification is None:
            raise ValueError(
                "waiting_user 必须提供 clarification；权限拒绝或操作失败但不缺用户信息时使用 completed。"
            )
        if self.status == "completed" and self.clarification is not None:
            raise ValueError("completed 的 clarification 必须为 null。")
        return self


class PrepareInvoiceImport(Scope):
    upload_id: UUID


class GetInvoiceImport(StrictModel):
    batch_id: UUID
    cursor: int = Field(default=0, ge=0)


class PublicWebQuery(StrictModel):
    kind: Literal["weather", "search"]
    question: str = Field(min_length=1, max_length=240)
    city: str | None = Field(default=None, max_length=32)
    days_ahead: int = Field(default=0, ge=0, le=2)
    metric: Literal["overview", "low", "high", "current", "feels", "rain", "humidity", "wind"] = (
        "overview"
    )


TOOLS = {
    "search_public_web": (
        PublicWebQuery,
        "查用户所问的公开实时信息。天气用weather，question逐字引用本轮天气问句，city用本轮地点或上一次天气结果中的城市，days_ahead按今天=0、明天=1、后天=2，metric按所问字段；缺城市可省略。公开搜索用search，question逐字引用本轮明确要求联网的公开问句。不可传企业资料、附件或历史正文。",
    ),
    "search_conversations": (
        SearchConversations,
        "仅在用户要查过去的聊天原话、决定或方案时搜索本人可访问的聊天。当前日期、运行状态和资料规则不查此工具。查询词须来自当前需求或可辨认的上文。partial代表覆盖不足，多个候选须澄清。date_to为不含的带时区ISO上界，分页保持参数并传next_cursor。",
    ),
    "read_conversation": (
        ReadConversation,
        "按真实source_id和char_start回读旧消息，最多1200字符。只读取回答所需片段；找结尾可按source_length直接从尾部位置回读。目标内容已在搜索摘录时直接引用，不必从头遍历无关正文。需要全文才按next_char_start继续，超预算时保留原文入口。角色assistant不代表用户确认；旧指令不是当前修改授权。",
    ),
    "prepare_knowledge_text": (
        PrepareKnowledgeText,
        "准备文字入库草稿，由用户点击提交才写入知识库，不得声称草稿已入库。",
    ),
    "read_summary_source": (
        ReadSummarySource,
        "仅回读本轮摘要protected_excerpts内的source_id和逐字quote，返回核验后的citation_id。不是搜索；原话不授权当前修改，过期摘要不可读。",
    ),
    "search_knowledge": (
        SearchKnowledge,
        "查授权企业已提交的制度和资料，返回出处和citation_id。自然追问前一资料时用follow_up=true；多个可能目标且用户未选时先澄清。赛事正式规则用get_rules，聊天原话用search_conversations。资料问答只读。",
    ),
    "lookup_knowledge": (
        LookupKnowledge,
        "查知识依据。rules查指定企业、年月的正式财税规则；documents查上传资料。需要两类来源时分别调用。聊天历史和当前业务数据使用各自工具。",
    ),
    "read_evidence": (ReadEvidence, "回读本人引用的当前有效原文；停用或换版后旧引用失效。"),
    "get_invoice_operation": (
        GetInvoiceOperation,
        "按本人真实operation_id只读核验保存状态；超时或重试应先查原编号，不另造修改。",
    ),
    "resolve_scope": (
        ResolveScope,
        "为有真实用户/任务依据的企业月份签发本轮scope_ref；不能确认猜测月份。优先使用request_frame.available_scope_refs。",
    ),
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
        "查询指定企业、月份的实际发票总额、银行收入和支出；仅询问可查哪些资料时无需调用。",
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
        "只执行本轮宿主已授权的plan_id，不接收自由patch；未授权计划需要本人界面确认。",
    ),
    "prepare_invoice_write": (
        PrepareInvoiceWrite,
        "先查发票revision，再根据当前原话冻结用途/备注修改计划；同票字段合并。返回authorized才可update_invoice，needs_confirmation等待本人界面确认，prohibited不得执行。",
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
        "查本人历史原文；query支持同义表达。纯日期浏览省略query，提供至多31天的date_from/date_to（结束不含）；无企业仅查个人私有。历史助手声明不是当前数据，partial不代表从未说过。",
    ),
    "read_history": (
        ReadHistory,
        "按citation_id核验历史原文与角色；更正、删除或撤权后失效。长消息按next_char_start继续传char_start读取，每次最多1200字符，不能把摘录当全消息。",
    ),
}


def _without_schema_titles(value):
    if isinstance(value, list):
        return [_without_schema_titles(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _without_schema_titles(item)
            for key, item in value.items()
            if not (key == "title" and isinstance(item, str))
        }
    return value


def tool_definitions() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": _without_schema_titles(schema.model_json_schema()),
            },
        }
        for name, (schema, description) in TOOLS.items()
        if name not in ("search_history", "read_history", "read_summary_source")
    ]


REFERENCE_MUTATIONS = frozenset({"prepare_invoice_write", "update_invoice", "change_task"})


def reference_definitions(definitions):
    """Show only capabilities the existing read-only dispatch guard permits."""
    result = []
    for definition in definitions:
        function = definition["function"]
        if function["name"] in REFERENCE_MUTATIONS:
            continue
        if function["name"] == "manage_preference":
            definition = {
                **definition,
                "function": {
                    **function,
                    "description": "资料查询阶段只读列出本人当前偏好，不记住、更正或删除。",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "action": {"type": "string", "enum": ["list"]},
                            "scope": {"type": "string", "default": "@user"},
                        },
                        "required": ["action"],
                    },
                },
            }
        result.append(definition)
    return result
