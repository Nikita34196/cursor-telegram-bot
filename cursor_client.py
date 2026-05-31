"""Minimal client for Cursor Cloud Agents API v1."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator

API_BASE = "https://api.cursor.com/v1"


class CursorAPIError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class CursorClient:
    def __init__(self, api_key: str) -> None:
        self._auth = base64.b64encode(f"{api_key}:".encode()).decode()

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        timeout: int = 120,
    ) -> dict:
        url = f"{API_BASE}{path}"
        data = None
        headers = {
            "Authorization": f"Basic {self._auth}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise CursorAPIError(e.code, detail or str(e)) from e

    def me(self) -> dict:
        return self._request("GET", "/me")

    def create_agent(
        self,
        prompt: str,
        repo_url: str | None = None,
        starting_ref: str = "main",
        auto_create_pr: bool = True,
        mode: str = "agent",
        name: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "prompt": {"text": prompt},
            "mode": mode,
            "autoCreatePR": auto_create_pr,
        }
        if name:
            payload["name"] = name[:100]
        if repo_url:
            payload["repos"] = [{"url": repo_url, "startingRef": starting_ref}]
        return self._request("POST", "/agents", payload)

    def create_run(self, agent_id: str, prompt: str, mode: str | None = None) -> dict:
        body: dict[str, Any] = {"prompt": {"text": prompt}}
        if mode:
            body["mode"] = mode
        return self._request("POST", f"/agents/{agent_id}/runs", body)

    def get_agent(self, agent_id: str) -> dict:
        return self._request("GET", f"/agents/{agent_id}")

    def get_run(self, agent_id: str, run_id: str) -> dict:
        return self._request("GET", f"/agents/{agent_id}/runs/{run_id}")

    def cancel_run(self, agent_id: str, run_id: str) -> dict:
        return self._request(
            "POST", f"/agents/{agent_id}/runs/{run_id}/cancel", body={}
        )

    def list_agents(self, limit: int = 10) -> dict:
        return self._request("GET", f"/agents?limit={limit}")

    def list_runs(self, agent_id: str, limit: int = 5) -> dict:
        return self._request("GET", f"/agents/{agent_id}/runs?limit={limit}")

    def stream_run(
        self,
        agent_id: str,
        run_id: str,
        on_event: Callable[[str, dict], None],
        last_event_id: str | None = None,
    ) -> None:
        url = f"{API_BASE}/agents/{agent_id}/runs/{run_id}/stream"
        headers = {
            "Authorization": f"Basic {self._auth}",
            "Accept": "text/event-stream",
        }
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id

        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                event_type: str | None = None
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        continue
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        try:
                            data = json.loads(line[5:].strip() or "{}")
                        except json.JSONDecodeError:
                            continue
                        if event_type:
                            on_event(event_type, data)
        except urllib.error.HTTPError as e:
            if e.code == 410:
                raise CursorAPIError(410, "stream_expired") from e
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise CursorAPIError(e.code, detail or str(e)) from e


def agent_web_url(agent_id: str, web_url: str | None = None) -> str:
    return web_url or f"https://cursor.com/agents/{agent_id}"


def format_git_links(git: dict | None) -> str:
    if not git:
        return ""
    lines: list[str] = []
    for branch in git.get("branches") or []:
        repo = branch.get("repoUrl", "")
        name = branch.get("branch", "")
        pr = branch.get("prUrl", "")
        if pr:
            lines.append(f"🔀 PR: {pr}")
        elif name:
            lines.append(f"🌿 Ветка: {repo} → `{name}`")
    return "\n".join(lines)


def terminal_statuses() -> set[str]:
    return {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}
