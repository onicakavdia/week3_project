import fnmatch
import json
import os
import sys
from datetime import datetime, timezone

import requests
import trafilatura
from dotenv import load_dotenv
from markdownify import markdownify
from nanoid import generate as nanoid_generate
from openai import OpenAI

load_dotenv()

MODEL = "openrouter/free"


WORKSPACE_ROOT = os.path.abspath(os.environ.get("WORKSPACE_ROOT"))



SESSIONS_DIR = os.path.join(WORKSPACE_ROOT, ".agent", "sessions")
AGENTS_PATHS = (
    os.path.join(WORKSPACE_ROOT, "AGENTS.md"),
    os.path.join(WORKSPACE_ROOT, ".agent", "AGENTS.md"),
)
BASE_PROMPT = "You are Research Desk, a helpful research assistant."


def create_session() -> str:
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    return nanoid_generate(size=8)


def save_session(session_id: str, messages: list, title: str = "Untitled") -> None:
    path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    data = {
        "id": session_id,
        "title": title,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "messages": messages,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_session(session_id: str) -> dict:
    path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No session found with id '{session_id}'")
    with open(path) as f:
        return json.load(f)


def list_sessions() -> list[dict]:
    if not os.path.exists(SESSIONS_DIR):
        return []

    sessions = []
    for filename in os.listdir(SESSIONS_DIR):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(SESSIONS_DIR, filename)
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        sessions.append({
            "id": data.get("id", filename[:-5]),
            "title": data.get("title", "Untitled"),
            "updated_at": data.get("updated_at", ""),
        })

    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return sessions


def build_system_prompt() -> str:
    prompt = BASE_PROMPT
    for path in AGENTS_PATHS:
        if os.path.exists(path):
            rules = open(path).read().strip()
            if rules:
                prompt += f"\n\n---\nProject rules from AGENTS.md:\n\n{rules}"
            break
    return prompt



def resolve_path(path: str) -> str:
    full = os.path.abspath(os.path.join(WORKSPACE_ROOT, path))
    if os.path.commonpath([WORKSPACE_ROOT, full]) != WORKSPACE_ROOT:
        raise ValueError(f"Path '{path}' escapes the sandboxed project root.")
    return full


def read_file(path: str, start_line: int = 1, read_lines: int = 200) -> dict:
    try:
        full = resolve_path(path)
    except ValueError as e:
        return {"error": str(e)}

    if not os.path.exists(full):
        return {"error": f"File not found: {path}"}
    if not os.path.isfile(full):
        return {"error": f"Not a file: {path}"}
    if start_line < 1:
        return {"error": "start_line must be >= 1"}

    with open(full, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()

    total_lines = len(lines)
    if total_lines > 0 and start_line > total_lines:
        return {"error": f"start_line {start_line} exceeds file length ({total_lines} lines)"}

    end_index = min(start_line - 1 + read_lines, total_lines)
    chunk = lines[start_line - 1:end_index]
    numbered = "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(chunk, start=start_line))

    return {
        "content": numbered,
        "start_line": start_line,
        "end_line": end_index,
        "total_lines": total_lines,
        "has_more": end_index < total_lines,
    }


def write_file(path: str, content: str) -> dict:
    try:
        full = resolve_path(path)
    except ValueError as e:
        return {"error": str(e)}

    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)

    return {"content": f"Wrote {len(content)} characters to {path}"}


def edit_file(path: str, operation: str, start_line: int = None,
              end_line: int = None, content: str = "") -> dict:
    try:
        full = resolve_path(path)
    except ValueError as e:
        return {"error": str(e)}

    if not os.path.exists(full):
        return {"error": f"File not found: {path}"}
    if operation not in ("replace", "delete", "append"):
        return {"error": f"Unknown operation '{operation}'. Use replace, delete, or append."}

    with open(full, "r", encoding="utf-8", errors="replace") as f:
        original = f.read()
    lines = original.splitlines()
    had_trailing_newline = original.endswith("\n")

    if operation == "append":
        added = content.splitlines()
        new_lines = lines + added
        diff = "\n".join(f"+ {l}" for l in added)
    else:
        if start_line is None:
            return {"error": f"start_line is required for '{operation}'"}
        end = end_line or start_line
        if start_line < 1 or end > len(lines) or start_line > end:
            return {"error": f"Invalid line range {start_line}-{end} (file has {len(lines)} lines)"}

        removed = lines[start_line - 1:end]
        removed_preview = "\n".join(f"- {l}" for l in removed)

        if operation == "delete":
            new_lines = lines[:start_line - 1] + lines[end:]
            diff = removed_preview
        else: 
            inserted = content.splitlines()
            new_lines = lines[:start_line - 1] + inserted + lines[end:]
            added_preview = "\n".join(f"+ {l}" for l in inserted)
            diff = removed_preview + ("\n" if removed_preview and added_preview else "") + added_preview

    with open(full, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines) + ("\n" if had_trailing_newline else ""))

    return {"content": f"Applied '{operation}' to {path}:\n{diff}"}


def list_files(path: str = ".", pattern: str = "*") -> dict:
    try:
        full = resolve_path(path)
    except ValueError as e:
        return {"error": str(e)}

    if not os.path.exists(full):
        return {"error": f"Path not found: {path}"}
    if not os.path.isdir(full):
        return {"error": f"Not a directory: {path}"}

    entries = []
    for name in sorted(os.listdir(full)):
        if name.startswith(".") and name != ".agent":
            continue
        if not fnmatch.fnmatch(name, pattern):
            continue
        kind = "dir" if os.path.isdir(os.path.join(full, name)) else "file"
        entries.append(f"[{kind}] {name}")

    return {"content": "\n".join(entries) if entries else "(no matching entries)"}



HF_BASE_URL = "https://huggingface.co"
HF_TIMEOUT = 15


def _hf_headers() -> dict:
    token = os.environ.get("HF_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def paper_search(query: str, limit: int = 5) -> dict:
    if not query or not query.strip():
        return {"error": "query must be a non-empty string"}

    try:
        resp = requests.get(
            f"{HF_BASE_URL}/api/papers/search",
            params={"q": query},
            headers=_hf_headers(),
            timeout=HF_TIMEOUT,
        )
    except requests.RequestException as e:
        return {"error": f"Request to HF Papers API failed: {e}"}

    if resp.status_code == 429:
        return {"error": "Rate limited by Hugging Face. Set HF_TOKEN to raise limits, or retry shortly."}
    if not resp.ok:
        return {"error": f"HF Papers API returned {resp.status_code}: {resp.text[:200]}"}

    data = resp.json()
    raw_items = data if isinstance(data, list) else data.get("papers", [])

    papers = []
    for item in raw_items[:limit]:
        paper = item.get("paper", item)
        arxiv_id = paper.get("id") or paper.get("arxiv_id")
        papers.append({
            "arxiv_id": arxiv_id,
            "title": (paper.get("title") or "").strip(),
            "abstract": (paper.get("summary") or paper.get("abstract") or "").strip(),
            "url": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None,
        })

    return {"papers": papers}


def read_paper(arxiv_id: str) -> dict:
    if not arxiv_id or not arxiv_id.strip():
        return {"error": "arxiv_id must be a non-empty string"}

    arxiv_id = arxiv_id.strip().replace("arXiv:", "").rstrip("/").split("/")[-1]
    if arxiv_id.endswith(".pdf"):
        arxiv_id = arxiv_id[:-4]

    try:
        meta_resp = requests.get(
            f"{HF_BASE_URL}/api/papers/{arxiv_id}",
            headers=_hf_headers(),
            timeout=HF_TIMEOUT,
        )
    except requests.RequestException as e:
        return {"error": f"Request to HF Papers API failed: {e}"}

    if meta_resp.status_code == 404:
        return {"error": f"No paper found on Hugging Face for arxiv_id '{arxiv_id}'."}
    if meta_resp.status_code == 429:
        return {"error": "Rate limited by Hugging Face. Set HF_TOKEN to raise limits, or retry shortly."}
    if not meta_resp.ok:
        return {"error": f"HF Papers API returned {meta_resp.status_code}: {meta_resp.text[:200]}"}

    raw = meta_resp.json()
    paper = raw.get("paper", raw)
    title = (paper.get("title") or "").strip()
    abstract = (paper.get("summary") or paper.get("abstract") or "").strip()
    authors = [a.get("name", a) if isinstance(a, dict) else a for a in paper.get("authors", [])]

    content = None
    try:
        md_resp = requests.get(f"{HF_BASE_URL}/papers/{arxiv_id}.md", headers=_hf_headers(), timeout=HF_TIMEOUT)
        if md_resp.ok and md_resp.text.strip():
            content = md_resp.text.strip()
    except requests.RequestException:
        pass

    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "abstract": abstract,
        "content": content or abstract,
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "authors": authors,
    }



SERPER_URL = "https://google.serper.dev/search"
WEB_TIMEOUT = 15


def web_search(query: str, num_results: int = 5) -> dict:
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        return {"error": "SERPER_API_KEY is not set in the environment."}
    if not query or not query.strip():
        return {"error": "query must be a non-empty string"}

    try:
        resp = requests.post(
            SERPER_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": num_results},
            timeout=WEB_TIMEOUT,
        )
    except requests.RequestException as e:
        return {"error": f"Serper request failed: {e}"}

    if not resp.ok:
        return {"error": f"Serper API returned {resp.status_code}: {resp.text[:200]}"}

    data = resp.json()
    organic = data.get("organic", [])[:num_results]
    results = [
        {"title": r.get("title", ""), "link": r.get("link", ""), "snippet": r.get("snippet", "")}
        for r in organic
    ]

    return {"content": results} if results else {"content": [], "note": f"No results for: {query}"}


def web_fetch(url: str) -> dict:
    if not url or not url.strip():
        return {"error": "url must be a non-empty string"}

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return {"error": f"Could not download content from {url}"}

    extracted = trafilatura.extract(downloaded, output_format="markdown")
    if not extracted:
        extracted = markdownify(downloaded)

    if not extracted or not extracted.strip():
        return {"error": f"No readable content extracted from {url}"}

    return {"content": extracted.strip()}



TOOLS = {
    "web_search": web_search,
    "web_fetch": web_fetch,
    "paper_search": paper_search,
    "read_paper": read_paper,
    "read_file": read_file,
    "write_file": write_file,
    "list_files": list_files,
    "edit_file": edit_file,
}

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web for current info, news, or general queries.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "num_results": {"type": "integer", "default": 5}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "Fetch and extract readable content from a web page URL.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "paper_search",
        "description": "Search academic papers on arXiv via the Hugging Face Papers API. "
                        "Use for academic/technical topics.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer", "default": 5}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_paper",
        "description": "Read a paper's metadata and full content by arXiv ID (e.g. '2307.08691').",
        "parameters": {"type": "object", "properties": {"arxiv_id": {"type": "string"}},
                        "required": ["arxiv_id"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file from the project directory, with optional line range.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "start_line": {"type": "integer", "default": 1},
            "read_lines": {"type": "integer", "default": 200}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write content to a file in the project directory, creating it if needed.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "list_files",
        "description": "List files and directories at a given path, optionally filtered by glob pattern.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "default": "."}, "pattern": {"type": "string", "default": "*"}}}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Edit an existing file by line range: replace, delete, or append lines.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "operation": {"type": "string", "enum": ["replace", "delete", "append"]},
            "start_line": {"type": "integer"}, "end_line": {"type": "integer"},
            "content": {"type": "string"}}, "required": ["path", "operation"]}}},
]


class Agent:
    def __init__(self, session_id: str | None = None):
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set. Check your .env file.")

        self.client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)

        if session_id:
            session = load_session(session_id)
            self.session_id = session_id
            self.title = session.get("title", "Untitled")
            self.messages = session["messages"]
        else:
            self.session_id = create_session()
            self.title = "Untitled"
            self.messages = [{"role": "system", "content": build_system_prompt()}]

    def dispatch(self, name: str, arguments: dict) -> dict:
        fn = TOOLS.get(name)
        if fn is None:
            return {"error": f"Unknown tool: {name}"}
        try:
            return fn(**arguments)
        except Exception as e:
            return {"error": f"Tool '{name}' raised an exception: {e}"}

    def _emit(self, event: str, **kwargs) -> None:
        pass

    def _run_loop(self) -> str:
        while True:
            response = self.client.chat.completions.create(
                model=MODEL,
                messages=self.messages,
                tools=TOOL_SCHEMAS,
            )
            choice = response.choices[0].message
            self.messages.append(choice.model_dump(exclude_none=True))

            if not choice.tool_calls:
                return choice.content or ""

            for call in choice.tool_calls:
                name = call.function.name
                try:
                    arguments = json.loads(call.function.arguments)
                except json.JSONDecodeError:
                    arguments = {}

                self._emit("tool_call", name=name, arguments=arguments)
                result = self.dispatch(name, arguments)
                self._emit("tool_result", name=name, result=result)

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result),
                })

    def chat(self, user_input: str) -> str:
        self.messages.append({"role": "user", "content": user_input})
        answer = self._run_loop()
        self._maybe_auto_title()
        save_session(self.session_id, self.messages, title=self.title)
        return answer

    def _maybe_auto_title(self) -> None:
        if self.title != "Untitled" or len(self.messages) < 3:
            return
        try:
            convo = [m for m in self.messages if m["role"] in ("user", "assistant")
                     and isinstance(m.get("content"), str)][:2]
            resp = self.client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": "Summarize this conversation topic in 5 "
                                                          "words or fewer. Reply with only the title."},
                          *convo],
                max_tokens=20,
            )
            self.title = resp.choices[0].message.content.strip()
        except Exception:
            pass


class REPLAgent(Agent):
    def run_once(self, question: str) -> None:
        print(self.chat(question))

    def run(self) -> None:
        print(f"Research Desk — session {self.session_id}")
        print("Type 'exit' to quit, '/sessions' to list, '/resume <id>' to switch.\n")

        agent = self
        while True:
            try:
                user_input = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                break

            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                break
            if user_input == "/sessions":
                for s in list_sessions():
                    print(f"  {s['id']}  {s['title']}  ({s['updated_at']})")
                continue
            if user_input.startswith("/resume "):
                target_id = user_input.split(" ", 1)[1].strip()
                try:
                    agent = REPLAgent(session_id=target_id)
                    print(f"Resumed session {target_id} ({agent.title})")
                except FileNotFoundError as e:
                    print(str(e))
                continue

            print(f"\n{agent.chat(user_input)}\n")


def main() -> None:
    args = sys.argv[1:]

    session_id = None
    if "--session" in args:
        idx = args.index("--session")
        session_id = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    if args:
        REPLAgent(session_id=session_id).run_once(" ".join(args))
    else:
        REPLAgent(session_id=session_id).run()




if __name__ == "__main__":
    main()
