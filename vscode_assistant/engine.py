"""The chat turn, shared by the desktop window and the browser version.

run_turn() talks to the model, runs any web-sandbox tool calls it asks for,
feeds the results back, and repeats until the model answers in plain text.
It runs on a worker thread and reports through callbacks, so it knows
nothing about Tk or HTTP.
"""
import json

from . import agent_browser, net, providers, web

TOOL_RESULT_LIMIT = 15000


def split_model(v):
    if not v or "::" not in v:
        return None, None
    a, b = v.split("::", 1)
    return a, b


def context_size(system, messages):
    """Ollama's default context (2–4k tokens) silently cuts off long documents;
    pick a window that fits them, unless the chat sets num_ctx itself."""
    chars = len(system) + sum(len(m.get("content") or "") for m in messages)
    need = int(chars / 3.2) + 2048
    size = 4096
    while size < need and size < 32768:
        size *= 2
    return size


def compact(messages, keep=2):
    """Shorten all but the latest few browser page snapshots, so long browser
    sessions still fit in a small model's context window."""
    idx = [i for i, m in enumerate(messages) if m["role"] == "tool" and m.get("name") in agent_browser.TOOL_NAMES]
    old = set(idx[:-keep]) if len(idx) > keep else set()
    if not old:
        return messages
    out = []
    for i, m in enumerate(messages):
        if i in old and len(m.get("content") or "") > 300:
            head = "\n".join((m["content"] or "").split("\n")[:3])
            m = dict(m, content=head + "\n(older page snapshot shortened)")
        out.append(m)
    return out


def agent_for(store):
    """The shared agent browser for this app's data folder."""
    return agent_browser.shared(store.dir, (store.settings.get("agent") or {}).get("chrome_path", ""))


def needs_confirm(store, c, name):
    """Whether this tool call needs the user's OK before it runs."""
    if name in agent_browser.SELF_CONFIRMING:
        return False   # browser_login asks for approval itself, every time, whatever the settings
    if c.get("allow_all_tools"):
        return False
    if name in agent_browser.TOOL_NAMES:
        mode = (store.settings.get("agent") or {}).get("confirm", "actions")
        return mode == "all" or (mode == "actions" and name not in agent_browser.READ_ONLY)
    return bool(store.settings["web"].get("confirm", True))


def run_turn(store, library, c, conn, model, cancel, emit, ask_tool, sandbox, agent=None):
    """One reply, including any tool round-trips.

    emit(kind, *args) is called with:
      "update"           a message was added or finished (redraw the chat)
      "delta"            text streamed into the last message
      "done", info       finished; info is a stats dict, "stopped" or "error"
      "toast", text      an error worth showing
    ask_tool(c, call, cancel) -> bool decides whether a web action may run.
    sandbox is this chat's web.Sandbox, or None when the web is off.
    agent is the shared agent_browser.AgentBrowser when this chat may drive the browser window.
    """
    prov = providers.create(conn)
    bs = store.settings["web"]
    tools = None
    try:
        if c.get("browser") and sandbox is not None:
            tools = web.TOOLS if bs.get("allow_search", True) else \
                [t for t in web.TOOLS if t["function"]["name"] != "web_search"]
            sandbox.allow_private = bool(bs.get("allow_private"))
            sandbox.allow_search = bool(bs.get("allow_search", True))
        if c.get("agent") and agent is not None:
            tools = (tools or []) + agent_browser.TOOLS
        driving = bool(c.get("agent") and agent is not None)
        steps = int(bs.get("max_steps") or 12)
        if driving:  # browser tasks take many small steps
            steps = max(steps, int((store.settings.get("agent") or {}).get("max_steps") or 25))
        lib_context = library.context_prompt(c.get("docs"), c.get("template", "auto")) if library else ""
        params = dict(c["params"])
        if (lib_context or driving) and not params.get("num_ctx") and conn["type"] == "ollama":
            params["num_ctx"] = max(context_size(lib_context, c["messages"]), 16384 if driving else 0)
        for step in range(steps + 1):
            history = []
            system = (c["params"].get("system") or "").strip()
            if lib_context:
                system = (system + "\n\n" if system else "") + lib_context
            if tools and sandbox is not None and c.get("browser"):
                system = (system + "\n\n" if system else "") + web.SYSTEM_PROMPT
            if tools and agent is not None and c.get("agent"):
                system = (system + "\n\n" if system else "") + agent_browser.SYSTEM_PROMPT
            if system:
                history.append({"role": "system", "content": system})
            history += compact([m for m in c["messages"] if not m.get("error") and m["role"] != "note"])
            reply = {"role": "assistant", "content": "", "thinking": "", "model": c["model"]}
            c["messages"].append(reply)
            emit("update")

            def on_delta(text, think, r=reply):
                r["content"] += text
                if think:
                    r["thinking"] += think
                emit("delta")

            calls, stats = prov.chat(model, history, params, on_delta, cancel,
                                     tools=tools if step < steps else None,
                                     keep_alive=store.settings["defaults"].get("keep_alive") or None)
            reply["stats"] = stats
            if not calls:
                if not reply["content"].strip() and not reply["thinking"]:
                    reply["content"] = "_(empty reply)_"
                emit("done", stats)
                return
            reply["tool_calls"] = calls
            for call in calls:
                if cancel.is_set():
                    raise net.Cancelled()
                res = {"role": "tool", "name": call["name"], "tool_call_id": call["id"],
                       "arguments": call["arguments"], "content": "", "status": "running"}
                c["messages"].append(res)
                emit("update")
                names = [t["function"]["name"] for t in tools or []]
                if call["name"] not in names:
                    res.update(content="Unknown tool %s." % call["name"], status="error", is_error=True)
                    continue
                is_agent = call["name"] in agent_browser.TOOL_NAMES
                if needs_confirm(store, c, call["name"]) and not ask_tool(c, call, cancel):
                    res.update(content="The user declined this %s action." % ("browser" if is_agent else "web"),
                               status="denied", is_error=True)
                    continue
                try:
                    if is_agent:
                        text, is_err = agent.run(c["id"], call["name"], call["arguments"]), False
                    else:
                        text, is_err = sandbox.run(call["name"], call["arguments"]), False
                except Exception as e:  # report tool failures back to the model
                    text, is_err = "Tool error: %s" % e, True
                if len(text) > TOOL_RESULT_LIMIT:
                    text = text[:TOOL_RESULT_LIMIT] + "\n…[truncated %d chars]" % (len(text) - TOOL_RESULT_LIMIT)
                res.update(content=text, status="error" if is_err else "done", is_error=is_err)
                emit("update")
        emit("done", None)
    except net.Cancelled:
        last = c["messages"][-1] if c["messages"] else None
        if last and last["role"] == "assistant":
            last["content"] += ("\n\n" if last["content"] else "") + "_— stopped —_"
        elif last and last["role"] == "tool" and last.get("status") == "running":
            last.update(status="cancelled", content="Cancelled by the user.", is_error=True)
        emit("done", "stopped")
    except Exception as e:
        last = c["messages"][-1] if c["messages"] else None
        if last and last["role"] == "assistant" and not last["content"] and not last.get("tool_calls"):
            last["error"] = str(e)
        else:
            c["messages"].append({"role": "assistant", "content": "", "error": str(e), "model": c["model"]})
        emit("done", "error")
        emit("toast", str(e))


def chat_markdown(c):
    out = ["# " + c["title"], ""]
    for m in c["messages"]:
        if m["role"] == "tool":
            out += ["> ⚙ `%s` %s" % (m.get("name"), json.dumps(m.get("arguments") or {})), ""]
            continue
        who = "You" if m["role"] == "user" else (split_model(m.get("model"))[1] or "Assistant")
        body = "> Error: " + m["error"] if m.get("error") else m.get("content", "")
        if body.strip():
            out += ["### " + who, "", body, ""]
    return "\n".join(out)


def import_chats(store, data):
    """Merge chats from a backup (list, or {"chats": [...]}). Returns how many were added."""
    incoming = data if isinstance(data, list) else data.get("chats")
    have = {c["id"] for c in store.chats}
    added = [c for c in incoming or [] if isinstance(c, dict) and c.get("id") and
             isinstance(c.get("messages"), list) and c["id"] not in have]
    for c in added:
        c.setdefault("params", {"system": "", "temperature": 0.7, "top_p": 0.9})
        c.setdefault("updated", 0)
        c.setdefault("title", "Imported")
        for m in c["messages"]:  # older web builds stored images as data: URLs
            m["images"] = [i.split(",", 1)[-1] for i in m.get("images") or []]
    store.chats.extend(added)
    return len(added)
