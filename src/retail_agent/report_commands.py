import re
from collections.abc import Callable
from datetime import UTC, datetime

from rich.console import Console

from retail_agent.auth import Principal
from retail_agent.errors import AgentError, ErrorCode
from retail_agent.models import Report
from retail_agent.observability import Telemetry
from retail_agent.presentation import render_report
from retail_agent.reports import ReportStore


class ReportCommands:
    def __init__(
        self,
        store: ReportStore,
        authenticate: Callable[[], Principal],
        conversation_id: str,
        console: Console,
        telemetry: Telemetry,
    ) -> None:
        self.store, self.authenticate = store, authenticate
        self.conversation_id, self.console, self.telemetry = conversation_id, console, telemetry
        self.last_report: Report | None = None
        self.last_policy = ""
        self.shown_report_id: str | None = None
        self.deleted = False

    def handle(self, text: str) -> bool:
        text = text.strip()
        lower = text.lower()
        self.deleted = False
        if lower == "/details":
            principal = self.authenticate()
            if self.last_report is None or self.last_policy != principal.fingerprint:
                raise AgentError(ErrorCode.ACCESS)
            details_report = self.last_report
            if self.shown_report_id is not None:
                saved = self.store.get(principal, self.shown_report_id)
                details_report = Report.model_validate_json(saved.body)
            render_report(self.console, details_report)
            return True
        if lower in {"/save", "save this report", "save this report."}:
            principal = self.authenticate()
            if self.last_report is None or self.last_policy != principal.fingerprint:
                raise AgentError(ErrorCode.ACCESS)
            saved = self.store.save(principal, self.conversation_id, self.last_report)
            self.telemetry.emit("report_saved", count=1)
            self.console.print(f"Saved report: {saved.id}")
            return True
        if lower == "/reports" or lower.startswith("/reports "):
            principal = self.authenticate()
            reports = self.store.list(principal, mention=text[len("/reports") :].strip())
            for report in reports:
                self.console.print(f"{report.id} | {report.title}")
            if not reports:
                self.console.print("No matching reports.")
            return True
        if lower == "/show" or lower.startswith("/show "):
            if not re.fullmatch(r"[a-f0-9]{32}", text[len("/show") :].strip()):
                self.console.print("Use /show REPORT_ID. Copy an actual ID from /reports.")
                return True
            principal = self.authenticate()
            try:
                report = self.store.get(principal, text[len("/show ") :].strip())
            except AgentError as error:
                if error.code != ErrorCode.ACCESS:
                    raise
                self.telemetry.emit("error", error_category=error.code)
                self.console.print(
                    "Report not found or unavailable with your current access. "
                    f"Trace: {self.telemetry.trace_id}"
                )
                return True
            displayed = Report.model_validate_json(report.body)
            self.last_report = displayed
            self.last_policy = principal.fingerprint
            self.shown_report_id = report.id
            render_report(self.console, displayed, show_details=False)
            return True
        mention = re.fullmatch(r"delete all (?:the )?reports mentioning (.+?)\.?", text, re.I)
        conversation = lower in {
            "/delete conversation",
            "delete all reports in this conversation",
            "delete all the reports we made in this conversation",
        }
        if conversation or mention or lower.startswith("/delete mention "):
            term = (
                mention.group(1).strip('"') if mention else text[len("/delete mention ") :].strip()
            )
            preview = self.store.preview_delete(
                self.authenticate(),
                mention="" if conversation else term,
                conversation_id=self.conversation_id if conversation else None,
            )
            if not preview.reports:
                self.console.print("No matching reports. Nothing deleted.")
                return True
            self.console.print("Permanent deletion. No recovery. Exact reports:")
            for report in preview.reports:
                created = datetime.fromtimestamp(report.created_at, UTC).isoformat()
                self.console.print(f"{report.id} | {report.title} | {created}")
            self.console.print(
                f"Count: {len(preview.reports)}. Expires in {self.store.ttl} seconds."
            )
            self.console.print(f"To confirm type exactly: DELETE {preview.challenge}")
            self.telemetry.emit("delete_preview", count=len(preview.reports))
            try:
                confirmation = self.console.input("Confirmation (anything else cancels)> ")
            except (KeyboardInterrupt, EOFError):
                confirmation = ""
            if confirmation != "DELETE " + preview.challenge:
                self.store.cancel_delete(preview.challenge, self.authenticate())
                self.console.print("Cancelled. Nothing deleted.")
                return True
            count = self.store.confirm_delete(confirmation, self.authenticate)
            self.last_report = None
            self.last_policy = ""
            self.deleted = True
            self.telemetry.emit("report_deleted", count=count)
            self.console.print(f"Permanently deleted {count} report(s).")
            return True
        if text.startswith("/"):
            self.console.print(
                "Unknown command. Use /details, /save, /reports [text], /show REPORT_ID, "
                "/delete conversation, /delete mention TEXT, or /exit."
            )
            return True
        return False
