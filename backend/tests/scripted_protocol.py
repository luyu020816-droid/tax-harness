"""Migrate program-test scripts to current wire DTOs through real tool outputs.

This adapter never grants authorization or confirms a plan. Production does not
import it; old narrative fixtures supply test intent, not user-visible facts.
"""

import json
from uuid import UUID

MISSING_REF = str(UUID(int=1))


def actual_outputs(payload):
    return [json.loads(m["content"]) for m in payload["messages"] if m.get("role") == "tool"]


def migrate_answer(legacy, payload):
    outputs = actual_outputs(payload)
    blocks = []
    for out in outputs:
        if not out.get("ok"):
            continue
        data = out.get("data") or {}
        kind = data.get("kind")
        if kind == "operation":
            blocks.append({"type": "operation", "operation_id": data["operation_id"]})
        elif kind in (
            "monthly_summary",
            "purpose_check",
            "invoices",
            "rules",
            "import_preview",
            "import_receipt",
        ):
            blocks.append(
                {
                    "type": "result",
                    "result_ref": out["result_ref"],
                    "view": "invoice_import_batch" if kind.startswith("import_") else kind,
                }
            )
        elif "preferences" in data:
            blocks.append(
                {"type": "result", "result_ref": out["result_ref"], "view": "preferences"}
            )
    action = legacy.get("preference_action")
    if action and legacy.get("preference_status") == "committed":
        ref = next(
            (
                o["result_ref"]
                for o in reversed(outputs)
                if o.get("ok") and (o.get("data") or {}).get("preference_id")
            ),
            MISSING_REF,
        )
        blocks.append({"type": "preference_receipt", "result_ref": ref, "action": action})
    if not blocks:
        blocks = [{"type": "explanation", "code": "acknowledged"}]
    return {
        "role": "assistant",
        "content": json.dumps(
            {
                "blocks": blocks,
                "status": legacy.get("status", "completed"),
                "clarification": legacy.get("clarification"),
                "resume_turn_id": legacy.get("resume_turn_id"),
            },
            ensure_ascii=False,
        ),
    }
