from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from inspect import isawaitable
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Protocol, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.comparison import compare_documents
from app.extraction import analyze_document


@dataclass
class WorkflowContext:
    """Run-scoped inputs following the AI-base WorkflowContext contract."""

    input_val: Any
    provider: Any
    agent_id: str = "prospectus-checker"
    role_id: str = "investment-reviewer"
    role_prompt: str = ""
    skill_md: str = ""
    knowledge_search: Callable[..., Any] | None = None
    knowledge_context_loader: Callable[..., Any] | None = None
    resources: Any = None
    signal: Any = None
    message_chunk_chars: int = 256
    run_id: str | None = None
    session_id: str | None = None
    document_ids: list[str] = field(default_factory=list)
    document_loader: Callable[[str], Any] | None = None
    document_bytes_loader: Callable[[str], Any] | None = None
    trace_callback: Callable[[dict[str, Any]], None] | None = None


def make_document_bytes_loader(document_service: Any) -> Callable[[str], dict[str, str]]:
    """Adapt the AI-base document service's ready record to original PDF bytes."""
    def load(document_id: str) -> dict[str, str]:
        record = document_service.require_ready(document_id)
        if hasattr(record, "model_dump"):
            record = record.model_dump()
        if not isinstance(record, dict):
            record = vars(record)
        file_path = str(record.get("file_path") or "")
        if not file_path:
            raise RuntimeError(f"PDF_BINARY_UNAVAILABLE: {document_id}")
        return {
            "filename": str(record.get("filename") or f"{document_id}.pdf"),
            "file_path": file_path,
        }

    return load


class WorkflowProtocol(Protocol):
    id: str

    def stream(self, context: WorkflowContext) -> AsyncGenerator[Any, None]: ...


class WorkflowRegistry:
    """Small workflow-id registry with the AI-base registry API."""

    def __init__(self, workflows: list[WorkflowProtocol] | None = None) -> None:
        self._workflows: dict[str, WorkflowProtocol] = {}
        for workflow in workflows or []:
            self.register(workflow)

    def register(self, workflow: WorkflowProtocol) -> None:
        if workflow.id in self._workflows:
            raise ValueError(f"WORKFLOW_ALREADY_REGISTERED: {workflow.id}")
        self._workflows[workflow.id] = workflow

    def require(self, workflow_id: str) -> WorkflowProtocol:
        try:
            return self._workflows[workflow_id]
        except KeyError as exc:
            raise KeyError(f"UNKNOWN_WORKFLOW: {workflow_id}") from exc

    def list_ids(self) -> list[str]:
        return list(self._workflows)


class WorkflowEngine:
    """Dispatch only; graph execution remains in the registered workflow."""

    def __init__(self, registry: WorkflowRegistry) -> None:
        self.registry = registry

    def stream(self, workflow_id: str, context: WorkflowContext) -> AsyncGenerator[Any, None]:
        return self.registry.require(workflow_id).stream(context)


class ComparisonState(TypedDict, total=False):
    context: Any
    payload: dict[str, Any]
    prospectus: dict[str, Any]
    periodic_report: dict[str, Any]
    result: dict[str, Any]


def _direct_payload(context: Any) -> dict[str, Any] | None:
    payload = getattr(context, "input_val", None)
    required = {
        "prospectus_filename", "prospectus_content",
        "periodic_filename", "periodic_content",
    }
    return payload if isinstance(payload, dict) and required.issubset(payload) else None


async def _load_base_documents(context: Any) -> dict[str, Any]:
    input_val = getattr(context, "input_val", None)
    document_ids = list(
        getattr(context, "document_ids", None)
        or getattr(input_val, "documentIds", None)
        or (input_val.get("documentIds", []) if isinstance(input_val, dict) else [])
    )
    loader = getattr(context, "document_bytes_loader", None)
    if len(document_ids) != 2 or not callable(loader):
        raise RuntimeError(
            "PDF_BINARY_LOADER_REQUIRED: pass two ordered document IDs "
            "(prospectus, periodic report) and a document_bytes_loader"
        )

    async def load(document_id: str) -> tuple[str, bytes]:
        value = loader(document_id)
        if isawaitable(value):
            value = await value
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        if isinstance(value, (str, Path)):
            value = {"file_path": str(value)}
        elif not isinstance(value, dict):
            value = vars(value)
        filename = str(value.get("filename") or f"{document_id}.pdf")
        content = value.get("content") or value.get("pdf_bytes")
        if content is None and value.get("file_path"):
            content = await asyncio.to_thread(Path(value["file_path"]).read_bytes)
        if isinstance(content, bytearray):
            content = bytes(content)
        if not isinstance(content, bytes):
            raise RuntimeError(f"PDF_BINARY_UNAVAILABLE: {document_id}")
        return filename, content

    (prospectus_filename, prospectus_content), (periodic_filename, periodic_content) = await asyncio.gather(
        load(document_ids[0]), load(document_ids[1])
    )
    return {
        "prospectus_filename": prospectus_filename,
        "prospectus_content": prospectus_content,
        "prospectus_preview_id": None,
        "periodic_filename": periodic_filename,
        "periodic_content": periodic_content,
        "periodic_preview_id": None,
    }


class InvestmentComparisonWorkflow:
    """AI-base WorkflowProtocol implementation backed by an actual LangGraph."""

    id = "prospectus-periodic-comparison"

    def __init__(self) -> None:
        graph = StateGraph(ComparisonState)

        async def load_inputs(state: ComparisonState) -> dict[str, Any]:
            context = state["context"]
            payload = _direct_payload(context)
            if payload is None:
                payload = await _load_base_documents(context)
            return {"payload": payload}

        async def analyze_prospectus(state: ComparisonState) -> dict[str, Any]:
            context, payload = state["context"], state["payload"]
            get_stream_writer()({
                "event": "workflow.progress",
                "data": {"runId": getattr(context, "run_id", None), "stage": "prospectus", "step_status": "running", "message": "正在识别招募说明书"},
            })
            result = await analyze_document(
                filename=str(payload["prospectus_filename"]),
                content=payload["prospectus_content"],
                role="prospectus",
                provider=context.provider,
                trace_callback=getattr(context, "trace_callback", None),
            )
            get_stream_writer()({
                "event": "workflow.progress",
                "data": {"runId": getattr(context, "run_id", None), "stage": "prospectus", "step_status": "completed", "message": "招募说明书识别完成"},
            })
            return {"prospectus": result}

        async def analyze_periodic_report(state: ComparisonState) -> dict[str, Any]:
            context, payload = state["context"], state["payload"]
            get_stream_writer()({
                "event": "workflow.progress",
                "data": {"runId": getattr(context, "run_id", None), "stage": "periodic_report", "step_status": "running", "message": "正在识别定期报告"},
            })
            result = await analyze_document(
                filename=str(payload["periodic_filename"]),
                content=payload["periodic_content"],
                role="periodic_report",
                provider=context.provider,
                trace_callback=getattr(context, "trace_callback", None),
            )
            get_stream_writer()({
                "event": "workflow.progress",
                "data": {"runId": getattr(context, "run_id", None), "stage": "periodic_report", "step_status": "completed", "message": "定期报告识别完成"},
            })
            return {"periodic_report": result}

        async def compare(state: ComparisonState) -> dict[str, Any]:
            context = state["context"]
            get_stream_writer()({
                "event": "workflow.progress",
                "data": {"runId": getattr(context, "run_id", None), "stage": "comparison", "step_status": "running", "message": "正在比对核对结果"},
            })
            result = compare_documents(state["prospectus"], state["periodic_report"])
            trace_callback = getattr(context, "trace_callback", None)
            if trace_callback:
                trace_callback({
                    "kind": "artifact",
                    "path": "comparison/comparison_result.json",
                    "content": result,
                })
            payload = state["payload"]
            result["documents"]["prospectus"]["preview_id"] = payload.get("prospectus_preview_id")
            result["documents"]["periodic_report"]["preview_id"] = payload.get("periodic_preview_id")
            get_stream_writer()({
                "event": "workflow.completed",
                "data": {"runId": getattr(context, "run_id", None), "result": result},
            })
            return {"result": result}

        graph.add_node("load_inputs", load_inputs)
        graph.add_node("analyze_prospectus", analyze_prospectus)
        graph.add_node("analyze_periodic_report", analyze_periodic_report)
        graph.add_node("compare", compare)
        graph.add_edge(START, "load_inputs")
        graph.add_edge("load_inputs", "analyze_prospectus")
        graph.add_edge("load_inputs", "analyze_periodic_report")
        graph.add_edge("analyze_prospectus", "compare")
        graph.add_edge("analyze_periodic_report", "compare")
        graph.add_edge("compare", END)
        self._graph = graph.compile()

    async def stream(self, context: WorkflowContext) -> AsyncGenerator[dict[str, Any], None]:
        async for mode, payload in self._graph.astream({"context": context}, stream_mode=["custom"]):
            if mode == "custom":
                yield payload
