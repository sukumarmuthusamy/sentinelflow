"""The M5-C Streamlit harness page.

Execution model (ADR-0003 "Harness UI"):

* All state lives in ``st.session_state``; a rerun only re-renders it. API writes
  happen only inside a submit handler, so reruns never create jobs.
* Creates send an ``Idempotency-Key`` derived from the request body
  (``SubmissionKeys``): a double click or a retry returns the job already created.
* Progress is read in a fragment that reruns every ``poll_seconds()`` while the job
  is queued or running. Each run reads the SSE stream for a bounded window from
  the durable cursor, then disconnects. No background threads, no long-lived
  connection, at most one subscription per session.
* A job waiting for approval is not streamed. The page shows the decision and the
  approval form, and resumes streaming only after an explicit approve or reject.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import streamlit as st

from ui.harness.client import (
    ApiError,
    ApiUnavailableError,
    SentinelFlowClient,
    is_loopback_url,
    validate_job_id,
)
from ui.harness.progress import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    JobProgress,
    SubmissionKeys,
)

DEFAULT_API_URL = "http://127.0.0.1:8000"


def _seconds(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def poll_seconds() -> float:
    """How often the progress fragment reruns while a job is queued or running."""
    return _seconds("SENTINELFLOW_UI_POLL_SECONDS", 1.0)


def read_window_seconds() -> float:
    """How long one poll reads the event stream before disconnecting."""
    return _seconds("SENTINELFLOW_UI_READ_WINDOW_SECONDS", 1.5)


# Bundled fixture packs and change ids, offered as suggestions; any id can be typed.
FIXTURE_CHANGES: dict[str, list[str]] = {
    "healthcare-claims": ["CHG-H001", "CHG-H002", "CHG-H003", "CHG-H004"],
    "ecommerce-payments": ["CHG-E001", "CHG-E002", "CHG-E003", "CHG-E004"],
}
DECISION_COLORS = {"PASS": "#1a7f37", "REVIEW": "#9a6700", "BLOCK": "#cf222e"}
STATUS_COLORS = {
    "queued": "gray",
    "running": "blue",
    "waiting_for_approval": "orange",
    "completed": "green",
    "failed": "red",
}
STATUS_LABELS = {
    "queued": "Queued",
    "running": "Running",
    "waiting_for_approval": "Waiting for approval",
    "completed": "Completed",
    "failed": "Failed",
}

_CSS = """
<style>
.block-container {padding-top: 2rem; max-width: 1200px;}
.sf-decision {border-radius: 10px; padding: 1.1rem 1.4rem; color: #fff; margin-bottom: .6rem;}
.sf-decision .label {font-size: .8rem; letter-spacing: .08em; text-transform: uppercase;
  opacity: .85;}
.sf-decision .value {font-size: 2.4rem; font-weight: 700; line-height: 1.1;}
.sf-decision .note {font-size: .85rem; opacity: .9; margin-top: .2rem;}
.sf-muted {color: #6e7781; font-size: .85rem;}
</style>
"""


# -- session state ----------------------------------------------------------------------


def _state() -> Any:
    ss = st.session_state
    ss.setdefault("analysis_keys", SubmissionKeys())
    ss.setdefault("evaluation_keys", SubmissionKeys())
    ss.setdefault("job_id", None)
    ss.setdefault("job", None)
    ss.setdefault("progress", None)
    ss.setdefault("decision", None)
    ss.setdefault("job_error", None)
    ss.setdefault("approval_sent", None)
    ss.setdefault("approval_message", None)
    ss.setdefault("launch_message", None)
    ss.setdefault("eval_job_id", None)
    ss.setdefault("eval_job", None)
    ss.setdefault("eval_result", None)
    ss.setdefault("eval_error", None)
    return ss


def _select_job(job: dict[str, Any]) -> None:
    """Make ``job`` the current job; a different job starts a fresh progress view."""
    ss = st.session_state
    if ss.job_id != job["job_id"]:
        ss.job_id = job["job_id"]
        ss.progress = JobProgress(job_id=job["job_id"])
        ss.decision = None
        ss.approval_sent = None
        ss.approval_message = None
    ss.job = job
    ss.job_error = None


# -- connection -------------------------------------------------------------------------


def _connection() -> tuple[str, str | None]:
    with st.sidebar:
        st.markdown("### API connection")
        base_url = st.text_input(
            "SentinelFlow API URL",
            value=os.getenv("SENTINELFLOW_API_URL", DEFAULT_API_URL),
            key="api_url",
            help="Loopback by default. The harness talks to the API over HTTP only.",
        ).strip()
        typed_key = st.text_input(
            "API key (only if the server sets API_KEY)",
            type="password",
            key="api_key_input",
            help="Kept in this browser session only. Or set SENTINELFLOW_API_KEY "
            "in the environment that starts the UI; it is never shown.",
        )
        env_key = os.getenv("SENTINELFLOW_API_KEY", "").strip()
        api_key = typed_key.strip() or env_key or None
        if api_key and not typed_key.strip():
            st.caption("Using the API key from SENTINELFLOW_API_KEY.")
        if api_key and base_url.startswith("http://") and not is_loopback_url(base_url):
            st.warning("The API key would be sent unencrypted to a non-loopback host.")
        if st.button("Check connection", key="check_connection"):
            with SentinelFlowClient(base_url, api_key=api_key, timeout_seconds=3) as client:
                try:
                    health = client.health()
                except (ApiError, ApiUnavailableError) as exc:
                    _show_error(exc)
                else:
                    st.success(f"Connected: {health.get('service', 'SentinelFlow')}")
        st.divider()
        st.markdown(
            "<span class='sf-muted'>Local developer harness for the M5 API. "
            "Fixture analyses and the evaluation run offline with a fake or "
            "configured provider; live model calls need provider keys on the "
            "server.</span>",
            unsafe_allow_html=True,
        )
    return base_url, api_key


def _describe(exc: ApiError | ApiUnavailableError) -> str:
    if isinstance(exc, ApiUnavailableError):
        return exc.message
    return f"{exc.code}: {exc.message}"


def _client(base_url: str, api_key: str | None) -> SentinelFlowClient:
    return SentinelFlowClient(base_url, api_key=api_key)


def _show_error(exc: ApiError | ApiUnavailableError, *, context: str | None = None) -> None:
    prefix = f"{context}: " if context else ""
    if isinstance(exc, ApiUnavailableError):
        st.error(f"{prefix}{exc.message}", icon=":material/cloud_off:")
        return
    text = f"{prefix}**{exc.code}**: {exc.message}"
    if exc.request_id:
        text += f"  \nRequest id `{exc.request_id}`"
    errors = exc.details.get("errors")
    if isinstance(errors, list):
        for item in errors[:5]:
            if isinstance(item, dict):
                loc = ".".join(str(part) for part in item.get("loc", []) if part != "body")
                text += f"  \n- `{loc}`: {item.get('msg', '')}"
    st.error(text, icon=":material/error:")


# -- analysis workspace -----------------------------------------------------------------


def _launch_form(client: SentinelFlowClient) -> None:
    ss = st.session_state
    st.markdown("#### New analysis")
    # Plain widgets (not a form), so the change-id suggestions follow the pack. Widget
    # changes only rerun the page; a job is created only by the button click below.
    with st.container(border=True):
        left, right = st.columns(2)
        with left:
            pack = st.selectbox(
                "Domain pack",
                options=list(FIXTURE_CHANGES),
                accept_new_options=True,
                key="form_pack",
                help="A bundled pack, or type another pack id.",
            )
            source = st.radio(
                "Change source",
                options=["fixture", "github"],
                horizontal=True,
                key="form_source",
                help="fixture: recorded change packages, offline. github: the "
                "repository configured on the server (needs a token there).",
            )
        with right:
            suggestions = FIXTURE_CHANGES.get(str(pack or ""), [])
            change_id = st.selectbox(
                "Change id",
                options=suggestions,
                accept_new_options=True,
                key="form_change",
                help="Fixture change ids are suggested; type a PR change id for GitHub.",
            )
            objective = st.text_input(
                "Objective (optional)",
                max_chars=500,
                key="form_objective",
                placeholder="Default: assess release risk and the minimum regression plan",
            )
        record = st.checkbox(
            "Require human approval, then record the gate decision in the audit log",
            key="form_record",
        )
        execute = st.checkbox(
            "Request test execution (always a dry run in M5)",
            key="form_execute",
        )
        submitted = st.button("Launch analysis", type="primary", key="launch")
    if not submitted:
        return
    if not pack or not change_id:
        st.warning("Choose a domain pack and a change id.")
        return
    body: dict[str, Any] = {
        "change_id": str(change_id).strip(),
        "domain_pack_id": str(pack).strip(),
        "source": source,
        "record_decision": bool(record),
        "execute_tests": bool(execute),
    }
    if objective and objective.strip():
        body["objective"] = objective.strip()
    key = ss.analysis_keys.key_for(body)
    try:
        created = client.create_analysis(body, idempotency_key=key)
    except ApiUnavailableError as exc:
        _show_error(exc, context="Launch")
        if exc.ambiguous:
            st.info(
                "The request may have reached the server. Submitting the same form "
                "again reuses its idempotency key, so it cannot start a second job."
            )
        return
    except ApiError as exc:
        _show_error(exc, context="Launch")
        return
    _select_job(created.job)
    ss.launch_message = (
        "This submission was already accepted; showing its existing job (idempotent replay)."
        if created.replayed
        else f"Started job `{created.job['job_id']}`."
    )


def _open_form(client: SentinelFlowClient) -> None:
    st.markdown("#### Open an existing job")
    with st.form("open_job", border=True):
        raw = st.text_input("Job id", key="open_job_id", placeholder="job-…")
        opened = st.form_submit_button("Open job")
    if not opened:
        return
    try:
        job = client.get_analysis(validate_job_id(raw))
    except (ApiError, ApiUnavailableError) as exc:
        _show_error(exc, context="Open job")
        return
    _select_job(job)
    st.session_state.launch_message = f"Opened job `{job['job_id']}` (read-only; not re-run)."


def _new_submission_button() -> None:
    if st.button(
        "New submission",
        key="new_submission",
        help="Forget the current idempotency key and job. The next launch "
        "creates a new job even with the same inputs.",
    ):
        ss = st.session_state
        ss.analysis_keys.reset()
        ss.job_id = None
        ss.job = None
        ss.progress = None
        ss.decision = None
        ss.launch_message = None
        st.rerun()


# -- progress ---------------------------------------------------------------------------


def _needs_stream(job: dict[str, Any], progress: JobProgress) -> bool:
    status = job["status"]
    if status in ACTIVE_STATUSES:
        return True
    last = progress.lifecycle[-1]["type"] if progress.lifecycle else None
    expected = {
        "completed": "job.completed",
        "failed": "job.failed",
        "waiting_for_approval": "job.waiting_for_approval",
    }.get(status)
    return expected is not None and last != expected


def _refresh(client: SentinelFlowClient) -> bool:
    """One poll: read a stream window, re-read the job and its decision.

    Returns True when the job's status changed (the caller reruns the page).
    """
    ss = st.session_state
    job, progress = ss.job, ss.progress
    before = job["status"]
    if _needs_stream(job, progress):
        try:
            batch = client.read_events(
                job["job_id"],
                after_seq=progress.cursor,
                window_seconds=read_window_seconds(),
            )
        except (ApiError, ApiUnavailableError) as exc:
            progress.stream_note = _describe(exc)
            progress.stream_failed = True
        else:
            if progress.stream_failed:
                progress.reconnects += 1
                progress.stream_note = (
                    f"Reconnected; resumed after event {progress.cursor} without duplicates."
                )
            progress.stream_failed = False
            progress.reads += 1
            progress.apply(batch.events)
            progress.stream_ended = batch.ended
            if batch.interrupted:
                progress.stream_failed = True
                progress.stream_note = "Connection dropped; resuming from the last event."
    try:
        ss.job = client.get_analysis(job["job_id"])
        if ss.job.get("result_available") and (
            ss.decision is None or ss.decision.get("status") != ss.job["status"]
        ):
            ss.decision = client.get_decision(job["job_id"])
        ss.job_error = None
    except (ApiError, ApiUnavailableError) as exc:
        ss.job_error = exc
    return bool(ss.job["status"] != before)


def _progress_panel(client: SentinelFlowClient) -> None:
    ss = st.session_state
    job = ss.job
    active = job["status"] in ACTIVE_STATUSES or _needs_stream(job, ss.progress)

    @st.fragment(run_every=poll_seconds() if active else None)
    def live() -> None:
        if _needs_stream(ss.job, ss.progress) or ss.job["status"] in ACTIVE_STATUSES:
            with _client(client.base_url, _current_key()) as poller:
                changed = _refresh(poller)
            # Re-render the whole page when the status moved (decision, approval form)
            # or the stream caught up, which also stops this fragment's timer.
            if active and (changed or not _needs_stream(ss.job, ss.progress)):
                st.rerun()
        _render_progress(ss.job, ss.progress)

    live()


def _current_key() -> str | None:
    typed = str(st.session_state.get("api_key_input") or "").strip()
    return typed or os.getenv("SENTINELFLOW_API_KEY", "").strip() or None


def _render_progress(job: dict[str, Any], progress: JobProgress) -> None:
    st.markdown("#### Execution progress")
    status = job["status"]
    color = STATUS_COLORS.get(status, "gray")
    st.markdown(
        f"**Job status** :{color}-badge[{STATUS_LABELS.get(status, status)}]"
        f"&nbsp;&nbsp; **Stage** {progress.stage(status)}"
    )
    if st.session_state.job_error is not None:
        _show_error(st.session_state.job_error, context="Refreshing the job")
    if progress.stream_note:
        st.caption(progress.stream_note)
    if job.get("error"):
        error = job["error"]
        st.error(f"**{error.get('code')}**: {error.get('message')}", icon=":material/error:")

    nodes = sorted(progress.nodes.values(), key=lambda node: node.started_seq or 0)
    if nodes:
        st.dataframe(
            [
                {
                    "run": node.run,
                    "node": node.node,
                    "agent": node.agent or "",
                    "task": node.task_id[:8],
                    "status": node.status,
                    "state keys written": ", ".join(node.updated_keys),
                    "fragments": node.fragments_streamed,
                }
                for node in nodes
            ],
            hide_index=True,
        )
        st.caption(
            "Node events come from the SSE stream. Parallel agents run as separate "
            "tasks of one node and are told apart by agent and task id."
        )
    elif status in ACTIVE_STATUSES:
        st.caption("Waiting for the first node event…")

    with st.expander(f"Lifecycle events ({len(progress.lifecycle)})", expanded=False):
        for event in progress.lifecycle:
            when, detail = _time(event.get("at")), _event_detail(event)
            st.markdown(f"`{event['seq']:>3}` **{event['type']}** {when} {detail}")
        st.caption(
            f"Durable cursor (Last-Event-ID): {progress.cursor} · stream reads: "
            f"{progress.reads} · reconnects: {progress.reconnects}"
        )

    if progress.fragments:
        with st.expander("Live structured-output fragments: provisional, unverified"):
            st.warning(
                "These are pieces of agents' JSON as the model streamed it. They are "
                "not the decision: output can still fail validation, be repaired or "
                "be discarded. The release decision below is authoritative.",
                icon=":material/warning:",
            )
            missing = progress.missing_fragments + progress.server_dropped
            if missing:
                st.caption(
                    f"{missing} fragment(s) were not received by this page (between "
                    "stream reads, or dropped for a slow client). They are not "
                    "reconstructed."
                )
            for stream in progress.fragments.values():
                label = stream.agent or stream.node
                st.markdown(
                    f"**{label}** · task `{stream.task_id[:8]}` · {stream.schema_name} · "
                    f"response {stream.response} · {stream.channel}"
                )
                st.code(stream.text, language="json", wrap_lines=True)


def _event_detail(event: dict[str, Any]) -> str:
    data = event.get("data") or {}
    parts = [f"{key}={data[key]}" for key in ("decision", "code", "approved") if key in data]
    return f"· {', '.join(parts)}" if parts else ""


def _time(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%H:%M:%S")
    except ValueError:
        return ""


# -- decision ---------------------------------------------------------------------------


def _decision_panel(job: dict[str, Any], decision: dict[str, Any]) -> None:
    result = decision["result"]
    gate = result["risk_assessment"]
    report = result["report"]
    verdict = str(gate["decision"])
    status = job["status"]
    st.markdown("#### Release decision")
    note = {
        "waiting_for_approval": "Computed. Waiting for a human decision on recording it.",
        "completed": "Job completed. The job status only says the run finished; "
        "this is the release decision.",
        "failed": "The job failed after this decision was computed; the decision is unchanged.",
    }.get(status, "")
    color = DECISION_COLORS.get(verdict, "#57606a")
    st.markdown(
        f"<div class='sf-decision' style='background:{color}'>"
        f"<div class='label'>Deterministic release gate</div>"
        f"<div class='value'>{verdict}</div><div class='note'>{note}</div></div>",
        unsafe_allow_html=True,
    )
    metrics = st.columns(3)
    metrics[0].metric("Risk score", f"{gate['risk_score']} / 100")
    metrics[1].metric("Confidence", f"{float(gate['confidence']):.0%}")
    coverage = report.get("evidence_coverage")
    metrics[2].metric("Evidence coverage", "n/a" if coverage is None else f"{coverage:.0%}")

    if result.get("degraded"):
        reasons = result.get("degradation") or []
        lines = [
            f"- `{reason.get('code')}`"
            + (f" in `{reason['component']}`" if reason.get("component") else "")
            + (f": {reason['detail']}" if reason.get("detail") else "")
            for reason in reasons
        ]
        st.warning(
            "**Degraded run.** Part of the analysis took the conservative path:\n"
            + "\n".join(lines),
            icon=":material/report:",
        )

    gate_col, report_col = st.columns(2)
    with gate_col, st.container(border=True):
        st.markdown("**Deterministic gate** · computed by code, not by a model")
        override = gate.get("override_applied")
        st.markdown(f"Policy override applied: `{override}`" if override else "No policy override")
        matched = gate.get("overrides_matched") or []
        if matched:
            held = ", ".join(f"`{item}`" for item in matched)
            st.markdown(f"Policy conditions that held: {held}")
        scores = gate.get("component_scores") or {}
        if scores:
            st.dataframe(
                [{"component": name, "score": value} for name, value in scores.items()],
                hide_index=True,
            )
    with report_col, st.container(border=True):
        st.markdown("**Supporting analysis** · LLM agents, citations checked by code")
        services = report.get("impacted_services") or []
        st.markdown("Impacted services: " + (", ".join(f"`{s}`" for s in services) or "none"))
        findings = report.get("critical_findings") or []
        if findings:
            st.markdown("Critical findings:")
            for finding in findings:
                refs = ", ".join(f"`{ref}`" for ref in finding.get("evidence_ids") or [])
                suffix = f" · evidence: {refs}" if refs else ""
                st.markdown(f"- {finding.get('finding')}{suffix}")
        else:
            st.markdown("Critical findings: none")
        tests = report.get("recommended_tests") or []
        st.markdown("Recommended tests: " + (", ".join(f"`{t}`" for t in tests) or "none"))
        plan = result.get("plan")
        if plan:
            st.caption(
                f"Triage plan: {plan.get('change_category')} → "
                + ", ".join(plan.get("agents") or [])
            )

    _audit_status(job, result)
    with st.expander("Trace metrics"):
        st.json(result.get("trace") or {}, expanded=True)


def _audit_status(job: dict[str, Any], result: dict[str, Any]) -> None:
    approval = result.get("approval") or {}
    if not approval.get("required"):
        st.caption("Audit: this analysis did not request a recorded gate decision.")
        return
    recorded = bool(approval.get("gate_decision_recorded"))
    record = job.get("approval") or {}
    who = f" by {record.get('reviewer')}" if record.get("reviewer") else ""
    if job["status"] == "failed":
        st.error(
            "Audit: the gate decision was **not** recorded. The decision above is "
            "unchanged; this job cannot be approved again.",
            icon=":material/gpp_bad:",
        )
    elif recorded:
        st.success(f"Audit: gate decision recorded (approved{who}).", icon=":material/verified:")
    elif approval.get("status") == "rejected":
        st.info(f"Audit: rejected{who}; nothing was written to the audit log.")
    elif job["status"] == "waiting_for_approval":
        st.info("Audit: pending a human decision; nothing written yet.")
    else:
        st.caption(f"Audit: approval status {approval.get('status')}, not recorded.")


# -- approval ---------------------------------------------------------------------------


def _approval_panel(client: SentinelFlowClient) -> None:
    ss = st.session_state
    job = ss.job
    st.markdown("#### Human approval")
    if ss.approval_sent == job["job_id"]:
        st.info("Your decision was submitted; the job is resuming.")
        return
    st.markdown(
        "Approving records the decision above in the audit log. Rejecting records "
        "nothing. Neither changes the decision."
    )
    with st.form("approval", border=True):
        reviewer = st.text_input("Reviewer", max_chars=128, key="approval_reviewer")
        comment = st.text_area("Comment (optional)", max_chars=1000, key="approval_comment")
        reviewed = st.checkbox("I have reviewed the decision above", key="approval_reviewed")
        left, right = st.columns(2)
        approve = left.form_submit_button("Approve and record", type="primary")
        reject = right.form_submit_button("Reject")
    if not (approve or reject):
        return
    if not reviewed or not reviewer.strip():
        st.warning("Enter a reviewer and confirm you reviewed the decision.")
        return
    try:
        ss.job = client.submit_approval(
            job["job_id"],
            approved=bool(approve),
            reviewer=reviewer.strip(),
            comment=comment.strip() or None,
        )
    except ApiError as exc:
        if exc.code == "APPROVAL_ALREADY_SUBMITTED":
            # Another tab or reviewer got there first; this click changed nothing.
            ss.approval_sent = job["job_id"]
            ss.approval_message = (
                "A decision was already submitted for this job; your click was not applied."
            )
            _reload_job(client)
            st.rerun()
        _show_error(exc, context="Approval")
        return
    except ApiUnavailableError as exc:
        _show_error(exc, context="Approval")
        return
    ss.approval_sent = job["job_id"]
    who = reviewer.strip()
    ss.approval_message = (
        f"Approval submitted by {who}. The audit status below shows whether it was recorded."
        if approve
        else f"Rejection submitted by {who}. Nothing will be written to the audit log."
    )
    st.rerun()


def _reload_job(client: SentinelFlowClient) -> None:
    ss = st.session_state
    try:
        ss.job = client.get_analysis(ss.job_id)
    except (ApiError, ApiUnavailableError) as exc:
        ss.job_error = exc


def _analysis_tab(client: SentinelFlowClient) -> None:
    ss = st.session_state
    left, right = st.columns([3, 2], gap="large")
    with left:
        _launch_form(client)
    with right:
        _open_form(client)
    if ss.launch_message:
        st.info(ss.launch_message)
    if ss.job is None:
        return
    st.divider()
    request = ss.job["request"]
    st.markdown(f"#### Job `{ss.job['job_id']}`")
    st.caption(
        f"{request.get('domain_pack_id')} · {request.get('change_id')} · source "
        f"{request.get('source', 'fixture')} · run {ss.job.get('run_count', 0)}"
    )
    _new_submission_button()
    _progress_panel(client)
    if ss.approval_message:
        st.success(ss.approval_message)
    if ss.decision is not None:
        st.divider()
        _decision_panel(ss.job, ss.decision)
    if ss.job["status"] == "waiting_for_approval" and ss.decision is not None:
        st.divider()
        _approval_panel(client)


# -- evaluation -------------------------------------------------------------------------


def _evaluation_tab(client: SentinelFlowClient) -> None:
    ss = st.session_state
    st.markdown("#### Deterministic fixture evaluation")
    st.markdown(
        "Scores every bundled change fixture with the deterministic risk engine and "
        "policy gate, and compares each decision with the fixture's label. No model, "
        "network or container call is made."
    )
    with st.form("evaluation", border=True):
        packs = st.multiselect(
            "Domain packs (empty = all)",
            options=list(FIXTURE_CHANGES),
            accept_new_options=True,
            key="eval_packs",
        )
        run = st.form_submit_button("Run evaluation", type="primary")
    if run:
        body = {"domain_pack_ids": sorted(str(pack) for pack in packs) or None}
        key = ss.evaluation_keys.key_for(body)
        try:
            created = client.create_evaluation(
                domain_pack_ids=body["domain_pack_ids"],
                idempotency_key=key,
            )
        except (ApiError, ApiUnavailableError) as exc:
            _show_error(exc, context="Evaluation")
        else:
            if ss.eval_job_id != created.job["job_id"]:
                ss.eval_result = None
            ss.eval_job_id = created.job["job_id"]
            ss.eval_job = created.job
            if created.replayed:
                st.caption("Same selection as before: showing the evaluation already run.")
    if ss.eval_job is None:
        return
    active = ss.eval_job["status"] in ACTIVE_STATUSES

    @st.fragment(run_every=poll_seconds() if active else None)
    def poll() -> None:
        if ss.eval_job["status"] not in TERMINAL_STATUSES or (
            ss.eval_job["status"] == "completed" and ss.eval_result is None
        ):
            with _client(client.base_url, _current_key()) as poller:
                try:
                    ss.eval_job = poller.get_evaluation(ss.eval_job_id)
                    if ss.eval_job["status"] == "completed" and ss.eval_result is None:
                        ss.eval_result = poller.get_evaluation_result(ss.eval_job_id)
                    ss.eval_error = None
                except (ApiError, ApiUnavailableError) as exc:
                    ss.eval_error = exc
            if active and ss.eval_job["status"] in TERMINAL_STATUSES:
                st.rerun()
        _render_evaluation()

    poll()


def _render_evaluation() -> None:
    ss = st.session_state
    job = ss.eval_job
    st.caption(f"Job `{job['job_id']}` · {STATUS_LABELS.get(job['status'], job['status'])}")
    if ss.eval_error is not None:
        _show_error(ss.eval_error, context="Evaluation")
    if job.get("error"):
        st.error(f"**{job['error'].get('code')}**: {job['error'].get('message')}")
    if ss.eval_result is None:
        return
    result = ss.eval_result["result"]
    summary = result["summary"]
    st.info(
        "**Fixture consistency check, not model-quality accuracy.** The fixture "
        "labels are the decisions the deterministic integration test already pins, "
        "so a full match shows the risk engine and gate agree with those labels. It "
        "does not measure the agents or any model.",
        icon=":material/info:",
    )
    cols = st.columns(4)
    cols[0].metric("Fixtures", summary["fixtures"])
    cols[1].metric("Labelled", summary["labelled"])
    cols[2].metric("Matched label", f"{summary['matched']} / {summary['labelled']}")
    ratio = summary.get("decision_accuracy")
    cols[3].metric("Consistency", "n/a" if ratio is None else f"{ratio:.0%}")
    st.dataframe(result["rows"], hide_index=True)


# -- entry point ------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(
        page_title="SentinelFlow harness", page_icon=":material/shield:", layout="wide"
    )
    st.markdown(_CSS, unsafe_allow_html=True)
    _state()
    base_url, api_key = _connection()
    st.title("SentinelFlow")
    st.caption("Release-risk analysis harness · every action goes through the HTTP API")
    with _client(base_url, api_key) as client:
        analysis, evaluation = st.tabs(["Release analysis", "Evaluation"])
        with analysis:
            _analysis_tab(client)
        with evaluation:
            _evaluation_tab(client)
