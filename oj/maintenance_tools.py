"""Owner-authenticated MCP bridge to the isolated Codex maintenance service."""
import json
from pathlib import Path
from typing import Literal
import urllib.request
from mcp.types import ToolAnnotations


def call(operation, **fields):
    key = Path('/etc/cann-oj-rsi/token').read_text().strip()
    request = urllib.request.Request('http://127.0.0.1:8776/rpc',
        data=json.dumps(dict(op=operation, **fields)).encode(),
        headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def register_maintenance_tools(server, meta):
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

    @server.tool(annotations=read, meta=meta)
    def get_oj_maintenance_status() -> dict:
        """Read remote Codex login, proxy and automatic OJ repair policy. Does not submit to any judge."""
        return call('status')

    @server.tool(annotations=write, meta=meta)
    def suggest_oj_improvement(instructions: str, request_id: str,
                               auto_apply: bool = True,
                               mode: Literal['inspect','repair'] = 'repair') -> dict:
        """OWNER MAINTENANCE, not an operator submission. Send a concrete OJ repair/improvement to server Codex.
        Requires the user's request to maintain OJ. request_id is an idempotency key (8..100 letters/digits/._-).
        Scope includes the entire OJ architecture: judge, scheduling/concurrency, database migrations,
        frontend/backend, MCP, dependencies and deployment. Refactoring and source removal are allowed.
        Codex prefers an isolated snapshot. repair runs original regression tests; auto_apply deploys source
        changes when jobs are idle, with source conflict checks, backups and health rollback.
        Each issue keeps its Codex session; failures feed back to that session for up to 3 repair attempts.
        Development and deployment have independent queues. Use continue_oj_improvement for the same issue.
        Server Codex runs as root with full permissions, GPT-6 Astra xhigh, no per-command approval.
        inspect only produces advice. Preserve judge correctness and historical records. Never consumes official quota.
        Query get_oj_improvement for progress, patch and tests; do not assume queued means deployed.
        Do not include passwords or API keys in the instructions. If logged out, tasks wait for owner login.
        """
        return call('submit', instructions=instructions, request_id=request_id, auto_apply=auto_apply, mode=mode)

    @server.tool(annotations=read, meta=meta)
    def list_oj_improvements() -> dict:
        """Read the latest 30 persistent OJ maintenance tasks across conversations."""
        return {'jobs':call('list')}

    @server.tool(annotations=read, meta=meta)
    def get_oj_improvement(task_id: str) -> dict:
        """Read a maintenance task's status, answer, changed files, tests, bounded log and patch. Production changes only exist at deployed."""
        return call('get', id=task_id)

    @server.tool(annotations=write, meta=meta)
    def cancel_oj_improvement(task_id: str) -> dict:
        """Cancel an OJ maintenance task before activation. Does not cancel evaluations or official submissions."""
        return call('cancel', id=task_id)

    @server.tool(annotations=write, meta=meta)
    def continue_oj_improvement(task_id: str, instructions: str, request_id: str) -> dict:
        """Continue an existing maintenance issue in its SAME persisted Codex session/workspace.
        Idempotent request_id. While running, feedback is queued for the next turn, never interrupts a judge.
        Reuses failed patches; already deployed tasks get a fresh source snapshot with the original conversation.
        Prefer this over creating duplicate fixes. No official competition submission."""
        return call('followup',id=task_id,instructions=instructions,request_id=request_id)

    @server.tool(annotations=write, meta=meta)
    def manage_oj_improvement(task_id: str, action: Literal['pause','resume','archive']) -> dict:
        """Manage a maintenance session. Pause takes effect at a safe phase boundary; resume continues it.
        Archive only marks a terminal record; source, session and audit stay available. Does not affect evaluations."""
        return call(action,id=task_id)

    @server.tool(annotations=write, meta=meta)
    def set_oj_autonomy(enabled: bool) -> dict:
        """Enable/disable owner-authorized autonomous RSI cycles. Lightweight Python observes service/queue state;
        GPT-5.6 Luna inspects OJ/controller READ-ONLY every 30 minutes when maintenance lanes are free.
        The controller hands concrete findings to GPT-6 Astra xhigh for all code changes, tests and deployment;
        Luna cannot edit code/configuration or publish. Back off after repeated no-issue cycles. Persistent sessions, bounded retries,
        original tests, idle deployment, rollback and post-deployment health checks. Existing tasks continue.
        This does not allow official competition submissions or changes to historical scores."""
        return call('autonomy',enabled=enabled)
