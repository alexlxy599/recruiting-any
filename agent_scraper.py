"""Agentic web browser for discovering lab members.

Supports two modes:
1. Anthropic direct API — uses Claude's native web_search tool (most reliable)
2. OpenAI-compatible (OpenRouter, etc.) — uses custom tools with visit_page

The agent autonomously searches the web, visits pages, and extracts
structured lab member information.
"""

import json
import re
import requests
from bs4 import BeautifulSoup
import anthropic
from openai import OpenAI

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
})


def _visit_page(url: str) -> str:
    """Fetch a web page and return cleaned text (max 15k chars)."""
    try:
        resp = _session.get(url, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()

        # Preserve link URLs inline for the agent to follow
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith(("http", "/")):
                text = a.get_text(strip=True)
                if text:
                    a.string = f"{text} [{href}]"

        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:15000]
    except Exception as e:
        return f"Error fetching page: {e}"


# ---------------------------------------------------------------------------
# Anthropic native mode (with built-in web_search)
# ---------------------------------------------------------------------------

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 15,
}

ANTHROPIC_CUSTOM_TOOLS = [
    {
        "name": "visit_page",
        "description": (
            "Visit a specific web page and read its full text content. "
            "URLs found in the text are shown as [url] so you can follow links."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The full URL to visit"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "submit_results",
        "description": "Submit the final list of lab members found.",
        "input_schema": {
            "type": "object",
            "properties": {
                "members": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string", "description": "PhD Student, Postdoc, Professor, etc."},
                            "email": {"type": "string"},
                            "research_area": {"type": "string"},
                            "personal_page": {"type": "string"},
                            "google_scholar": {"type": "string"},
                            "institution": {"type": "string"},
                            "advisor": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": ["members"],
        },
    },
]


# ---------------------------------------------------------------------------
# OpenAI-compatible mode (OpenRouter, etc.) — tools as functions
# ---------------------------------------------------------------------------

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "visit_page",
            "description": (
                "Visit a specific web page and read its full text content. "
                "URLs found in the text are shown as [url] so you can follow links. "
                "Use this to read lab homepages, people/team pages, Google Scholar, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The full URL to visit"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_results",
            "description": "Submit the final list of lab members found. Call when done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "members": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "role": {"type": "string"},
                                "email": {"type": "string"},
                                "research_area": {"type": "string"},
                                "personal_page": {"type": "string"},
                                "google_scholar": {"type": "string"},
                                "institution": {"type": "string"},
                                "advisor": {"type": "string"},
                            },
                            "required": ["name"],
                        },
                    },
                },
                "required": ["members"],
            },
        },
    },
]


SYSTEM_PROMPT = """\
You are a research assistant that finds lab members by browsing the web.

Given a query (professor name, lab name, or research topic), your job is to:
1. Search the web to find the professor's homepage or lab page
2. Visit the page and look for a "People", "Team", or "Members" section
3. Extract all current lab members with their details
4. If the people page lists personal homepages, visit a few key ones for more info
5. Submit the complete list using submit_results

Strategy:
- Search for the professor/lab name to find their homepage
- On the homepage, look for links to People/Team/Group/Members/Students pages
- Use visit_page to read those pages in detail
- For each member capture: name, role (PhD/Postdoc/etc), research area, email, personal page
- Include the professor/PI themselves in the results
- Breadth over depth — don't spend too many steps on one member
- Complete within 10-15 tool calls total

Submit results as soon as you have a good list."""

# For OpenAI mode where there's no built-in web search, give extra guidance
SYSTEM_PROMPT_NO_WEBSEARCH = """\
You are a research assistant that finds lab members by visiting web pages.

Given a query (professor name, lab name, or research topic), your job is to:
1. Figure out likely URLs for the professor's homepage or lab page
2. Visit those pages and look for "People", "Team", or "Members" sections
3. Extract all current lab members with their details
4. Submit the complete list using submit_results

Since you don't have a web search tool, use these strategies to find pages:
- Try common university URL patterns: https://www.cs.{university}.edu/~{lastname}/
- Try Google Scholar: https://scholar.google.com/citations?mauthors={name}&hl=en&view_op=search_authors
- Try DBLP: https://dblp.org/search?q={name}
- Visit the university's CS department page and look for the professor
- Once you find the homepage, look for People/Team/Group/Members links
- For each member capture: name, role, research area, email, personal page
- Include the professor/PI themselves in the results

Be resourceful and try multiple URL patterns. Submit results as soon as you have a good list."""


def _run_anthropic(query: str, api_key: str, model: str, max_turns: int,
                   report) -> list[dict]:
    """Run agent loop using Anthropic API with native web_search."""
    client = anthropic.Anthropic(api_key=api_key)
    messages = [
        {"role": "user", "content": f"Find all current lab members for: {query}"},
    ]
    all_tools = [WEB_SEARCH_TOOL] + ANTHROPIC_CUSTOM_TOOLS

    for turn in range(max_turns):
        report(f"Thinking... (step {turn + 1})")

        response = client.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=all_tools,
            messages=messages,
        )

        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        if response.stop_reason == "end_turn":
            report("Requesting final results...")
            messages.append({
                "role": "user",
                "content": "Please call submit_results with the members you've found.",
            })
            continue

        tool_results = []
        for block in assistant_content:
            if block.type == "server_tool_use":
                report("Searching the web...")
                continue
            elif block.type == "web_search_tool_result":
                n = len(block.content) if hasattr(block, "content") and block.content else 0
                report(f"Got {n} search results")
                continue
            elif block.type != "tool_use":
                continue

            if block.name == "submit_results":
                members = block.input.get("members", [])
                report(f"Found {len(members)} lab members")
                return members

            if block.name == "visit_page":
                url = block.input.get("url", "")
                display = url[:80] + "..." if len(url) > 80 else url
                report(f"Visiting: {display}")
                result = _visit_page(url)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

    report("Reached max steps")
    return []


def _run_openai_compat(query: str, api_key: str, base_url: str, model: str,
                       max_turns: int, report) -> list[dict]:
    """Run agent loop using OpenAI-compatible API (OpenRouter, etc.)."""
    client = OpenAI(api_key=api_key, base_url=base_url)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_NO_WEBSEARCH},
        {"role": "user", "content": f"Find all current lab members for: {query}"},
    ]

    for turn in range(max_turns):
        report(f"Thinking... (step {turn + 1})")

        response = client.chat.completions.create(
            model=model,
            max_tokens=16000,
            messages=messages,
            tools=OPENAI_TOOLS,
            tool_choice="auto",
        )

        msg = response.choices[0].message
        messages.append(msg)

        if not msg.tool_calls:
            if response.choices[0].finish_reason == "stop":
                report("Requesting final results...")
                messages.append({
                    "role": "user",
                    "content": "Please call submit_results with the members you've found.",
                })
                continue
            break

        for tc in msg.tool_calls:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            if fn_name == "submit_results":
                members = fn_args.get("members", [])
                report(f"Found {len(members)} lab members")
                return members

            if fn_name == "visit_page":
                url = fn_args.get("url", "")
                display = url[:80] + "..." if len(url) > 80 else url
                report(f"Visiting: {display}")
                result = _visit_page(url)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

    report("Reached max steps")
    return []


def run_agent_search(query: str, api_key: str, status_cb=None,
                     model: str = "claude-sonnet-4-20250514",
                     base_url: str = "",
                     max_turns: int = 25) -> list[dict]:
    """Run the agentic search loop. Returns list of member dicts.

    If base_url is empty, uses Anthropic API directly (with native web_search).
    If base_url is set, uses OpenAI-compatible API (OpenRouter, etc.).
    """
    def report(msg):
        if status_cb:
            status_cb(msg)

    report(f"Starting search for: {query}")

    if base_url:
        report(f"Using OpenAI-compatible API: {base_url}")
        return _run_openai_compat(query, api_key, base_url, model, max_turns, report)
    else:
        report("Using Anthropic API with web search")
        return _run_anthropic(query, api_key, model, max_turns, report)
