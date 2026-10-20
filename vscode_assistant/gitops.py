"""Git for a project folder, by running the git program installed on this computer.

Signing in to GitHub (or any remote) is left to git itself: SSH keys, Git
Credential Manager on Windows, the macOS keychain, a credential helper. The
app never sees or stores those credentials. Git is told never to stop and ask
for a password in a console (there is none), so a missing login fails with a
message instead of hanging.

Pull only fast-forwards unless the user picks merge or rebase. Push never
forces.
"""
import os
import shutil
import subprocess
import sys

TIMEOUT = 120
NET_TIMEOUT = 600


class GitError(Exception):
    pass


def git_path():
    return shutil.which("git")


def _env():
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"     # fail instead of waiting for a console password prompt
    env["GIT_EDITOR"] = "true"           # never open an editor (continue a merge/rebase as is)
    env["GIT_MERGE_AUTOEDIT"] = "no"
    env["LANGUAGE"] = "en"               # English messages, so friendly() can recognise them
    return env


def _flags():
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def run(root, args, timeout=TIMEOUT, check=True, input_text=None):
    """Run `git args` in root. Returns stdout; raises GitError with git's message on failure."""
    exe = git_path()
    if not exe:
        raise GitError("Git isn't installed (or isn't on PATH). Install it from https://git-scm.com and restart the app.")
    try:
        p = subprocess.run([exe] + list(args), cwd=root, env=_env(), capture_output=True, timeout=timeout,
                           input=input_text.encode() if input_text is not None else None,
                           stdin=None if input_text is not None else subprocess.DEVNULL,
                           creationflags=_flags())
    except subprocess.TimeoutExpired:
        raise GitError("git %s took too long and was stopped." % args[0])
    out = p.stdout.decode("utf-8", "replace")
    if check and p.returncode != 0:
        err = (p.stderr.decode("utf-8", "replace") or out).strip()
        raise GitError(friendly(err) or "git %s failed." % args[0])
    return out


def friendly(err):
    """Git's error, with a plain-language line in front for the common cases."""
    low = err.lower()
    hint = ""
    if "terminal prompts disabled" in low or "could not read username" in low or "authentication failed" in low \
            or "permission denied (publickey)" in low:
        hint = ("Git couldn't sign in to the remote. Set up a login that works without typing a password: "
                "Git Credential Manager (comes with Git for Windows), an SSH key, or `gh auth login`. "
                "If `git push` works in your own terminal, it works here too. (GitHub also says this when the "
                "repository doesn't exist or your account can't see it.)")
    elif "not possible to fast-forward" in low or "diverging branches" in low or "have diverged" in low:
        hint = "Your branch and the remote both have new commits. Choose Merge or Rebase to combine them."
    elif "would be overwritten" in low:
        hint = "You have uncommitted changes in files the pull would change. Commit or discard them first."
    elif "[rejected]" in low and ("fetch first" in low or "non-fast-forward" in low):
        hint = "The remote has commits you don't have yet. Pull first, then push."
    elif "no upstream" in low or "has no upstream branch" in low:
        hint = "This branch isn't on the remote yet."
    elif "not a git repository" in low:
        hint = "This folder isn't a Git repository."
    elif "could not resolve host" in low or "unable to access" in low:
        hint = "Couldn't reach the remote. Check your internet connection or proxy."
    return (hint + "\n\n" + err).strip() if hint else err


def is_repo(root):
    try:
        return run(root, ["rev-parse", "--is-inside-work-tree"], timeout=15).strip() == "true"
    except GitError:
        return False


def toplevel(root):
    return os.path.normpath(run(root, ["rev-parse", "--show-toplevel"], timeout=15).strip())


def _state(root):
    try:
        gd = run(root, ["rev-parse", "--git-dir"], timeout=15).strip()
    except GitError:
        return ""
    gd = gd if os.path.isabs(gd) else os.path.join(root, gd)
    if os.path.isdir(os.path.join(gd, "rebase-merge")) or os.path.isdir(os.path.join(gd, "rebase-apply")):
        return "rebase"
    if os.path.exists(os.path.join(gd, "MERGE_HEAD")):
        return "merge"
    return ""


CODES = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied", "T": "type changed",
         "U": "conflict", "?": "new"}


def status(root):
    """Branch, upstream, ahead/behind and changed files."""
    out = run(root, ["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"], timeout=60)
    info = {"is_repo": True, "branch": "", "upstream": "", "ahead": 0, "behind": 0, "files": [], "state": _state(root),
            "detached": False}
    fields = out.split("\0")
    i = 0
    while i < len(fields):
        f = fields[i]
        i += 1
        if not f:
            continue
        if f.startswith("# branch.head "):
            info["branch"] = f[len("# branch.head "):]
            info["detached"] = info["branch"] == "(detached)"
        elif f.startswith("# branch.upstream "):
            info["upstream"] = f[len("# branch.upstream "):]
        elif f.startswith("# branch.ab "):
            a, b = f[len("# branch.ab "):].split()
            info["ahead"], info["behind"] = int(a.lstrip("+")), int(b.lstrip("-"))
        elif f[0] in "12u":
            parts = f.split(" ")
            xy = parts[1]
            n = {"1": 8, "2": 9, "u": 10}[f[0]]
            path = " ".join(parts[n:])
            orig = ""
            if f[0] == "2":
                orig = fields[i]
                i += 1
            if f[0] == "u":
                code = "U"
            else:
                code = next((c for c in (xy[1], xy[0]) if c != "."), "M")
            info["files"].append({"path": path, "code": code, "status": CODES.get(code, code), "orig": orig,
                                  "staged": xy[0] not in ".?", "conflict": f[0] == "u"})
        elif f.startswith("? "):
            info["files"].append({"path": f[2:], "code": "?", "status": "new", "orig": "", "staged": False,
                                  "conflict": False})
    info["files"].sort(key=lambda x: x["path"].lower())
    return info


def branches(root):
    out = run(root, ["branch", "--format=%(refname:short)"], timeout=30)
    return [b.strip() for b in out.splitlines() if b.strip() and not b.startswith("(")]


def remotes(root):
    return [r.strip() for r in run(root, ["remote"], timeout=15).splitlines() if r.strip()]


def log(root, n=30, rng=None):
    args = ["log", "-n", str(int(n)), "--format=%h%x1f%an%x1f%ar%x1f%s"]
    if rng:
        args.append(rng)
    try:
        out = run(root, args, timeout=30)
    except GitError as e:
        if "does not have any commits" in str(e) or "unknown revision" in str(e):
            return []
        raise
    rows = []
    for line in out.splitlines():
        p = line.split("\x1f")
        if len(p) == 4:
            rows.append({"hash": p[0], "author": p[1], "when": p[2], "subject": p[3]})
    return rows


def diff(root, path=None, staged=None, untracked_text=None):
    """Unified diff of uncommitted changes (both staged and not) against HEAD, for one file or all."""
    spec = ["--", path] if path else []
    if untracked_text is not None:
        import difflib
        lines = untracked_text.splitlines(True)
        return "".join(difflib.unified_diff([], lines, "/dev/null", "b/" + path))
    has_head = True
    try:
        run(root, ["rev-parse", "--verify", "HEAD"], timeout=15)
    except GitError:
        has_head = False
    if has_head:
        return run(root, ["diff", "HEAD", "--no-color", "--no-ext-diff"] + spec, timeout=60)
    return run(root, ["diff", "--cached", "--no-color", "--no-ext-diff"] + spec, timeout=60)


def commit(root, message, paths=None):
    message = (message or "").strip()
    if not message:
        raise GitError("Write a commit message first.")
    if paths:
        run(root, ["add", "-A", "--"] + list(paths))
        st = status(root)
        if st["state"] == "merge":    # finishing a merge must commit everything
            run(root, ["commit", "-m", message])
        else:
            run(root, ["commit", "-m", message, "--"] + list(paths))
    else:
        run(root, ["add", "-A"])
        run(root, ["commit", "-m", message])
    return log(root, 1)[0]


def fetch(root):
    return run(root, ["fetch", "--prune"], timeout=NET_TIMEOUT)


def pull(root, mode="ff"):
    """mode: "ff" (fast-forward only, the default), "merge" or "rebase"."""
    st = status(root)
    if st["state"]:
        raise GitError("A %s is in progress. Finish or abort it first." % st["state"])
    if not st["upstream"]:
        raise GitError("This branch has no remote branch to pull from. Push it first.")
    tracked = [f for f in st["files"] if f["code"] != "?"]
    if tracked:
        raise GitError("You have uncommitted changes (%s%s). Commit or discard them before pulling, so nothing "
                       "gets overwritten." % (", ".join(f["path"] for f in tracked[:5]),
                                              "…" if len(tracked) > 5 else ""))
    arg = {"ff": "--ff-only", "merge": "--no-rebase", "rebase": "--rebase"}[mode]
    try:
        return run(root, ["pull", arg, "--no-edit"] if mode == "merge" else ["pull", arg], timeout=NET_TIMEOUT)
    except GitError as e:
        if _state(root):
            raise GitError("Some files conflict. They're marked in the Changes list: fix them (or ask the AI to), "
                           "then Continue, or Abort to undo the pull.\n\n" + str(e))
        raise


def push_preview(root):
    """What a push would send: branch, remote and the commits."""
    st = status(root)
    if st["detached"]:
        raise GitError("You're not on a branch (detached HEAD). Switch to a branch before pushing.")
    rems = remotes(root)
    if not rems:
        raise GitError("This repository has no remote. Add one first (for example with the Terminal: "
                       "git remote add origin https://github.com/you/repo.git).")
    branch = st["branch"]
    if st["upstream"]:
        remote = st["upstream"].split("/", 1)[0]
        commits = log(root, 50, "@{u}..HEAD")
        new_branch = False
    else:
        remote = "origin" if "origin" in rems else rems[0]
        commits = log(root, 50, "HEAD") if not _any_remote_refs(root) else _unpushed(root)
        new_branch = True
    return {"branch": branch, "remote": remote, "upstream": st["upstream"], "commits": commits,
            "new_branch": new_branch, "behind": st["behind"], "ahead": st["ahead"]}


def _any_remote_refs(root):
    return bool(run(root, ["for-each-ref", "--count=1", "refs/remotes"], timeout=15).strip())


def _unpushed(root):
    out = run(root, ["log", "-n", "50", "--format=%h%x1f%an%x1f%ar%x1f%s", "HEAD", "--not", "--remotes"], timeout=30)
    return [dict(zip(("hash", "author", "when", "subject"), l.split("\x1f"))) for l in out.splitlines()
            if l.count("\x1f") == 3]


def push(root):
    pv = push_preview(root)
    if pv["behind"]:
        raise GitError("The remote has %d commit%s you don't have yet. Pull first, then push."
                       % (pv["behind"], "" if pv["behind"] == 1 else "s"))
    if pv["new_branch"]:
        run(root, ["push", "-u", pv["remote"], "HEAD:refs/heads/" + pv["branch"]], timeout=NET_TIMEOUT)
        run(root, ["branch", "--set-upstream-to=%s/%s" % (pv["remote"], pv["branch"])], check=False)
    else:
        run(root, ["push", pv["remote"], "HEAD:" + pv["upstream"].split("/", 1)[1]], timeout=NET_TIMEOUT)
    return pv


def switch(root, name, create=False):
    name = (name or "").strip()
    if not name or name.startswith("-") or any(c in name for c in " ~^:?*[\\") or ".." in name:
        raise GitError("That isn't a valid branch name.")
    run(root, ["switch", "-c", name] if create else ["switch", name])


def abort(root):
    st = _state(root)
    if st == "merge":
        run(root, ["merge", "--abort"])
    elif st == "rebase":
        run(root, ["rebase", "--abort"])
    else:
        raise GitError("There's no merge or rebase to abort.")


def finish(root):
    """Continue a merge or rebase once the conflicts are fixed."""
    st = status(root)
    left = [f["path"] for f in st["files"] if f["conflict"]]
    if left:
        # conflict markers still in a file mean it isn't fixed yet
        for p in list(left):
            try:
                with open(os.path.join(root, p), encoding="utf-8", errors="replace") as fh:
                    if "<<<<<<< " in fh.read():
                        raise GitError("%s still has conflict markers (<<<<<<<). Fix it first." % p)
            except OSError:
                pass
        run(root, ["add", "-A", "--"] + left)
    if st["state"] == "merge":
        run(root, ["commit", "--no-edit"])
    elif st["state"] == "rebase":
        run(root, ["-c", "core.editor=true", "rebase", "--continue"])
    else:
        raise GitError("There's no merge or rebase in progress.")


def discard(root, path):
    """Throw away uncommitted changes to one file (a new file is deleted)."""
    st = status(root)
    f = next((x for x in st["files"] if x["path"] == path), None)
    if not f:
        return
    if f["code"] == "?":
        full = os.path.join(root, path)
        if os.path.isfile(full):
            os.remove(full)
        return
    if f["code"] == "A":
        run(root, ["rm", "-f", "--cached", "--", path])
        full = os.path.join(root, path)
        if os.path.isfile(full):
            os.remove(full)
        return
    run(root, ["restore", "--staged", "--worktree", "--source=HEAD", "--", path])
    if f.get("orig"):
        run(root, ["restore", "--staged", "--worktree", "--source=HEAD", "--", f["orig"]], check=False)


def init(root):
    run(root, ["init"])


def clone(url, dest):
    url = (url or "").strip()
    if not url or url.startswith("-"):
        raise GitError("Paste the repository's address, e.g. https://github.com/you/project.git")
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    run(parent, ["clone", "--", url, dest], timeout=NET_TIMEOUT)


def ls_files(root, skip_untracked_dirs=()):
    """Tracked plus untracked-but-not-ignored files (git applies .gitignore). Untracked files inside
    folders named in skip_untracked_dirs (caches, virtualenvs…) are left out too."""
    tracked = run(root, ["ls-files", "--cached", "-z"], timeout=60).split("\0")
    others = run(root, ["ls-files", "--others", "--exclude-standard", "-z"], timeout=60).split("\0")
    skip = set(skip_untracked_dirs)
    seen, files = set(), []
    for p in tracked + [o for o in others if not skip.intersection(o.split("/")[:-1])]:
        if p and p not in seen:
            seen.add(p)
            files.append(p)
    return sorted(files, key=str.lower)
