# Inkelus

A desktop chat app for your local LLMs, written in pure Python. It uses only
tkinter and the standard library: no pip installs, no Node, and no admin
rights needed. It includes its own **sandbox browser**, so a model can search
the web and read pages without touching any browser on your computer.

It is styled after the **Ink** and **Glass** interface styles from NXBNVNB.
Ink is charcoal ink, cream paper and gold, with serif type, thin square frames
and a left rail where the active item gets a gold wash. Glass uses dark slate
tiles, condensed type and a yellow accent. You switch between them in Settings.

## Run it

```bash
python run.py
```

On Windows you can also double-click **`run.pyw`**, which opens without a
console window. `python -m inkelus` works too.

You need **Python 3.8+ with Tk**. The python.org installers for Windows and
macOS include Tk. On Linux, install your distro's `python3-tk` package if
`import tkinter` fails.

Then start [Ollama](https://ollama.com) and pull a model from the
**Models** screen, for example `qwen2.5:1.5b`.

## Features

- **Chat**
  - Replies stream in as they're written.
  - Markdown is formatted: code blocks with a Copy button, tables, lists,
    links, bold and italic.
  - Reasoning from thinking models (deepseek-r1, qwen3…) is shown in a
    separate block.
  - You can attach images for vision models.
  - Copy / Edit / Retry / Delete on messages, and tokens/s stats.
  - `Esc` stops a reply.
- **Conversations**: saved automatically. You can search and rename them
  (click the title), export one to Markdown, and back up or restore all of
  them as JSON. Right-click a conversation for more options.
- **Per-chat parameters**: system prompt, temperature, top-p, context length
  and max tokens.
- **Models** (Ollama):
  - See which models are in memory, with VRAM, GPU % and time until unload.
  - Load or unload a model.
  - View a model's details: template, parameters, license, context length.
  - Pull new models with a progress bar, and delete models.
- **Multiple servers**: Ollama, plus OpenAI-compatible servers (LM Studio,
  llama.cpp `llama-server`, vLLM, LocalAI, Jan…). Models from every server
  appear in one picker.

## Web sandbox

Click **Web · off** in a chat's header to turn it on. The model gets five
read-only tools:

| Tool | What it does |
|---|---|
| `web_search` | Searches DuckDuckGo and returns titles, URLs and snippets |
| `open_page` | Opens a URL and returns its readable text and a numbered list of links |
| `follow_link` | Opens link *N* from the last page |
| `read_more` | Continues reading a long page |
| `find_in_page` | Finds a word or phrase in the last page |

It's a sandbox. It is **not** BrowserOS or Chrome, and it never touches any
browser on your computer:

- GET requests only. It can't click, submit forms, log in or post anything.
- No cookies, no saved logins, no JavaScript, and nothing is saved to disk.
- Only text pages are read (HTML, text, JSON, XML), up to 3 MB each.
- Local and intranet addresses (localhost, 10.x, 192.168.x, `.local`,
  `.corp`…) are refused, even through redirects. You can allow them in
  Settings.
- Each chat has its own sandbox.
- By default it **asks before each page or search**: Allow, Allow all in
  this chat, or Deny. Each step shows in the chat as a ⚙ card, which you can
  click to see the full result.

You need a model that supports tool calling, such as qwen2.5 / qwen3,
llama3.1+, mistral-nemo or command-r. Even `qwen2.5:1.5b` works.

## Servers

Ollama at `http://localhost:11434` is set up by default. To add others, go to
**Settings › Servers**:

| Server | Type | Base URL |
|---|---|---|
| Ollama | Ollama | `http://localhost:11434` |
| LM Studio | OpenAI-compatible | `http://localhost:1234/v1` |
| llama.cpp `llama-server` | OpenAI-compatible | `http://localhost:8080/v1` |
| vLLM | OpenAI-compatible | `http://localhost:8000/v1` |

Pull, delete, load/unload and the "In memory" list need Ollama's native API.
OpenAI-compatible servers support listing models and chatting, including
tool calls.

## Work laptops

- **Nothing to install** beyond Python. The bundled fonts (Kaisei Tokumin,
  Cinzel, Inter, Oswald) are loaded for this app's process only, not
  installed system-wide. If that's blocked, the app falls back to Georgia or
  Segoe UI.
- **Corporate proxies**:
  - Requests to your model servers on `localhost` skip any `HTTP(S)_PROXY`
    setting.
  - The web sandbox goes through your proxy like any other app.
- **Your data** stays in your user folder: `%APPDATA%\Inkelus` on Windows,
  `~/Library/Application Support/Inkelus` on macOS, `~/.config/inkelus` on
  Linux. Set `INKELUS_HOME` to use a different folder.

## Keyboard

- `Enter` sends, `Shift+Enter` adds a new line
- `Esc` stops generation
- `Ctrl+N` starts a new chat

## Files

```
run.py / run.pyw          start the app
inkelus/app.py            window, views, chat + tool loop
inkelus/providers.py      Ollama + OpenAI-compatible clients (streaming, tool calls)
inkelus/web.py            the sandbox browser (read-only web tools)
inkelus/mdtk.py           Markdown → Tk text renderer
inkelus/widgets.py        Ink/Glass widgets: paper slip button, nav rail rows, panels
inkelus/theme.py          Ink and Glass palettes, font loading
inkelus/net.py            urllib helpers
inkelus/store.py          settings + chats on disk
inkelus/assets/           fonts and ink art from NXBNVNB
```
