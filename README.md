# VSCode Assistant

A local AI **command center** for the models on your own computer, written in
pure Python with only the standard library: no pip installs, no Node, and no
admin rights needed. It runs as a small server on your computer and opens in
its own browser window, and from there it opens whatever else it needs, such
as the **agent browser** window the AI can drive.

It brings together chat with your local models (Ollama, LM Studio,
llama.cpp…), **Projects** (a code workspace with an editor, Git and a
terminal, where the AI can read, change and test your code), a read-only
**web sandbox**, an **agent browser**, a **Library** of documents and
templates, an encrypted **Passwords** vault, and chat **groups**.

## Run it

```bash
python run.py
```

On Windows you can double-click **`run.pyw`**, which starts without a console
window. `python -m vscode_assistant` works too.

It starts a small server on your own computer (`http://127.0.0.1:8765`, never
reachable from other machines) and opens the app in Chrome as its own window,
or in Edge or your default browser if Chrome isn't installed. Options:

- `--tab`: open as a normal browser tab instead of an app window
- `--no-open`: just start the server; open the printed address yourself
- `--port 9000`: use a different port
- `--keep-running`: don't stop when the last app window closes

You need **Python 3.8+**. Then start [Ollama](https://ollama.com) and pull a
model from the **Models** screen, for example `qwen2.5:7b`.

The server stops by itself about 30 seconds after you close the last app
window (unless a reply or a model download is still running). Launching it
again while it's running just reopens the window. You can also press `Ctrl+C`
in its console.

**Appearance:** the default **Warm dark** theme has charcoal greys, a clay
accent, rounded shapes, serif replies in one centred column, and a greeting
on the start page. **Blue dark**, **Light** and **Match system** are in
**Settings › Appearance**. You can paste or drag images into the message box.

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
- **Projects**: open, clone or import a code project; edit files, run
  commands, and commit, pull and push with Git; let the AI read, change
  and test the code with your approval. See [Projects](#projects) below.
- **Library**: a place to keep **documents** (reference material the AI
  can read) and **templates** (examples of how answers should be laid out).
  See [Library](#library) below.
- **Multiple servers**: Ollama, plus OpenAI-compatible servers (LM Studio,
  llama.cpp `llama-server`, vLLM, LocalAI, Jan…). Models from every server
  appear in one picker under **Settings › Model**.

## Chat groups, Fast mode and the thinking indicator

- **Groups.** Click **+** next to *Recents* to make a group and type its name
  (a project, a class, "Work"…). Drag chats onto a group to move them;
  chats not in a group are under **General**. Click a group's name to
  collapse it; hover it to start a new chat in it (**+**), rename it (**✎**,
  or double-click the name) or delete it (**✕**; its chats move back to
  General).
- **Fast mode.** The **⚡ Fast** button by the message box tells reasoning
  models (qwen3, deepseek-r1…) to skip their thinking step and answer straight
  away. It's remembered per chat and used for new chats until you switch it
  off. Ollama gets `think: false`; OpenAI-compatible servers get
  `enable_thinking: false` (llama.cpp, vLLM). If a server doesn't support it,
  the request is simply sent again without it.
- **Thinking indicator.** Until the first words of a reply arrive, it shows
  an animated *Working*, *Thinking* (while a reasoning model reasons) or
  *Answering* (in Fast mode) with a seconds counter. Running tool steps show
  a small spinner.

## Projects

Work on your code without opening another editor. **Projects** in the sidebar:

- **Add a project** three ways: **open a folder** that's already on your
  computer (a repo you cloned), **clone from GitHub** (paste the address), or
  **import a ZIP** (e.g. GitHub's *Download ZIP*). Files are worked on in
  place; nothing is copied or uploaded.
- **Files**: a file tree (it respects `.gitignore`) and an editor with line
  numbers, auto-indent and `Ctrl+S` to save. **Ask AI** starts a chat about
  the open file.
- **Terminal**: run any command in the project folder (`pytest`, `npm test`,
  `git log`…) and see the output as it arrives. `↑`/`↓` recall earlier
  commands.
- **Git**, using the Git installed on your computer:
  - The bar at the top shows the branch and how many commits you are ahead
    ↑ or behind ↓ the remote. Click the branch to switch or create one.
  - **Changes** lists changed files. Click one to see its diff, untick
    files to leave them out, press **✨ Write message** to have the AI
    describe the changes, then **Commit**. ↺ discards a file's changes.
  - **Pull** only fast-forwards. If your branch and the remote have both
    changed, it asks whether to **merge** or **rebase**. If lines conflict,
    the files are marked: fix them (or ask the AI to), then **Continue**, or
    **Abort** to go back.
  - **Push** shows which commits will be sent and asks first. It never
    force-pushes, and refuses while the remote has commits you haven't
    pulled. A new branch is created on the remote for you.
  - **History** lists recent commits.
  - **Logging in to GitHub** is left to Git: Git Credential Manager (part of
    Git for Windows), an SSH key, or `gh auth login`. If `git push` works in
    your own terminal, it works here; the app never sees your credentials.
    Git isn't installed? Get it from <https://git-scm.com>.

**Chatting about a project.** Press **Chat about this project**, or turn on
**Project** in any chat's header. The AI then gets tools to:

- **look around**: list files, read files, search the code, and see the Git
  status, diff and log. These run without asking.
- **change files** (edit, create, delete). Each change is shown to you as a
  diff first: **Apply change**, **Reject** (you can add a note telling the AI
  why or what to do instead), or **Allow all edits in this chat**. Applied
  changes have an **Undo this change** button in the chat.
- **run commands**, e.g. the tests, so it can check its work and fix what
  fails. You approve each command; tick *Always allow `npm test` in this
  project* for ones you trust. Commands that chain others (`;`, `&&`, `|`)
  always ask.
- **commit, pull, switch branches and push.** Commits, pulls and branch
  switches ask first. **Every push asks**, showing the commits, and "allow
  all" never covers pushes.

It can only reach files inside the project folder, and never inside `.git`.
**Project settings** has notes for the AI (how to run the project,
conventions, what to avoid; added to every chat about it), the list of
always-allowed commands, and **protected branches** the AI may not push to
(you still can). *Remove from the app* only takes a project off the list;
its files stay where they are.

Coding works best with a model that's good at tool calling, such as
`qwen2.5-coder:14b`, `qwen3:14b` or larger. Small models (7–8B) manage
simple fixes but often lose track in multi-step changes.

## Library

Open **Library** in the left rail. It has two tabs:

- **Documents**: notes, specs, policies, code, anything the AI should know
  about. Create them in the editor, or click **Import…** to add text files
  (`.txt`, `.md`, `.csv`, `.json`, source code…) or Word `.docx` files, which
  are converted to text.
- **Templates**: examples of how an answer should look. Write them with
  headings, tables and `{{placeholders}}`; the AI copies the layout and fills
  in the blanks. Three examples come with the app: meeting notes, a bug
  report and an email.

In a chat, click **Library** in the header to choose:

- which **documents** the AI reads in that chat, and
- which **template** it follows. **Auto** (the default) lets the AI see every
  template and use the one that matches the request (ask for "a bug report
  about…" and it uses *Bug report*). You can also pin one template, or choose
  **None**.

Documents and templates are plain files in the `documents` and `templates`
folders inside your data folder (the **Folder** button opens it), so you can
also edit them with any editor. With Ollama, the app enlarges the context
window when documents are attached so they aren't cut off; set **Context
(num_ctx)** under **Parameters** to choose it yourself.

## Agent browser

Click **Browser · off** in a chat's header to let the model use a real
browser window: open pages, click, type, choose options and press keys. It
works like an AI browser assistant, but it's built into this app and stands
on its own:

- It starts your installed **Chrome** (or Edge or Chromium) as a separate
  window with its **own private profile** in the app's data folder
  (`agent-browser`). It shares no history, cookies, logins or extensions with
  your everyday browser or any other browser app.
- It controls that window over Chrome's built-in DevTools protocol on a
  random local port, using only Python's standard library.
- Each chat gets its own tab. You can watch everything it does, and use the
  window yourself at any time.

How the model sees and uses a page:

- **Page outline from the accessibility tree.** Each page arrives as an
  indented outline of roles, names, states and text, the same structure
  screen readers use, so it stays clear on modern sites. Everything it can
  use carries a short ref like `[e12]`.
- **Clickable things without a proper role** (a `<div>` with a click
  handler or pointer cursor, common in web apps) are found and listed too.
- **Only the changes after each action.** After a click or typing, the model
  gets just the lines that changed when that's shorter than the whole page,
  so long tasks fit in small local models.
- **Real input.** Clicks are real mouse events at the element's position,
  and typing clears the field and types key by key, so sites that listen to
  keystrokes work.
- **Stable refs.** Refs stay the same until the page changes to a new
  document. If an element is rebuilt, it's found again by its role and name.
  After each action it waits for the page to load and stop changing.
- **Pop-up dialogs.** Alerts are closed and reported. Confirm and prompt
  dialogs are handed to the model to answer, which you approve like any
  other action.

| Tool | What it does |
|---|---|
| `browser_open` | Opens a URL |
| `browser_snapshot` | Gets the full page outline again |
| `browser_click` | Clicks a link, button, tab, clickable item… |
| `browser_type` | Clears a text field and types into it, optionally pressing Enter |
| `browser_select` | Chooses an option in a dropdown |
| `browser_check` | Ticks or unticks a checkbox, radio button or switch |
| `browser_press` | Presses a key or shortcut (Enter, Escape, Tab, arrows, `Control+a`…) |
| `browser_scroll` | Scrolls the page or a scrollable element |
| `browser_back` | Goes back a page |
| `browser_find` | Searches the page's text |
| `browser_dialog` | Answers a confirm or prompt dialog |
| `browser_login` | Fills a saved login from your Passwords vault, after you approve |

Safety:

- By default you **approve every action that changes something** (opening,
  clicking, typing…); just reading the page doesn't ask. Change this in
  **Settings › Agent browser** to ask for every step, or
  never.
- The model **can't type into password fields** itself. To sign in, it asks to
  use a login from your **Passwords** vault (below); you can also click **Open
  the agent browser** in Settings and log in yourself, and the private profile
  remembers it.
- Page content is fenced with markers that carry a fresh random code each
  time, so a page can't fake the end of its own content. The model is told
  never to follow instructions found inside, and to ask you before buying,
  posting, sending or deleting anything.

You need a model that supports tool calling; bigger models (7B+) handle
multi-step browsing much better than tiny ones. On some work computers,
company policy blocks Chrome's remote control (`RemoteDebuggingAllowed`); the
app tells you if that happens. Set the browser path in Settings, or the
`VSCODE_ASSISTANT_CHROME` environment variable, if it can't find Chrome.

## Passwords

**Passwords** in the left rail keeps the logins the agent browser may use on
sites that need you to sign in.

- **Encrypted on your computer.** You choose a master password the first time.
  Your logins are encrypted with **AES-256-GCM**, with a key made from the
  master password by **PBKDF2-SHA256 (600,000 rounds)**. Encryption and
  decryption happen inside the app's page, using the browser's built-in Web
  Crypto. Only the encrypted vault is saved, as `vault.json` in the data
  folder; your master password is never stored anywhere.
- **The AI never sees a password.** When a site needs a login, the AI uses
  `browser_login` with the username and password fields' refs. You get a
  **Use a saved login?** prompt showing the site and username (unlock the
  vault there if it's locked), and only after you click **Fill login** is
  that one login sent to the app, typed into the page and forgotten. The
  model is only told that the login was filled; even the password's length
  is hidden from it. "Allow all in this chat" doesn't skip this prompt.
- **Only on the right site.** A login is filled only when the page's address
  matches the site it was saved for (or one of its subdomains), and only on
  https pages (plus localhost). That check runs again just before filling, so
  a look-alike site or a page that redirected away doesn't get it.
- **Locks itself** after 15 minutes without use, and when you close or reload
  the window. There's a password generator, and you can change the master
  password.
- **No recovery.** If you forget the master password, the only option is to
  delete the vault and start again.

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

- **Nothing to install** beyond Python and a browser (Chrome or Edge is best).
  The Inter font is served by the app itself, never installed system-wide.
- **Corporate proxies**:
  - Requests to your model servers on `localhost` skip any `HTTP(S)_PROXY`
    setting.
  - The web sandbox goes through your proxy like any other app.
- **Your data** stays in your user folder: `%APPDATA%\VSCode Assistant` on
  Windows, `~/Library/Application Support/VSCode Assistant` on macOS,
  `~/.config/vscode-assistant` on Linux. Set `VSCODE_ASSISTANT_HOME` to use a
  different folder. If you used the app when it was called Inkelus, its
  existing folder keeps being used, so your chats carry over.

## Keyboard

- `Enter` sends, `Shift+Enter` adds a new line
- `Esc` stops generation (or closes a dialog)
- `Ctrl+S` saves in the Library and Projects editors
- `Tab` / `Shift+Tab` indent and unindent in the Projects editor
- `Ctrl+Enter` commits in the commit message box

## Files

```
run.py / run.pyw                   start the command center (run_web.py / .pyw do the same)
vscode_assistant/server.py         local web server + JSON API; opens the app window
vscode_assistant/static/           the app's page, styles and scripts
vscode_assistant/static/vault.js   the encrypted password vault (Web Crypto, in the page)
vscode_assistant/static/projects.js  the Projects workspace (files, editor, Git, terminal)
vscode_assistant/engine.py         the chat + tool loop
vscode_assistant/projects.py       projects: file access, the AI's coding tools, commands, undo
vscode_assistant/gitops.py         Git commands (status, diff, commit, pull, push, branches)
vscode_assistant/providers.py      Ollama + OpenAI-compatible clients (streaming, tool calls)
vscode_assistant/web.py            the sandbox browser (read-only web tools)
vscode_assistant/agent_browser.py  the agent browser: drives a Chrome window over DevTools
vscode_assistant/library.py        documents + templates, and the prompt text they add
vscode_assistant/net.py            urllib helpers
vscode_assistant/store.py          settings + chats on disk
vscode_assistant/assets/           app icons, logos and the Inter font
```
