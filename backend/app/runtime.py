import asyncio
import json
import time
from contextlib import suppress

import psycopg
from pydantic import ValidationError

from app import context, tasks
from app.answer_renderer import render
from app.config import Settings
from app.errors import AppError
from app.events import emit
from app.execution import Execution, guard
from app.identity import Identity, assert_access, owned
from app.model import ModelAdapter
from app.sources import add_source, advance_progress, assess, read_source
from app.tool_schemas import FinalReply
from app.tools import ToolExecutor
from app.trace import record_context
from app.turns import mentioned_companies
from app.values import canonical, digest, jsonb, uid


class Runtime:
    def __init__(self, db, settings: Settings, model: ModelAdapter, *, retrieval=None):
        self.db, self.settings, self.model = db, settings, model
        if retrieval is None:
            from app.knowledge_retrieval import KnowledgeRetrieval
            from app.retrieval_engine import LocalRetrieval

            retrieval = KnowledgeRetrieval(
                db, settings, LocalRetrieval(settings.knowledge_model_directory)
            )
        self.tools = ToolExecutor(db, retrieval)
        self.stopping = asyncio.Event()
        self.workers: list[asyncio.Task] = []

    async def start(self):
        self.workers = [
            asyncio.create_task(self._worker()) for _ in range(self.settings.worker_count)
        ]

    async def stop(self):
        self.stopping.set()
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)

    def claim(self) -> Execution | None:
        from app.recovery import in_maintenance

        if in_maintenance(self.settings.database_url):
            return None
        with self.db.transaction() as conn:
            user = conn.execute(
                "SELECT u.* FROM users u WHERE EXISTS(SELECT 1 FROM work_items w JOIN sources s USING(source_id) "
                "JOIN turns t ON t.turn_id=s.turn_id WHERE w.user_id=u.user_id AND w.kind='turn' "
                "AND (w.state='ready' OR (w.state='running' AND w.lease_until<clock_timestamp())) "
                "AND t.status IN ('queued','running')) AND NOT EXISTS(SELECT 1 FROM work_items x "
                "WHERE x.user_id=u.user_id AND x.state='running' AND x.lease_until>clock_timestamp()) "
                "ORDER BY u.user_id FOR UPDATE OF u SKIP LOCKED LIMIT 1"
            ).fetchone()
            if not user:
                return None
            candidate = conn.execute(
                "SELECT w.work_id,s.* FROM work_items w JOIN sources s USING(source_id) JOIN turns t USING(turn_id) "
                "WHERE w.user_id=%s AND w.kind='turn' AND t.status IN ('queued','running') "
                "AND (w.state='ready' OR (w.state='running' AND w.lease_until<clock_timestamp())) ORDER BY s.source_seq LIMIT 1",
                (user["user_id"],),
            ).fetchone()
            if not candidate:
                return None
            turn = conn.execute(
                "SELECT * FROM turns WHERE turn_id=%s FOR UPDATE", (candidate["turn_id"],)
            ).fetchone()
            work = conn.execute(
                "SELECT * FROM work_items WHERE work_id=%s FOR UPDATE", (candidate["work_id"],)
            ).fetchone()
            if candidate["recall_state"] != "active":
                conn.execute(
                    "UPDATE turns SET status='interrupted',error=%s WHERE turn_id=%s",
                    (
                        jsonb(
                            {
                                "code": "SOURCE_INVALIDATED",
                                "message": "原指令包含已更正或删除的信息，请按当前任务发新消息继续。",
                            }
                        ),
                        turn["turn_id"],
                    ),
                )
                conn.execute(
                    "UPDATE work_items SET state='invalidated' WHERE work_id=%s", (work["work_id"],)
                )
                assess(conn, str(candidate["source_id"]), "invalidated")
                emit(conn, str(turn["turn_id"]), None, "failed", {"code": "SOURCE_INVALIDATED"})
                return None
            session = conn.execute(
                "SELECT * FROM sessions WHERE session_id=%s", (turn["session_id"],)
            ).fetchone()
            if candidate["scope_kind"] == "user_private" and session["selected_company_id"]:
                assert_access(conn, Identity(user["user_id"]), session["selected_company_id"])
                conn.execute(
                    "INSERT INTO source_scopes VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (candidate["source_id"], session["selected_company_id"]),
                )
                conn.execute(
                    "UPDATE sources SET scope_kind='company_scoped' WHERE source_id=%s",
                    (candidate["source_id"],),
                )
            generation = turn["active_run_generation"] + 1
            token = work["fencing_token"] + 1
            run_id = uid()
            conn.execute(
                "UPDATE runs SET state='interrupted',finished_at=now() WHERE turn_id=%s AND finished_at IS NULL",
                (turn["turn_id"],),
            )
            conn.execute(
                "UPDATE turns SET status='running',active_run_generation=%s,error=NULL WHERE turn_id=%s",
                (generation, turn["turn_id"]),
            )
            conn.execute(
                "UPDATE work_items SET state='running',fencing_token=%s,attempts=attempts+1,lease_until=clock_timestamp()+%s*interval '1 second' WHERE work_id=%s",
                (token, self.settings.lease_seconds, work["work_id"]),
            )
            conn.execute(
                "INSERT INTO runs(run_id,turn_id,generation,state,config_version) VALUES(%s,%s,%s,'running',%s)",
                (
                    run_id,
                    turn["turn_id"],
                    generation,
                    digest([self.settings.model_id, self.settings.input_budget]),
                ),
            )
            return Execution(
                Identity(user["user_id"]),
                str(turn["turn_id"]),
                str(turn["session_id"]),
                str(candidate["source_id"]),
                candidate["source_seq"],
                str(work["work_id"]),
                run_id,
                generation,
                token,
                user["memory_epoch"],
            )

    async def _worker(self):
        while not self.stopping.is_set():
            try:
                execution = await asyncio.to_thread(self.claim)
                if execution:
                    await self.run(execution)
                else:
                    await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                raise
            except (psycopg.Error, AppError):
                await asyncio.sleep(1)

    def renew(self, execution):
        with self.db.transaction() as conn:
            return (
                conn.execute(
                    "UPDATE work_items SET lease_until=clock_timestamp()+%s*interval '1 second' WHERE work_id=%s AND fencing_token=%s AND state='running' RETURNING work_id",
                    (self.settings.lease_seconds, execution.work_id, execution.fencing_token),
                ).fetchone()
                is not None
            )

    async def _heartbeat(self, execution):
        while True:
            await asyncio.sleep(min(20, self.settings.lease_seconds / 3))
            if not await asyncio.to_thread(self.renew, execution):
                return

    async def run(self, execution: Execution):
        heartbeat = asyncio.create_task(self._heartbeat(execution))
        try:
            async with asyncio.timeout(self.settings.turn_timeout):
                with self.db.transaction(readonly=True) as conn:
                    turn = conn.execute(
                        "SELECT request_kind,control_ref FROM turns WHERE turn_id=%s",
                        (execution.turn_id,),
                    ).fetchone()
                if turn["request_kind"] == "write_confirmation":
                    await self._confirm_loop(execution, str(turn["control_ref"]))
                else:
                    await self._loop(execution)
        except asyncio.CancelledError:
            # Leave the durable lease for recovery; cancellation API uses a separate fence.
            raise
        except TimeoutError:
            await asyncio.to_thread(
                self.fail,
                execution,
                AppError("RUN_LIMIT_EXCEEDED", "本轮处理超时，已提交操作仍保留，可继续核对。", 503),
            )
        except AppError as exc:
            await asyncio.to_thread(self.fail, execution, exc)
        except Exception as exc:
            await asyncio.to_thread(
                self.fail,
                execution,
                AppError(
                    "PROCESSING_FAILED",
                    "本轮未能完成，已保存的资料与操作可在记录中核对。",
                    503,
                    details={"type": type(exc).__name__},
                ),
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _loop(self, execution):
        groups = []
        tool_count = 0
        retry_used = False
        stale_retry = False
        reply_retries = 0
        budget = None
        compaction_attempted = False
        for step in range(1, self.settings.max_model_steps + 1):
            try:
                payload, manifest, _dropped = await asyncio.to_thread(
                    context.assemble, self.db, self.settings, execution, groups, budget=budget
                )
            except AppError as exc:
                if exc.code == "STALE_CONTEXT" and not stale_retry:
                    with self.db.transaction(readonly=True) as conn:
                        epoch = conn.execute(
                            "SELECT memory_epoch FROM users WHERE user_id=%s",
                            (execution.auth.user_id,),
                        ).fetchone()["memory_epoch"]
                    if epoch != execution.expected_epoch:
                        raise
                    groups = []
                    stale_retry = True
                    payload, manifest, _dropped = await asyncio.to_thread(
                        context.assemble, self.db, self.settings, execution, groups, budget=budget
                    )
                else:
                    raise
            if _dropped and not compaction_attempted and step < self.settings.max_model_steps:
                from app.compaction import compact

                compaction_attempted = True
                try:
                    await compact(self.db, self.settings, self.model, execution, _dropped, step)
                except AppError as exc:
                    if exc.code in ("SOURCE_INVALIDATED", "STALE_CONTEXT"):
                        raise
                    with self.db.transaction() as conn:
                        emit(
                            conn,
                            execution.turn_id,
                            execution.run_id,
                            "compaction_skipped",
                            exc.as_dict(),
                        )
                continue
            execution.observed_heads = manifest["heads"]
            with self.db.transaction() as conn:
                context.validate(self.db, execution, payload, manifest, conn=conn)
                context_id = record_context(conn, execution, step, payload, manifest)
            started = time.monotonic()
            try:
                response = await self.model.complete(payload)
            except AppError as exc:
                if exc.code == "CONTEXT_CAPACITY_EXCEEDED" and budget is None:
                    budget = int(self.settings.context_budget * 0.7)
                    continue
                if exc.retryable and not retry_used:
                    retry_used = True
                    await asyncio.sleep(0.5)
                    continue
                raise
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE context_snapshots SET usage=%s,duration_ms=%s WHERE context_id=%s",
                    (
                        jsonb(response.get("usage")) if response.get("usage") else None,
                        round((time.monotonic() - started) * 1000),
                        context_id,
                    ),
                )
                emit(
                    conn,
                    execution.turn_id,
                    execution.run_id,
                    "model_finished",
                    {
                        "step": step,
                        "message": response["message"],
                        "finish_reason": response.get("finish_reason"),
                    },
                    input_ref=context_id,
                )
            message = response["message"]
            calls = message.get("tool_calls") or []
            if calls:
                results, refs, tool_messages = [], [], []
                for call in calls:
                    tool_count += 1
                    if tool_count > self.settings.max_tool_calls:
                        raise AppError(
                            "RUN_LIMIT_EXCEEDED",
                            "本轮工具调用已到上限，已完成项可在记录核对。",
                            422,
                        )
                    output, ref = await asyncio.to_thread(self.tools.execute, execution, call)
                    results.append(output)
                    refs.append(ref)
                    tool_messages.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": canonical(output)}
                    )
                groups.append(
                    {
                        "messages": [message, *tool_messages],
                        "sources": refs,
                        "results": results,
                        # Each tool is guarded against external memory changes. Own writes
                        # advance expected_epoch; retain the closed batch at its final epoch.
                        # Source revisions and business heads are still checked by assemble.
                        "memory_epoch": execution.expected_epoch,
                    }
                )
                continue
            try:
                reply = FinalReply.model_validate_json(message.get("content") or "")
                await asyncio.to_thread(self.finish, execution, reply, payload, manifest)
                return
            except (ValidationError, json.JSONDecodeError, AppError) as exc:
                if isinstance(exc, AppError) and exc.code != "REPLY_EVIDENCE_MISSING":
                    raise
                if reply_retries >= 2:
                    raise AppError(
                        "MODEL_PROTOCOL_UNSUPPORTED",
                        "模型最终答复格式无效，未把它当作成功结果。",
                        422,
                    ) from exc
                reply_retries += 1
                if isinstance(exc, ValidationError):
                    feedback = [
                        {"field": ".".join(str(x) for x in e["loc"]), "message": e["msg"]}
                        for e in exc.errors(include_input=False, include_url=False)
                    ]
                else:
                    feedback = (
                        exc.as_dict()
                        if isinstance(exc, AppError)
                        else {"message": "仅接受一个合法 JSON 对象。"}
                    )
                groups.append(
                    {
                        "messages": [
                            {"role": "assistant", "content": message.get("content") or ""},
                            {
                                "role": "system",
                                "content": "最终答复未通过校验，尚未交付用户。请按错误修正，仅输出约定 JSON；如缺少保存证据，应先调用工具完成用户明确要求，不能只改措辞声称成功。错误："
                                + canonical(feedback),
                            },
                        ],
                        "sources": [],
                        "results": [],
                        "memory_epoch": execution.expected_epoch,
                    }
                )
        raise AppError(
            "RUN_LIMIT_EXCEEDED", "本轮模型调用到达上限，已提交数据保留，请继续核对。", 422
        )

    async def _confirm_loop(self, execution, plan_id):
        call = {
            "id": "confirmed-invoice-write",
            "type": "function",
            "function": {
                "name": "update_invoice",
                "arguments": canonical({"plan_id": plan_id}),
            },
        }
        output, ref = await asyncio.to_thread(self.tools.execute, execution, call)
        if not output.get("ok"):
            error = output["error"]
            raise AppError(error["code"], error["message"], 409)
        group = {
            "messages": [
                {"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "tool", "tool_call_id": call["id"], "content": canonical(output)},
            ],
            "sources": [ref],
            "results": [output],
            "memory_epoch": execution.expected_epoch,
        }
        payload, manifest, _ = await asyncio.to_thread(
            context.assemble,
            self.db,
            self.settings,
            execution,
            [group],
            budget=self.settings.input_budget,
        )
        manifest["kind"] = "control_execution"
        with self.db.transaction() as conn:
            record_context(conn, execution, 1, payload, manifest)
        with self.db.transaction(readonly=True) as conn:
            origin = conn.execute(
                "SELECT t.turn_id FROM write_plans p JOIN sources s ON s.source_id=p.origin_source_id "
                "JOIN turns t ON t.turn_id=s.turn_id WHERE p.plan_id=%s AND t.status='waiting_user'",
                (plan_id,),
            ).fetchone()
        reply = FinalReply.model_validate(
            {
                "blocks": [
                    {"type": "operation", "operation_id": str(output["data"]["operation_id"])}
                ],
                "resume_turn_id": str(origin["turn_id"]) if origin else None,
            }
        )
        await asyncio.to_thread(self.finish, execution, reply, payload, manifest)

    def _waiting_task_in(self, conn, execution, pending):
        company = pending.get("company_id")
        if not company:
            return pending
        assert_access(conn, execution.auth, company)
        task = tasks.current_in(conn, execution.auth, company)
        if task:
            return {
                **pending,
                "task_id": str(task["task_id"]),
                "task_generation": task["generation"],
            }
        if pending["intent"] not in tasks.INTENTS:
            return pending
        slot = tasks._slot(conn, execution.auth, company)
        if execution.source_seq <= slot["authority_source_seq"]:
            raise AppError("SOURCE_INVALIDATED", "旧来源不能重建已结束的任务。", 409)
        task = conn.execute(
            "INSERT INTO tasks(task_id,user_id,company_id,period,intent,status,generation,authority_source_seq,unresolved_json) VALUES(%s,%s,%s,%s,%s,'waiting_user',%s,%s,%s) RETURNING *",
            (
                uid(),
                execution.auth.user_id,
                company,
                pending.get("period"),
                pending["intent"],
                slot["generation"] + 1,
                execution.source_seq,
                jsonb(
                    {"missing_fields": pending["missing_fields"], "question": pending["summary"]}
                ),
            ),
        ).fetchone()
        conn.execute(
            "UPDATE task_slots SET current_task_id=%s,generation=%s,revision=revision+1,authority_source_seq=%s WHERE user_id=%s AND company_id=%s",
            (
                task["task_id"],
                task["generation"],
                execution.source_seq,
                execution.auth.user_id,
                company,
            ),
        )
        tasks._revision(
            conn, task, "waiting_user", [{"source_id": execution.source_id}], execution.source_seq
        )
        conn.execute(
            "UPDATE users SET memory_epoch=memory_epoch+1 WHERE user_id=%s",
            (execution.auth.user_id,),
        )
        return {**pending, "task_id": str(task["task_id"]), "task_generation": task["generation"]}

    def finish(self, execution, reply: FinalReply, payload, manifest):
        with self.db.transaction() as conn:
            context.validate(self.db, execution, payload, manifest, conn=conn)
            # Upgrade the same locks before committing state, answer and events atomically.
            guard(conn, execution)
            reply = render(conn, execution, reply, payload, manifest)
            if reply.preference_status == "committed":
                evidence = conn.execute(
                    "SELECT result_ref FROM memory_change_ops WHERE user_id=%s AND target_type='preference' AND action=%s AND state='committed' AND request_id LIKE %s",
                    (
                        execution.auth.user_id,
                        reply.preference_action,
                        f"tool:{execution.source_id}:%",
                    ),
                ).fetchall()
                if not evidence:
                    raise AppError(
                        "REPLY_EVIDENCE_MISSING",
                        "本轮没有该偏好操作的成功记录，不能声称已记住、更正或删除。",
                        422,
                        details={"action": reply.preference_action},
                    )
            if reply.resume_turn_id:
                old = owned(
                    conn, "turns", "turn_id", str(reply.resume_turn_id), execution.auth, lock=True
                )
                already_closed_here = (
                    old["status"] == "resolved" and old["resolved_by_turn_id"] is None
                )
                if already_closed_here:
                    old_task = (old["pending_clarification"] or {}).get("task_id")
                    task_row = (
                        conn.execute(
                            "SELECT authority_source_seq FROM tasks WHERE task_id=%s AND user_id=%s",
                            (old_task, execution.auth.user_id),
                        ).fetchone()
                        if old_task
                        else None
                    )
                    already_closed_here = bool(
                        task_row and task_row["authority_source_seq"] == execution.source_seq
                    )
                if old["status"] != "waiting_user" and not already_closed_here:
                    raise AppError("SOURCE_INVALIDATED", "所关联的待澄清事项已结束。", 409)
                conn.execute(
                    "UPDATE turns SET status='resolved',resolved_by_turn_id=%s,pending_clarification=jsonb_set(pending_clarification,'{state}','\"resolved\"') WHERE turn_id=%s",
                    (execution.turn_id, reply.resume_turn_id),
                )
                conn.execute(
                    "UPDATE turns SET continuation_of_turn_id=%s WHERE turn_id=%s",
                    (reply.resume_turn_id, execution.turn_id),
                )
                conn.execute(
                    "UPDATE users SET memory_epoch=memory_epoch+1 WHERE user_id=%s",
                    (execution.auth.user_id,),
                )
                conn.execute(
                    "UPDATE summaries SET status='invalidated' WHERE user_id=%s AND status='active'",
                    (execution.auth.user_id,),
                )
            pending = None
            if reply.status == "waiting_user":
                if not reply.clarification:
                    raise AppError("INVALID_ARGUMENT", "等待补充的答复需要明确缺失字段。")
                pending = reply.clarification.model_dump(mode="json")
                if pending.get("period"):
                    from app.values import month_bounds

                    month_bounds(pending["period"])
                pending = self._waiting_task_in(conn, execution, pending)
                pending.update(
                    state="waiting_user",
                    generation=1,
                    revision=1,
                    authority_source_seq=execution.source_seq,
                )
            cards = reply.cards
            if reply.displayed_view_ids and not {str(x) for x in reply.displayed_view_ids} <= {
                str(c.get("view_id")) for c in cards
            }:
                raise AppError("SOURCE_INVALIDATED", "答复引用了无效或未查询的列表。", 409)
            user_source = read_source(
                conn, execution.auth, execution.source_id, allow_suppressed=True
            )
            companies = mentioned_companies(conn, user_source["content"])
            for company in companies:
                # An unauthorized question must not change the trusted company selector.
                try:
                    assert_access(conn, execution.auth, company)
                except AppError:
                    companies = []
                    break
            if len(companies) == 1:
                conn.execute(
                    "UPDATE sessions SET selected_company_id=%s,updated_at=now() WHERE session_id=%s",
                    (companies[0], execution.session_id),
                )
            answer_companies = sorted(
                {c["company_id"] for c in cards if c.get("company_id")} | set(companies)
            )
            if pending and pending.get("company_id"):
                answer_companies = sorted(set(answer_companies) | {pending["company_id"]})
            source = add_source(
                conn,
                execution.auth,
                reply.message,
                role="assistant",
                kind="assistant_final",
                turn_id=execution.turn_id,
                source_seq=execution.source_seq,
                companies=answer_companies,
            )
            assess(conn, str(source["source_id"]), "no_change")
            assess(conn, execution.source_id, "accepted" if pending else "no_change")
            advance_progress(conn, execution.auth)
            status = "waiting_user" if pending else "completed"
            conn.execute(
                "UPDATE turns SET status=%s,pending_clarification=%s,final_message=%s,final_response_ref=%s,final_cards=%s,updated_at=now() WHERE turn_id=%s",
                (
                    status,
                    jsonb(pending) if pending else None,
                    reply.message,
                    source["source_id"],
                    jsonb(cards),
                    execution.turn_id,
                ),
            )
            conn.execute(
                "UPDATE runs SET state=%s,finished_at=now(),final_source_id=%s WHERE run_id=%s",
                (status, source["source_id"], execution.run_id),
            )
            conn.execute(
                "UPDATE work_items SET state='done',lease_until=NULL WHERE work_id=%s AND fencing_token=%s",
                (execution.work_id, execution.fencing_token),
            )
            emit(
                conn,
                execution.turn_id,
                execution.run_id,
                "final",
                {"message": reply.message, "status": status, "cards": cards},
            )

    def fail(self, execution, error: AppError):
        with self.db.transaction() as conn:
            conn.execute(
                "SELECT 1 FROM users WHERE user_id=%s FOR UPDATE", (execution.auth.user_id,)
            )
            turn = conn.execute(
                "SELECT * FROM turns WHERE turn_id=%s FOR UPDATE", (execution.turn_id,)
            ).fetchone()
            work = conn.execute(
                "SELECT * FROM work_items WHERE work_id=%s FOR UPDATE", (execution.work_id,)
            ).fetchone()
            if (
                turn["active_run_generation"] != execution.generation
                or work["fencing_token"] != execution.fencing_token
                or turn["status"] != "running"
            ):
                return
            status = (
                "interrupted" if error.code in ("STALE_CONTEXT", "SOURCE_INVALIDATED") else "failed"
            )
            conn.execute(
                "UPDATE turns SET status=%s,error=%s,updated_at=now() WHERE turn_id=%s",
                (status, jsonb(error.as_dict()), execution.turn_id),
            )
            conn.execute(
                "UPDATE runs SET state=%s,finished_at=now() WHERE run_id=%s",
                (status, execution.run_id),
            )
            conn.execute(
                "UPDATE work_items SET state='failed',last_error=%s,lease_until=NULL WHERE work_id=%s",
                (jsonb(error.as_dict()), execution.work_id),
            )
            assess(conn, execution.source_id, "failed", [])
            advance_progress(conn, execution.auth)
            emit(conn, execution.turn_id, execution.run_id, "failed", error.as_dict())
