import json
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from github import REPOSITORY_PATTERN, fetch_repository
from source_tools import read_source, resolve_commit, validate_location
from collaboration_tools import fetch_issue, fetch_pull_request, read_pr_diff

TOOLS = [{
    "type": "function",
    "function": {
        "name": "fetch_repository",
        "description": "读取 GitHub 仓库的基本信息、根目录和 README，不读取完整源码。",
        "parameters": {
            "type": "object",
            "properties": {"repository": {
                "type": "string", "pattern": f"^{REPOSITORY_PATTERN}$",
                "description": "GitHub 仓库名，格式为 owner/repo，例如 fastapi/fastapi。",
            }},
            "required": ["repository"],
            "additionalProperties": False,
        },
    },
}]


class RepositoryArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repository: str = Field(pattern=f"^{REPOSITORY_PATTERN}$", max_length=140)


class SourceArguments(RepositoryArguments):
    path: str = Field(default="", max_length=1024)
    ref: str | None = Field(default=None, min_length=1, max_length=200)


class FileArguments(SourceArguments):
    path: str = Field(min_length=1, max_length=1024)
    start_line: int = Field(default=1, ge=1, le=1000000)
    line_count: int = Field(default=120, ge=1, le=200)


class NumberArguments(RepositoryArguments):
    number: int = Field(ge=1, le=2147483647)


class DiffArguments(NumberArguments):
    page: int = Field(default=1, ge=1, le=150)


for name, model, description in [
    ("list_directory", SourceArguments, "读取 GitHub 指定目录。path 为相对路径，根目录用空字符串。同轮同仓库固定首次 ref 对应 commit，后续省略 ref。"),
    ("read_file", FileArguments, "读取 GitHub UTF-8 源码，返回真实行号和 commit 固定链接。用 start_line、line_count 分页。不支持二进制及大文件。"),
    ("fetch_issue", NumberArguments, "按编号读取 GitHub Issue 标题、正文、状态与来源。不含评论；若编号是 PR 会明确标记。"),
    ("fetch_pull_request", NumberArguments, "按编号读取 PR 说明、状态、base/head commit、变更统计，不含 diff、评论或 Review。"),
    ("read_pr_diff", DiffArguments, "按 PR 编号读取变更文件与 patch，每页20文件。按 next_page 继续读取，明确说明缺失及截断。"),
]:
    TOOLS.append({"type": "function", "function": {
        "name": name, "description": description, "parameters": model.model_json_schema(),
    }})


class FunctionCall(BaseModel):
    name: str = Field(max_length=128)
    arguments: str = Field(max_length=16000)


class ToolCall(BaseModel):
    id: str = Field(min_length=1)
    type: Literal["function"]
    function: FunctionCall


TOOL_MODELS = {"fetch_repository": RepositoryArguments, "list_directory": SourceArguments, "read_file": FileArguments,
               "fetch_issue": NumberArguments, "fetch_pull_request": NumberArguments, "read_pr_diff": DiffArguments}


async def execute_tool(call: ToolCall, pins: dict | None = None) -> str:
    model = TOOL_MODELS.get(call.function.name)
    if model is None:
        return json.dumps({"ok": False, "error": "未知工具，请使用已声明的只读工具。"}, ensure_ascii=False)
    try:
        args = model.model_validate_json(call.function.arguments)
    except ValidationError:
        return json.dumps({"ok": False, "error": "参数无效，请按工具定义提供仓库、路径、版本、编号或分页范围。"}, ensure_ascii=False)
    try:
        if call.function.name == "fetch_repository":
            snapshot = await fetch_repository(args.repository)
            payload = {"repository": snapshot.model_dump()}
        elif isinstance(args, NumberArguments):
            pins = pins if pins is not None else {}
            key = ('pr', args.repository.lower(), args.number)
            if call.function.name == 'fetch_issue':
                payload = {'issue': await fetch_issue(args.repository, args.number)}
            else:
                if call.function.name == 'read_pr_diff':
                    result = await read_pr_diff(args.repository, args.number, args.page, pins.get(key))
                else:
                    result = await fetch_pull_request(args.repository, args.number)
                version = (result['base_sha'], result['head_sha'])
                if key in pins and pins[key] != version:
                    raise HTTPException(409, '本轮 PR 版本已变化，请在下一轮重新读取。')
                pins[key] = version
                payload = {'pull_request' if call.function.name == 'fetch_pull_request' else 'diff': result}
        else:
            validate_location(args.repository, args.path)
            pins = pins if pins is not None else {}
            key = args.repository.lower()
            if key not in pins:
                commit = await resolve_commit(args.repository, args.ref)
                pins[key] = (args.ref, commit)
            original_ref, commit = pins[key]
            if args.ref is not None and args.ref not in {original_ref, commit}:
                raise HTTPException(422, "本轮源码已固定版本，请省略 ref 继续读取；切换版本请开启下一轮。")
            options = {"start_line": args.start_line, "line_count": args.line_count} if isinstance(args, FileArguments) else {}
            payload = {"source": await read_source(args.repository, args.path, commit,
                       directory=call.function.name == "list_directory", **options)}
        return json.dumps({"ok": True, **payload}, ensure_ascii=False)
    except HTTPException as exc:
        return json.dumps({"ok": False, "status": exc.status_code, "error": exc.detail}, ensure_ascii=False)

