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
- **API**: a Postman-style API client: collections and folders, every body
  and auth type, pre-request and test scripts (`pm.*`), a collection runner
  with reports, OpenAPI / Postman / curl import, WebSocket and SSE. It also
  runs your dev server and checks the endpoints in your code against the
  docs. See [API](#api) below.
- **Context**: your GitHub pull requests, Jira issues and email, plus notes
  you add yourself, in one diagram, linked where they mention each other or
  where you linked them, and readable by the model.
  See [Context](#context) below.
- **Library**: a place to keep **documents** (reference material the AI
  can read) and **templates** (examples of how answers should be laid out).
  See [Library](#library) below.
- **Multiple servers**: Ollama, OpenAI-compatible servers (LM Studio,
  llama.cpp `llama-server`, vLLM, LocalAI, Jan…), and **Amazon Bedrock**
  (Claude through your company's AWS account and SSO sign-in). Models from every server
  appear in one picker under **Settings › Model**.

## Sidebar and the + menu

- **Collapse the sidebar** with the button next to the app name, or
  `Ctrl+B`, to get a strip of icons and more room. It stays the way you left
  it. On narrow windows it starts collapsed.
- **New chat** opens a blank conversation. It's only saved once you send a
  message, so empty chats don't pile up.
- The **+** in the message box opens a menu: **Upload images** (for vision
  models), **Upload documents** (text, Markdown, code or Word; they're added
  to *Library › Documents* and the AI reads them in this chat), **Library
  documents and templates**, and **Work on a project**.

## Chat groups, Fast mode and the thinking indicator

- **Groups.** Click the **folder** button next to *Recents* to make a group and type its name
  (a project, a class, "Work"…). Drag chats onto a group to move them;
  chats not in a group are under **General**. Click a group's name to
  collapse it; hover it to start a new chat in it (**+**), rename it (the
  **pencil**, or double-click the name) or delete it (the **x**; its chats
  move back to General).
- **Fast mode.** The **Fast** button by the message box tells reasoning
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
- **Files**: a file tree (it respects `.gitignore`) and a real code editor
  (Monaco, the editor inside VS Code, shipped with the app so it works
  offline): syntax colours for most languages, tabs for several open files
  (unsaved changes are kept when you switch), find and replace, multiple
  cursors, a minimap and `Ctrl+S` to save. **Outline** lists the open file's
  classes and methods. **Ask AI** starts a chat about the open file.
- **Terminal**: run any command in the project folder (`pytest`, `npm test`,
  `git log`…) and see the output as it arrives. `↑`/`↓` recall earlier
  commands.
- **Git**, using the Git installed on your computer:
  - The bar at the top shows the branch and how many commits you are ahead
    ↑ or behind ↓ the remote. Click the branch to switch or create one.
  - **Changes** lists changed files. Click one to see its diff, untick
    files to leave them out, press **Write message** to have the AI
    describe the changes, then **Commit**. The **undo arrow** discards a
    file's changes.
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

**Tests tab.** One place for every test run and every test worth writing:

- **Runs**: each test run is listed as passed or failed with a summary such
  as *2 passed, 1 failed*. That includes runs you start with **Run**, test
  commands you type in the Terminal, and the ones the AI runs. Click a run to
  see the failing tests by name and the full output, then **Ask the AI to fix
  these** or **Run again**. Output from pytest, unittest, Jest, Vitest, Mocha,
  `go test`, `cargo test` and simple PASS/FAIL runners is understood.
- **Suggested**: tests to write or check. **Suggest** asks the AI what's
  missing (it looks at the layout, the existing tests and your uncommitted
  changes). In a project chat the AI also adds suggestions on its own, e.g.
  a test that would have caught a bug it just fixed. You can add your own
  with **+**. Each one has **Write with AI**, **Run** (when it has a command;
  it's then marked passed or failed), **done** and **dismiss**.
- The **test command** is guessed from the project (npm test, pytest, go
  test, cargo test…) and you can change it. The AI is told the command, the
  last result and the open suggestions.

The list is kept in the app's data folder (`tests.json`), not in your
project.

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

## Java (like NetBeans)

Open a Maven, Gradle or NetBeans Ant project and the editor understands Java
the way NetBeans does. The first time, the app downloads the Eclipse Java
language server (about 50 MB, from eclipse.org) and the Java debugger plugin
(from Maven Central) into its data folder; the **Java** bar under the editor
shows the progress, then **Ready**. It needs a JDK 17 or newer to run (21 is
best); your projects can still target older Java versions. Pick the JDK and
the memory it may use in **Settings › Java**. Nothing is written into your
project (no `.classpath`, `.project` or `.settings`).

- **Errors as you type**, underlined in the editor and listed on the
  **Problems** tab under the editor (click one to jump to it).
- **Completion** (`Ctrl+Space`) with parameter hints, and **hover** for a
  class's or method's Javadoc.
- **Go to definition** (`Ctrl+B`, `F12` or Ctrl+click), also into library
  classes such as `String` or a jar's classes (shown read-only, decompiled
  when there's no source). **Go to type** (`Ctrl+O`).
- **Find usages** (`Alt+F7`), listed on the **Usages** tab by file.
- **Rename** (`Ctrl+R`) across every file, **Fix imports**
  (`Ctrl+Shift+I`), **Format** (`Alt+Shift+F`) and **quick fixes**
  (`Alt+Enter`: add an import, create a method, fix a typo…).
- **Run test** / **Debug test** links above each JUnit test and test class,
  **Run** / **Debug** above `main`. Test runs go to the Terminal and the
  Tests list.

**Debugger.** Click left of a line number (or `F9`) to set a breakpoint;
right-click one to give it a condition. Then **Debug** a main class (the
**Debug** button, or the link above `main`) or **Debug test**. When it stops,
the **Debug** tab shows the call stack, the variables (expand objects and
lists), and a console where you can evaluate Java expressions; hovering a
variable in the editor shows its value. NetBeans keys: `F5` continue, `F8`
step over, `F7` step into, `Ctrl+F7` step out, `Shift+F5` stop (`F10`, `F11`
and `Shift+F11` work too). A test is debugged through its build tool (Maven
Surefire, Gradle or NetBeans Ant), so it runs exactly as it does in NetBeans.
Drag the top edge of the bottom panel to make it taller.

**NetBeans projects.**

- Maven: when `mvn` isn't installed separately, the Maven that comes with
  NetBeans is used. The **Run** button uses the Run settings NetBeans saved
  in `nbactions.xml` (main class, arguments, JVM options).
- Ant (`nbproject/`): the source folders and jars come from
  `nbproject/project.properties`, and NetBeans' own libraries (such as
  JUnit) from the IDE's settings that `nbproject/private` points to. Ant
  itself is found inside NetBeans too.

## TypeScript, JavaScript, React and Angular

TypeScript and JavaScript files (`.ts`, `.tsx`, `.js`, `.jsx`) get the same
help as Java, from typescript-language-server: the TypeScript compiler's own
language service, the one VS Code uses. It reads your `tsconfig.json` and
`node_modules` and uses **your project's own TypeScript**, so the errors match
what `tsc` and your build report.

The first time you open a project with a `package.json`, press **Set up
TypeScript support** in the bar under the editor: it installs the language
servers once with npm into the app's data folder (about 30 MB). It needs
Node.js 18 or newer; pick which Node in **Settings › TypeScript and
JavaScript** if it isn't on the PATH.

- **Errors as you type**, the same as `tsc`, and **ESLint** warnings from
  your project's own ESLint and config, both on the **Problems** tab.
- **Completion with auto-imports** (pick `cartTotal` and the `import` line
  is added), hover for types and docs, parameter hints.
- **Go to definition** (`F12`, `Ctrl+B`), into a library's type definitions
  in `node_modules` too; **find usages** (`Alt+F7`); **go to type or
  component** (`Ctrl+O`).
- **Rename** (`Ctrl+R`) across files, **organize imports**
  (`Ctrl+Shift+I`), and **quick fixes** (`Alt+Enter`): add a missing import,
  remove an unused variable, ESLint's fixes and "disable this rule here".
- **Format** (`Alt+Shift+F`) with your project's **Prettier** and its
  config when it has Prettier, else TypeScript's formatter.
- **Run test** links above Vitest and Jest tests (`it`, `test`, `describe`),
  run in the Terminal and recorded on the Tests list.

**Angular.** In an Angular project the Angular language server runs next to
TypeScript's and uses your project's own Angular language service:

- **Templates** (`.html` files and inline `template:` strings) get errors as
  you type (the same as `ng build`), completion of the component's fields
  and methods, hover, and **go to definition** from the template to the
  component.
- **Run the tests in a spec file** with `ng test` (Karma runs whole files;
  results are shown on the Tests list). Projects using Jest or Vitest get a
  link per test.
- Prettier formats templates too when the project has it.

Debugging a React or Angular app in the browser is best done in Chrome's
DevTools.

## Context

The **Context** page shows your GitHub pull requests, Jira issues and email
together, with lines between the items that mention each other, and lets the
model read the same data.

**Set it up** in **Settings › Work context**, then press **Sync**:

- **GitHub**: the repositories (`owner/repo`) and a personal access token
  (read-only is enough; public repos don't need one). Change the API URL
  for GitHub Enterprise.
- **Jira**: your site URL. For Jira Cloud, your login email and an API
  token; for Jira Server / Data Center, leave the email empty and use a
  personal access token. Optionally limit which project keys count, and
  add a JQL query for extra issues (e.g. `assignee = currentUser()`).
- **Email**: any IMAP account (server, username and an app password), a
  folder, and how many days back to read.

**How items are linked.** A sync reads recent pull requests, recent email,
and every Jira issue mentioned in them. Items are joined when one mentions
another: a Jira key like `ABC-123` in a PR's title, branch or description, a
PR link or `owner/repo#12` in an email or issue, or a Jira key in an email.
Key-shaped words like `UTF-8` are ignored, and so is quoted text in replies.
Linked items form a **thread**.

**The tree** (the default view) shows each thread as an outline, like a
folder tree. The main Jira issue is at the top, the pull requests that work
on it are under it, and the emails and notes that discuss each PR are under
that. Every branch says how the items relate ("worked on in", "discussed in",
"for", "mentions", "noted", with "by you" on links you drew). Fold or
unfold branches (big threads start folded), or a whole thread. An item can
be linked to more than one thing, but it only appears once in the tree. Its
other links show as small link buttons that jump to the other item. To see a
thread from another item's point of view, select it and press **Start the
tree here**.

**Graph** is a mind map, like Obsidian's graph view. Every item is a dot,
coloured by kind and bigger the more it's linked, and every link is a line.
The layout arranges itself, so linked items pull together and each
thread forms its own island. Hover a dot to light up what it's linked to;
click it for its details. Drag dots around, drag the background to pan,
scroll to zoom (names appear as you zoom in), and press the fit button to
see everything. Links you drew are dashed purple.

**Columns** shows the same threads as bands with Jira, pull request and
email columns and lines between linked items (a dashed line joins an issue
and an email directly). Notes you add get their own column. Switch views
with **Tree / Graph / Columns** at the top. In any view, hover an item to highlight its links; click it for its
details, everything linked to it, **Open** (in GitHub or Jira) and **Ask
about this**. Filter by words, and tick **Unlinked** to see items that don't
mention anything else. Problems from a sync (a wrong token, a missing repo)
show in the status line; click it for details.

**Adding context by hand.** Not everything lives in GitHub, Jira or email.
Press **Add note** to add your own: meeting notes, a Slack thread, a
decision, a link to a document. A note that mentions `ABC-123` or a PR links
to it automatically, and you can tick items to link it to when you write it
(or press **+ Note** on a selected item). **Link to…** on any item draws a
link by hand between two items that belong together but don't mention each
other; links you drew show in purple and have a remove button. Notes and
your links are kept in `context_manual.json`, so a sync never touches them,
and notes work even with no sources set up.

**Asking the model.** Turn on **Context** in a chat's header, or press **Ask
about this** on an item. The model can list the threads, search everything,
and read any item with what it's linked to, so it can answer questions like
"what's the status of ABC-101 and what did people say about the fix?"

**At scale.** The page stays quick with thousands of items. It was tested
with 1000 pull requests, 1000 Jira issues, 1000 emails and 500 notes:

- A sync of all of that took under a second against a test server.
- Tree and Columns show 40 threads at a time; **Show more** adds 40 more,
  and the filter box finds everything.
- The Graph runs at 60 fps once laid out.
- A long item's text arrives in full when you open it.

Sync pages through GitHub and Jira. It reads up to 1000 pull requests per
repository (set "Pull requests per repository") and up to 1000 Jira
issues, fetching mentioned issues 50 at a time. With a lot of context, the
model gets a short overview and reads the details it needs.

Everything is **read-only**: nothing is changed in GitHub or Jira, and email
isn't marked as read. Each sync is saved in the app's data folder
(`context.json`), so the diagram works offline, and the model reads that
saved copy without going online. Tokens and passwords are kept in
`settings.json` (like server API keys) and never sent to the page.

## API

The **API** screen is a full API client, like Postman, built into the app.
Pick a **Project** at the top to keep its collections, environments and
history with that project, or leave it on *No project* for global ones.

**Requests.** Each request opens in its own tab (`Ctrl+T` new, `Ctrl+W`
close, `Ctrl+Enter` send, `Ctrl+S` save). A dot on a tab means unsaved
changes. Every request has:

- **Params**: query parameters, and path variables for URLs like
  `/users/:id`. Pasting a URL with `?a=1` fills in the table.
- **Auth**: inherit from the folder or collection, none, bearer token,
  basic, API key (header or query), Digest, **OAuth 2.0** (client
  credentials, password, or authorization code with PKCE, which signs in
  in your browser) and **AWS Signature v4**. OAuth tokens are kept,
  refreshed and reused automatically.
- **Headers** and **Body**: JSON, text, XML, `x-www-form-urlencoded`,
  `form-data` with files, a binary file, or **GraphQL** (query plus
  variables).
- **Scripts**: pre-request and test scripts using Postman's `pm.*` API
  (`pm.test`, `pm.expect`, `pm.response`, `pm.environment.set`,
  `pm.setNextRequest`…). There are snippets for the common cases. Scripts
  run in a sandbox with no network access. With no environment selected,
  `pm.environment` reads and writes globals so values still carry over.
- **Checks**: no-code tests: status, response time, a header, body text, a
  JSON field (`data[0].id` equals, exists, is a type, has a length…) or a
  **JSON schema**. *Checks from this response* writes them for you.
- **Settings**: per-request redirects, TLS checking, cookies and timeout.
- **Examples**: *Save example* on a response keeps it with the request, and
  it's exported with the collection.

The response shows the status, time and size, then the body (JSON is
highlighted; *Find in body* searches it), headers, cookies, test results,
the scripts' console output, and the request exactly as sent. **Ask AI**
opens a chat about it; the file button saves the body.

**Collections** hold folders and requests; drag them to reorder or move
them into a folder. A collection or folder can set auth and scripts that
everything inside inherits, and a collection has its own **variables**.
Variables are written `{{name}}` and resolve in this order, lowest first:
globals, collection, environment, data file row, values set by scripts.
**Variables** at the top edits environments (values can be marked secret,
and are masked in reports) and globals. The dynamic variables `{{$guid}}`,
`{{$timestamp}}`, `{{$isoTimestamp}}`, `{{$randomInt}}` and
`{{$randomEmail}}` work too.

**Running a collection** (or a folder): set iterations, a delay, a **CSV or
JSON data file** (one iteration per row, its columns become variables) and
*stop at the first failure*. Results stream in as each request finishes;
download the report as **HTML**, **JUnit XML** (for CI) or JSON. Runs in a
project are added to its **Tests** list. `pm.setNextRequest("name")` jumps
to another request and `pm.setNextRequest(null)` ends the run.

The same runs work from the command line, for CI:

```
python -m vscode_assistant.apirun my.postman_collection.json -e env.json \
    -d users.csv -n 3 --folder Users --bail --junit report.xml --html report.html
```

**Import** takes a curl command (also by pasting it into the URL box), a
Postman collection or environment (v2.1), or an **OpenAPI 3 / Swagger 2**
spec in JSON or YAML. A spec becomes a collection with a folder per tag,
example bodies, auth from its security schemes and a schema check on every
request. **Code** (`<>`) turns a request into curl, Python, JavaScript,
Java, C#, Go or PowerShell.

**Sharing with the team.** In a project, *Share with the team through Git*
(in a collection's menu) stores it in the project's `.api` folder in
Postman format, so it's committed with the code and anyone, in this app or
Postman, can open it. Environments and secrets stay on your computer.

**Cookies** that servers set are kept per project and sent back, like a
browser; **Cookies** at the top lists and deletes them. **Settings** has the
default timeout, redirects, TLS checking (turn it off for self-signed local
servers), a proxy and a client certificate for mutual TLS.

**WebSocket and SSE.** Choose *WebSocket* or *SSE* in the method list (or
**New**) to connect, send messages and watch what comes back, with custom
headers and auth.

**Your dev server and its endpoints.** In a project, the bar above the
collections starts and stops the dev server (its command and address are
detected; change them if needed) and shows its log. `{{baseUrl}}` falls
back to the server's address when nothing else sets it. **Endpoints** finds
the routes your code defines (Express, FastAPI, Flask, Django, Spring,
JAX-RS, ASP.NET, Go, Rails, Laravel and OpenAPI files), lets you try each
one, and **Compare with docs** lists endpoints missing from the
documentation and documented ones that no longer exist.

**The AI** can use all of this in a project chat: start the server, call
endpoints (anything other than GET asks first), run a collection and read
its report, and suggest tests. Reading from a local server (GET, HEAD, OPTIONS) runs
straight away; any other request, or one to another machine, asks first.

## Library

Open **Library** in the left rail. It has three tabs:

- **Documents**: notes, specs, policies, code, anything the AI should know
  about. Create them in the editor, or click **Import…** to add text files
  (`.txt`, `.md`, `.csv`, `.json`, source code…) or Word `.docx` files, which
  are converted to text.
- **Templates**: examples of how an answer should look. Write them with
  headings, tables and `{{placeholders}}`; the AI copies the layout and fills
  in the blanks. Three examples come with the app: meeting notes, a bug
  report and an email.
- **Doc standards**: your documentation templates, which the AI **must**
  follow whenever it writes documentation. See below.

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

### Doc standards

Upload your team's documentation templates (README, API reference, runbook,
design doc…) under **Library › Doc standards** with **Import…** (Markdown,
text or Word `.docx`), or write one with **New**. Each standard has:

- **The AI must follow this standard**: on or off.
- **Use it for**: the kind of documentation it covers, e.g. *API reference*.
- **Project files it applies to**: patterns such as `README.md` or
  `docs/**/*.md`.

Every chat is told to follow the standards: whenever the AI writes
documentation it uses the matching standard's sections, headings and order,
and keeps sections that don't apply instead of dropping them. In a
**project** it's checked as well. When the AI writes a file that matches a
standard's patterns, its headings are compared with the standard. A new file
or full rewrite that is missing a required section (or has them out of
order), or an edit that removes one, is refused, and the AI is told exactly
which headings to use. The approval dialog then shows **Follows the
documentation standard**. A `README standard` example comes with the app,
switched off.

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
  this chat, or Deny. Each step shows in the chat as a tool card, which you can
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

### Amazon Bedrock (Claude through your company's AWS account)

Choose **Amazon Bedrock (AWS)** as the type to use Claude (or any other
Bedrock model) everywhere a local model works: chats, Projects, the agent
browser and Context. Replies stream in, and tool calls work.

It signs in the same way the AWS CLI does, so nothing is pasted into the app:

- **With the AWS CLI and an SSO profile** (you run `aws sso login`): pick the
  profile under **AWS profile** (the list shows your profiles; an SSO one is
  picked for you) and set the **Region**, e.g. `us-east-1`. The app uses the
  sign-in the AWS CLI saved and renews it when it can. When it runs out,
  run `aws sso login --profile <name>` again, or press **Sign in**.
- **Without the AWS CLI**: open *No AWS CLI profile?* and fill in your SSO
  start URL (your AWS access portal's address), its region, the account ID
  and the role name (both shown on the portal). Save, then press **Sign in**:
  your browser opens AWS's sign-in page with a code to confirm, the same as
  `aws sso login`. The sign-in is kept in the app's data folder.
- A profile with `credential_process` or keys in `~/.aws/credentials`, and
  the `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` environment variables,
  work too.

**Test** shows who you're signed in as and how many models you can use.
Models are listed from Bedrock's inference profiles (newer Claude models
are called through one, e.g. `us.anthropic.claude-…`) and on-demand models,
with Claude first. If your role isn't allowed to list them, type the model
IDs into **Models**. Your role needs `bedrock:InvokeModel` and
`bedrock:InvokeModelWithResponseStream` on the model, and the model must be
enabled for the account (Bedrock › Model access); the error says which one
is missing.

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
- `Ctrl+B` collapses or expands the sidebar (in the code editor it goes to a
  definition instead)
- `Tab` / `Shift+Tab` indent and unindent in the Projects editor
- Java and TypeScript: `Alt+Enter` quick fix, `Ctrl+B` go to definition, `Alt+F7` find
  usages, `Ctrl+R` rename, `Ctrl+Shift+I` fix imports, `Ctrl+O` go to type,
  `Alt+Shift+F` format
- Debugger: `F9` breakpoint, `F5` continue, `F8` step over, `F7` step into,
  `Ctrl+F7` step out, `Shift+F5` stop
- `Ctrl+Enter` commits in the commit message box
- `Ctrl+Enter` sends, `Ctrl+S` saves, `Ctrl+T` opens and `Ctrl+W` closes a request tab in API

## Files

```
run.py / run.pyw                   start the command center (run_web.py / .pyw do the same)
vscode_assistant/server.py         local web server + JSON API; opens the app window
vscode_assistant/static/           the app's page, styles and scripts
vscode_assistant/static/vault.js   the encrypted password vault (Web Crypto, in the page)
vscode_assistant/static/projects.js  the Projects workspace (files, editor, Git, terminal)
vscode_assistant/static/codeeditor.js  the code editor (Monaco, or a plain text box if it can't load)
vscode_assistant/static/javaide.js the Java and TypeScript features, and the Java debugger, in the editor
vscode_assistant/static/vendor/monaco/  the Monaco editor (MIT licence, see its LICENSE and ThirdPartyNotices.txt)
vscode_assistant/javals.py         the Java tools: finds a JDK, downloads and runs the Eclipse Java language server
vscode_assistant/javadebug.py      the Java debugger (talks to the java-debug plugin)
vscode_assistant/tsls.py           TypeScript/JavaScript: typescript-language-server, ESLint, Angular, Prettier, test runners
vscode_assistant/engine.py         the chat + tool loop
vscode_assistant/projects.py       projects: file access, the AI's coding tools, commands, undo
vscode_assistant/context.py        work context: GitHub, Jira and IMAP sync, linking, the model's tools
vscode_assistant/static/contextview.js  the Context page
vscode_assistant/api.py            API collections, variables, auth, sending, import/export, code snippets
vscode_assistant/apirun.py         the collection runner, its reports, and the command-line runner
vscode_assistant/realtime.py       WebSocket and SSE connections for the API screen
vscode_assistant/tasks.py          Build / Run / Test buttons for Maven, Gradle, NetBeans Ant, npm and more
vscode_assistant/static/apiview.js the API screen
vscode_assistant/static/pmsandbox.js  the sandbox that runs pm.* scripts
vscode_assistant/testlog.py        the Tests list: test runs, their results, and suggested tests
vscode_assistant/gitops.py         Git commands (status, diff, commit, pull, push, branches)
vscode_assistant/providers.py      Ollama + OpenAI-compatible clients (streaming, tool calls)
vscode_assistant/bedrock.py        Amazon Bedrock: AWS sign-in (SSO, profiles), model list, Converse streaming
vscode_assistant/web.py            the sandbox browser (read-only web tools)
vscode_assistant/agent_browser.py  the agent browser: drives a Chrome window over DevTools
vscode_assistant/library.py        documents + templates, and the prompt text they add
vscode_assistant/net.py            urllib helpers
vscode_assistant/store.py          settings + chats on disk
vscode_assistant/static/icons.js   the app's line icons
vscode_assistant/assets/           app icon, logos, illustrations and the Inter font
```
