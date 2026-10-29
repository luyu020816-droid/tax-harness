import json

from pydantic import ValidationError

from app import business, invoice_write, memory, preferences, rules, tasks
from app.errors import AppError
from app.events import emit
from app.execution import Execution, guard
from app.identity import assert_access
from app.request_evidence import bind_scope, require_period, validate_scope
from app.sources import add_source, assess, read_source
from app.tool_schemas import TOOLS
from app.turns import mentioned_companies
from app.values import digest, jsonb, safe_json, uid


class ToolExecutor:
    def __init__(self, db):
        self.db = db

    def execute(self, execution: Execution, call: dict) -> tuple[dict, dict]:
        name = call.get("function", {}).get("name", "")
        raw = call.get("function", {}).get("arguments", "{}")
        call_id = call["id"]
        arguments = {}
        try:
            arguments = json.loads(raw) if isinstance(raw, str) else raw
            if name not in TOOLS:
                raise AppError("INVALID_ARGUMENT", "工具未在允许列表中。")
            parsed = TOOLS[name][0].model_validate(arguments)
            arguments = parsed.model_dump(mode="json", exclude_unset=True)
        except (ValueError, TypeError, ValidationError, AppError) as exc:
            error = (
                exc
                if isinstance(exc, AppError)
                else AppError(
                    "INVALID_ARGUMENT",
                    "工具参数不符合 schema，请修正缺失或多余字段。",
                    details={
                        "fields": [
                            {"field": ".".join(str(x) for x in e["loc"]), "message": e["msg"]}
                            for e in exc.errors(include_input=False, include_url=False)
                        ]
                    }
                    if isinstance(exc, ValidationError)
                    else {},
                )
            )
            return self._record(
                execution,
                call_id,
                name,
                arguments if isinstance(arguments, dict) else {},
                {"ok": False, "error": error.as_dict()},
            )
        with self.db.transaction() as conn:
            guard(conn, execution)
            existing = conn.execute(
                "SELECT e.*,s.content FROM tool_executions e LEFT JOIN sources s ON e.result_source_id=s.source_id WHERE run_id=%s AND tool_call_id=%s",
                (execution.run_id, call_id),
            ).fetchone()
            if existing and existing["arguments_hash"] != digest([name, arguments]):
                raise AppError("IDEMPOTENCY_CONFLICT", "同一个工具调用编号的参数不同。", 409)
            if existing and existing["state"] in ("done", "failed") and existing["content"]:
                return json.loads(existing["content"]), {
                    "source_id": str(existing["result_source_id"]),
                    "revision": 1,
                }
            conn.execute(
                "INSERT INTO tool_executions(run_id,tool_call_id,name,arguments,arguments_hash,state) VALUES(%s,%s,%s,%s,%s,'running') ON CONFLICT DO NOTHING",
                (execution.run_id, call_id, name, jsonb(arguments), digest([name, arguments])),
            )
            emit(
                conn,
                execution.turn_id,
                execution.run_id,
                "tool_started",
                {"name": name},
                tool_call_id=call_id,
            )
        task_before = None
        company = arguments.get("company_id")
        if name == "update_invoice":
            try:
                company = invoice_write.get(self.db, execution.auth, arguments["plan_id"])[
                    "company_id"
                ]
            except AppError:
                company = None
        if company:
            with self.db.transaction(readonly=True) as conn:
                try:
                    task_before = tasks.current_in(conn, execution.auth, company)
                except AppError:
                    pass
        try:
            result = self._dispatch(execution, name, arguments)
            if "memory_epoch" in result and not result.get("replayed"):
                execution.expected_epoch = result["memory_epoch"]
            output = {"ok": True, "data": safe_json(result)}
        except AppError as exc:
            if name == "manage_preference" and exc.code in (
                "RESOURCE_NOT_FOUND",
                "VERSION_CONFLICT",
            ):
                visible = preferences.list_preferences(
                    self.db,
                    execution.auth,
                    arguments.get("scope") if arguments.get("scope") != "@user" else None,
                )
                exc.details = {
                    **exc.details,
                    "current_preferences": safe_json(visible),
                    "instruction": "使用当前列表中的准确 preference_id、scope_key、revision 重试；没有匹配项则说明未完成，不能猜对象 ID。",
                }
            output = {"ok": False, "error": exc.as_dict()}
        if output["ok"] and name == "update_invoice" and task_before:
            self._refresh_after_save(
                execution,
                call_id,
                {"company_id": output["data"]["company_id"]},
                output,
                task_before,
            )
        output, ref = self._record(execution, call_id, name, arguments, output)
        if output["ok"] and name in ("query_month", "get_rules", "check_purposes"):
            self._maintain_task(execution, name, arguments, output["data"], ref, task_before)
        return output, ref

    def _refresh_after_save(self, execution, call_id, arguments, output, task):
        if task["intent"] != "complete_purposes" or not task["period"]:
            return
        if output["data"].get("verification_state") != "verified":
            return
        args = {"company_id": arguments["company_id"], "period": task["period"]}
        with self.db.transaction(readonly=True) as conn:
            current = tasks.current_in(conn, execution.auth, args["company_id"])
        if not current or str(current["task_id"]) != str(task["task_id"]):
            return
        try:
            result = business.check_purposes(
                self.db, execution.auth, **args, session_id=execution.session_id
            )
            child = {"ok": True, "data": safe_json(result), "origin": "program_after_save"}
        except AppError as exc:
            child = {"ok": False, "error": exc.as_dict(), "origin": "program_after_save"}
        child, ref = self._record(execution, f"{call_id}:recheck", "check_purposes", args, child)
        if child["ok"]:
            self._maintain_task(execution, "check_purposes", args, child["data"], ref, current)
        output["data"]["post_save_check"] = child
        output["data"]["memory_epoch"] = execution.expected_epoch

    def _dispatch(self, execution, name, args):
        db, auth = self.db, execution.auth
        if args.get("company_id") and name in (
            "query_month",
            "find_invoices",
            "get_rules",
            "check_purposes",
            "get_task",
            "search_history",
        ):
            with db.transaction(readonly=True) as conn:
                source = read_source(conn, auth, execution.source_id, allow_suppressed=True)
                requested = mentioned_companies(conn, source["content"])
                if requested and args["company_id"] not in requested:
                    raise AppError(
                        "TARGET_SCOPE_MISMATCH",
                        "查询企业与本轮用户明确指定的企业不一致，不能替换为其他企业。",
                        422,
                        details={
                            "requested_company_ids": requested,
                            "actual_company_id": args["company_id"],
                        },
                    )
        scope_basis = None
        scope_ref = args.get("scope_ref")
        if name in ("query_month", "get_rules", "check_purposes") or (
            name == "change_task" and args.get("period")
        ):
            with db.transaction(readonly=True) as conn:
                scope_basis = require_period(conn, execution, args["company_id"], args["period"])
                if name != "change_task":
                    validate_scope(
                        conn, execution, args["scope_ref"], args["company_id"], args["period"]
                    )
            args = {k: v for k, v in args.items() if k != "scope_ref"}
        if name in ("prepare_invoice_import", "get_invoice_import"):
            execution.import_context = True
        if name == "resolve_scope":
            with db.transaction() as conn:
                guard(conn, execution, shared=True)
                return bind_scope(conn, execution, args["company_id"], args["period"])
        if name in ("prepare_invoice_write", "update_invoice", "change_task") or (
            name == "manage_preference" and args.get("action") != "list"
        ):
            with db.transaction(readonly=True) as conn:
                attached = conn.execute(
                    "SELECT cardinality(upload_ids)>0 AS attached FROM turns WHERE turn_id=%s",
                    (execution.turn_id,),
                ).fetchone()["attached"]
            if attached or execution.import_context:
                raise AppError(
                    "NEEDS_CLARIFICATION",
                    "附件办理轮次只允许预览、查询和界面确认追加。请在单独消息中明确修改任务、偏好或既有发票。",
                    409,
                )
        if name == "query_month":
            return {
                **business.query_month(db, auth, **args),
                "scope_basis": scope_basis,
                "scope_ref": scope_ref,
            }
        if name == "find_invoices":
            return business.find_invoices(db, auth, **args, session_id=execution.session_id)
        if name == "get_rules":
            return {
                **rules.get_rules(db, auth, **args),
                "scope_basis": scope_basis,
                "scope_ref": scope_ref,
            }
        if name == "check_purposes":
            return {
                **business.check_purposes(db, auth, **args, session_id=execution.session_id),
                "scope_basis": scope_basis,
                "scope_ref": scope_ref,
            }
        if name == "update_invoice":
            return invoice_write.commit(db, auth, args["plan_id"], execution=execution)
        if name == "get_invoice_operation":
            from app.mutations import verify_operation

            return {**verify_operation(db, auth, args["operation_id"]), "status_query": True}
        if name == "prepare_invoice_write":
            return invoice_write.prepare(
                db, auth, **args, source_id=execution.source_id, execution=execution
            )
        if name == "get_task":
            with db.transaction(readonly=True) as conn:
                return {"task": safe_json(tasks.current_in(conn, auth, args["company_id"]))}
        if name == "change_task":
            return tasks.change_task(
                db,
                auth,
                **args,
                execution=execution,
                request_id=f"tool:{execution.source_id}:{digest(args)}",
            )
        if name == "manage_preference":
            if args["action"] == "list":
                return {
                    "preferences": safe_json(
                        preferences.list_preferences(
                            db, auth, args.get("scope") if args.get("scope") != "@user" else None
                        )
                    )
                }
            return preferences.change_preference(
                db,
                auth,
                **args,
                execution=execution,
                request_id=f"tool:{execution.source_id}:{digest(args)}",
            )
        if name == "prepare_invoice_import":
            from app.invoice_imports import InvoiceImports

            return InvoiceImports(db).prepare(execution, **args)
        if name == "get_invoice_import":
            from app.invoice_imports import InvoiceImports

            return InvoiceImports(db).get(auth, args["batch_id"], args.get("cursor", 0))
        return memory.search_history(db, auth, **args, before_source_seq=execution.source_seq)

    def _record(self, execution, call_id, name, arguments, output):
        with self.db.transaction() as conn:
            guard(conn, execution)
            result_id = uid()
            output = {**output, "result_ref": result_id}
            companies = []
            result_company = arguments.get("company_id") or (output.get("data") or {}).get(
                "company_id"
            )
            if result_company:
                try:
                    assert_access(conn, execution.auth, result_company)
                    companies = [result_company]
                except AppError:
                    pass
            source = add_source(
                conn,
                execution.auth,
                output,
                role="tool",
                kind="tool_result",
                turn_id=execution.turn_id,
                source_seq=execution.source_seq,
                companies=companies,
                source_id=result_id,
            )
            assess(conn, str(source["source_id"]), "no_change")
            conn.execute(
                "INSERT INTO tool_executions(run_id,tool_call_id,name,arguments,arguments_hash,state,result_source_id,operation_id) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(run_id,tool_call_id) DO UPDATE SET "
                "state=excluded.state,result_source_id=excluded.result_source_id,operation_id=excluded.operation_id",
                (
                    execution.run_id,
                    call_id,
                    name,
                    jsonb(arguments),
                    digest([name, arguments]),
                    "done" if output["ok"] else "failed",
                    source["source_id"],
                    (output.get("data") or {}).get("operation_id"),
                ),
            )
            view_id = (output.get("data") or {}).get("view_id")
            if view_id:
                conn.execute(
                    "UPDATE result_views SET result_source_id=%s WHERE view_id=%s AND user_id=%s",
                    (source["source_id"], view_id, execution.auth.user_id),
                )
            emit(
                conn,
                execution.turn_id,
                execution.run_id,
                "tool_finished",
                {"name": name, **output},
                tool_call_id=call_id,
                result_ref=source["source_id"],
            )
            return safe_json(output), {
                "source_id": str(source["source_id"]),
                "revision": source["revision"],
            }

    def _maintain_task(self, execution, name, args, result, ref, task):
        intent = {
            "query_month": "query_month",
            "get_rules": "explain_rules",
            "check_purposes": "complete_purposes",
        }[name]
        with self.db.transaction(readonly=True) as conn:
            source = read_source(conn, execution.auth, execution.source_id, allow_suppressed=True)
        refs = [{"source_id": execution.source_id, "quote": source["content"]}]
        if not task and name == "check_purposes":
            task = tasks.change_task(
                self.db,
                execution.auth,
                company_id=args["company_id"],
                action="start",
                intent=intent,
                period=args["period"],
                request_id=f"auto-start:{execution.source_id}:{args['company_id']}",
                source_refs=refs,
                execution=execution,
            )
            execution.expected_epoch = task["memory_epoch"]
        if task and task["intent"] == intent and not task["period"]:
            task = tasks.change_task(
                self.db,
                execution.auth,
                company_id=args["company_id"],
                action="update",
                target_id=str(task["task_id"]),
                expected_revision=task["revision"],
                intent=intent,
                period=args["period"],
                request_id=f"auto-period:{execution.source_id}:{task['task_id']}",
                source_refs=refs,
                execution=execution,
            )
            execution.expected_epoch = task["memory_epoch"]
        if task and task["intent"] == intent and task["period"] == args["period"]:
            updated = tasks.apply_result(
                self.db,
                execution.auth,
                task,
                result,
                ref["source_id"],
                execution.source_seq,
                execution=execution,
            )
            execution.expected_epoch = updated["memory_epoch"]
