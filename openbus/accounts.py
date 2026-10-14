"""Agent accounts: one per provider config dir (CLAUDE_CONFIG_DIR or CODEX_HOME).

A profile is a provider plus a config dir. They are found in ~/.claude, ~/.claude-*,
~/.codex*, the dirs live panes use, and the ones added here, and named by the user's
label, else by the dir's non-secret account info. The store keeps only labels, colours
and forgotten dirs; nothing here reads, copies or links a login. See
docs/design/agent-accounts.md.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import plan_usage, tmux
from .expunge import _FILES, Refused, Session, _home, _registration, _stat
from .plan_usage import ENV
from .plan_usage import canonical as _canonical

COLORS = 6  # web/m/style.css draws --acct-0 .. --acct-5
# What a new account may share with the provider's default dir, one symlink each, and
# whether the add flow ticks it: settings often carry work-only hooks, MCP servers or
# permissions. The login (.credentials.json, auth.json), .claude.json and the transcripts
# are never offered.
SHARED = {"claude": {"CLAUDE.md": True, "skills": True, "hooks": True, "statusline.sh": True,
                     "settings.json": False},
          "codex": {"AGENTS.md": True, "prompts": True, "config.toml": False}}
# The provider's own sign-in. Plain `codex login` redirects the browser to a callback on
# this machine's localhost, which a remote browser cannot reach: the sheet takes the address
# it landed on and replays it here (codex_callback). `--device-auth` avoids that, where the
# workspace allows device codes.
LOGIN = {"claude": ["claude"], "codex": ["codex", "login"]}
CODEX_CALLBACK_PORT = 1455  # where `codex login` waits for the browser's redirect
RESUME = {"claude": ["claude", "--resume"], "codex": ["codex", "resume"]}
# What a session needs to resume on another account: of the files Expunge knows it by,
# Claude's transcript, its subagents' and tool results' folder and its /rewind snapshots;
# Codex's rollouts. The rest is per process or rebuilt when the agent starts.
MOVED = {"claude": _FILES["claude"][:3], "codex": _FILES["codex"][:4]}
MORE = {"claude": ".claude-*", "codex": ".codex?*"}  # each provider's other config dirs in HOME
# One store transaction at a time: the PlanUsage thread and the API's worker threads all
# read-modify-write it. Re-entrant, since add() and edit() look profiles up inside theirs.
_LOCK = threading.RLock()


def store_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return state / "tmux-rc" / "accounts.json"


def _load() -> dict:
    try:
        data = json.loads(store_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def default_dir(provider: str) -> Path:
    return Path.home() / f".{provider}"


def _tilde(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def identity(provider: str, config: Path) -> dict | None:
    """Who is signed in to this dir, from non-secret files only; None when nobody is.
    Codex keeps no such file, so only its login's presence is checked, never read."""
    if provider == "codex":
        signed_in = (config / "auth.json").exists() or _codex_login(config)
        return {"who": "", "plan": ""} if signed_in else None
    account = plan_usage.oauth_account(config, plan_usage.env_for("claude", config))
    if not account.get("accountUuid"):
        return None
    # A personal plan's organisation is named for its owner's email: show just the email.
    email, org = account.get("emailAddress") or "", account.get("organizationName") or ""
    org = email if email and org in (f"{email}'s Organization", f"{email}'s Organisation") else org
    plan = (account.get("organizationType") or "").removeprefix("claude_").replace("_", " ")
    return {"who": org or account.get("displayName") or email, "plan": plan.title(),
            # Tell apart two people of one organisation: the display name, else the email's
            # local part; the full email only if those clash too (see _distinguish).
            "_more": [account.get("displayName") or email.split("@")[0], email],
            # The login itself: one person in one organisation, whatever either is called;
            # without the organisation's id, no key, since a name may be shared.
            "_key": account.get("organizationUuid") and [account["accountUuid"],
                                                          account["organizationUuid"]]}


def _codex_login(config: Path) -> bool:
    """Whether Codex itself says this dir is signed in. Its login may live in the OS keyring
    (cli_auth_credentials_store), where no file shows it; `codex login status` sees both."""
    env = {k: v for k, v in os.environ.items() if k != ENV["codex"]}
    env |= plan_usage.env_for("codex", config)
    env["PATH"] = tmux.server_path() or env.get("PATH", "")
    try:
        return subprocess.run(["codex", "login", "status"], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=5, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def codex_callback(url: str) -> str | None:
    """The path and query to replay here from the address a remote browser landed on after
    `codex login`, or None unless it is exactly that callback: http on localhost or
    127.0.0.1, Codex's port, /auth/callback, a printable query, no credentials or fragment.
    Nothing else may be requested, so this is no proxy."""
    try:
        u = urlsplit(url.strip())
        ok = (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")
              and u.port == CODEX_CALLBACK_PORT and u.path == "/auth/callback"
              and re.fullmatch(r"[\x21-\x7e]+", u.query) and not (u.username or u.password)
              and "#" not in url)
    except ValueError:  # a port that is no number
        return None
    return f"{u.path}?{u.query}" if ok else None


def sign_in_link(screen: str) -> dict:
    """The sign-in page a Codex login window shows (the last on screen, whole: tmux joins
    wrapped lines), and the one-time code the device flow shows beside it; "" for each it
    has not shown yet."""
    urls = re.findall(r"https://auth\.openai\.com/[^\s\x00-\x1f]+", screen)
    code = re.findall(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,5}\b", screen)
    return {"url": urls[-1] if urls else "", "code": code[-1] if code else ""}


def oauth_state(url: str) -> str:
    """The OAuth `state` an authorize link or a callback address carries ("" for none)."""
    return parse_qs(urlsplit(url).query).get("state", [""])[0]


def _get_local(path: str) -> tuple[int, str, str]:
    """GET a path from the `codex login` listening here: status, body start, Location."""
    conn = http.client.HTTPConnection("127.0.0.1", CODEX_CALLBACK_PORT, timeout=15)
    try:
        conn.request("GET", path)
        reply = conn.getresponse()
        text = reply.read(500).decode(errors="replace")
        return reply.status, text, reply.getheader("Location") or ""
    finally:
        conn.close()


def replay_codex_callback(path: str) -> tuple[int, str] | None:
    """GET a codex_callback path from the `codex login` listening on this machine, as the
    browser would have: its status and the start of its body, or None when nothing is
    listening. Codex answers a good code with a redirect to its own success page, and only
    exits once that page is loaded too, so a redirect to the same server (and only that) is
    followed once; any other host is left alone."""
    try:
        status, text, location = _get_local(path)
        u = urlsplit(location)
        same = (not u.netloc or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")
                                 and u.port == CODEX_CALLBACK_PORT))
        if 300 <= status < 400 and same and u.path.startswith("/") and not u.path.startswith("//"):
            with contextlib.suppress(OSError, http.client.HTTPException):
                _get_local(u.path + (f"?{u.query}" if u.query else ""))
        return status, text
    except (OSError, http.client.HTTPException):  # nothing there, or no HTTP spoken
        return None


def profiles(pane_dirs=()) -> list[dict]:
    """Every account tmux-rc lists: signed in or added here, and not forgotten. A dir seen
    for the first time gets the lowest colour no other has, kept in the store."""
    with _LOCK:
        return _profiles(pane_dirs)


def _profiles(pane_dirs) -> list[dict]:
    store, home = _load(), Path.home()
    found = {str(d): t for t in ENV for d in [default_dir(t), *sorted(home.glob(MORE[t]))]
             if d.is_dir()}
    for tool, config in pane_dirs:  # what runs there says the provider, not the dir's name
        found[_canonical(str(config))] = tool
    for config, entry in store.items():  # an added dir's provider was chosen, not guessed
        if entry.get("provider") in ENV:
            found[config] = entry["provider"]
    used, dirty, out = {e.get("color") for e in store.values()}, False, []
    for config, provider in found.items():
        entry = store.get(config, {})
        if entry.get("hidden"):  # before identity(): a forgotten dir costs no codex call
            continue
        if not ((ident := identity(provider, Path(config))) or config in store):
            continue
        if "color" not in entry:
            free = sorted(set(range(COLORS)) - used) or [len(used) % COLORS]
            entry = store[config] = {**entry, "provider": provider, "color": free[0]}
            used.add(free[0])
            dirty = True
        name = entry.get("label") or (ident or {}).get("who") or (
            provider.title() if Path(config) == default_dir(provider)
            else Path(config).name.lstrip("."))
        out.append({"dir": config, "tilde": _tilde(config), "provider": provider, "name": name,
                    "label": entry.get("label"), "color": entry["color"], "panes": [],
                    "signed_in": bool(ident), "default": Path(config) == default_dir(provider),
                    **(ident or {"who": "", "plan": ""})})
    if dirty:
        _save(store)
    _distinguish([p for p in out if not p["label"]])
    for p in out:  # dirs signed in to one login (a stray /login): flag each, by dir
        p["same_as"] = [q["dir"] for q in out if q is not p and p.get("_key")
                        and q.get("_key") == p["_key"]]
    for p in out:  # the login's email, which the UI shows to tell each dir's login apart
        p["login"] = p.pop("_more", ["", ""])[1]
        p.pop("_key", None)
    return out


def _distinguish(unlabelled: list[dict]) -> None:
    """Unlabelled profiles that read the same ("QSI" twice) become "QSI · sam" and
    "QSI · alex", or carry the full email if those clash too. A label always wins."""
    for i in range(2):
        names = [p["name"] for p in unlabelled]
        for p in unlabelled:
            if names.count(p["name"]) > 1 and p.get("_more", ["", ""])[i]:
                p["name"] = f"{p['who']} · {p['_more'][i]}"


def find(config: str, provider: str | None = None) -> dict | None:
    """The listed profile for this config dir (and provider, if given), or None."""
    path = _canonical(config)
    return next((p for p in profiles() if p["dir"] == path
                 and provider in (None, p["provider"])), None)


def metered() -> list[tuple[str, Path]]:
    """The signed-in profiles' dirs, for PlanUsage to meter with or without a pane."""
    return [(p["provider"], Path(p["dir"])) for p in profiles() if p["signed_in"]]


def command(provider: str, config: str, cmd: str | list[str]) -> list[str]:
    """`cmd` on this account whatever the session's default: the provider's variable set
    to the dir, or unset for the default dir. A string runs through sh, as tmux would."""
    var = ENV[provider]
    pin = ["-u", var] if Path(config) == default_dir(provider) else [f"{var}={config}"]
    return ["env", *pin, *(cmd if isinstance(cmd, list) else ["sh", "-c", cmd])]


def session_defaults(sessions) -> dict[str, dict[str, str | None]]:
    """Each session's account per provider: the dir a new window there inherits from tmux,
    the session's environment over the global one; None when nothing sets it (unset, or
    removed with -r), so it falls back to the provider's default dir. Under "": a new
    session's, from the global environment alone."""
    glob = tmux.show_env()
    out = {}
    for session in ["", *sessions]:
        env = {**glob, **(tmux.show_env(session) if session else {})}
        out[session] = {t: _canonical(v) if (v := env.get(ENV[t])) else None for t in ENV}
    return out


def report(states: list[dict], birth) -> dict:
    """GET /api/accounts: the profiles with the panes on each, and, once a provider has
    several (the only time the UI asks), each session's default account."""
    # A pane on another HOME's default dir is outside this user's accounts: it could be
    # neither read (its .claude.json sits beside that dir) nor launched on as one.
    mine = str(Path.home())
    panes = {pane_id: (tool, plan_usage.home(tool, env))
             for tool, env, pane_id in plan_usage.pane_envs(states, birth)
             if env.get(ENV[tool]) or _canonical(env.get("HOME") or mine) == _canonical(mine)}
    listed = profiles(panes.values())
    by_dir = {p["dir"]: p for p in listed}
    for pane_id, (_, config) in panes.items():
        if profile := by_dir.get(_canonical(str(config))):
            profile["panes"].append(pane_id)
    several = any(sum(p["provider"] == t and p["signed_in"] for p in listed) > 1 for t in ENV)
    sessions = sorted({s["session"] for s in states if s.get("session")})
    return {"profiles": listed, "sessions": session_defaults(sessions) if several else {},
            "shared": SHARED}


def add(provider: str, config: str, label: str, link: list[str]) -> dict:
    """Create (mode 700) or reuse the dir, symlink the chosen shared files from the
    provider's default dir, and list it. Never deletes or overwrites anything."""
    with _LOCK:
        return _add(provider, config, label, link)


def _add(provider: str, config: str, label: str, link: list[str]) -> dict:
    path = Path(_canonical(config))
    if not path.is_absolute():
        raise ValueError("the config dir must be an absolute path")
    if (listed := find(str(path))) and listed["signed_in"]:  # a sign-in never finished: resumed
        raise FileExistsError(f"{_tilde(str(path))} is already an account")
    if bad := set(link) - set(SHARED[provider]):
        raise ValueError(f"not shareable: {', '.join(sorted(bad))}")
    existed = path.exists()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    linked = []
    for name in link:
        src, dst = default_dir(provider) / name, path / name
        if src.exists() and not (dst.exists() or dst.is_symlink()):
            dst.symlink_to(src)
            linked.append(name)
    store = _load()
    entry = {k: v for k, v in store.get(str(path), {}).items() if k != "hidden"}
    store[str(path)] = {**entry, "provider": provider, "label": label.strip() or None}
    _save(store)
    return {"dir": str(path), "existed": existed, "linked": linked}


def edit(config: str, label: str | None = None, *, forget: bool = False) -> None:
    """Rename (the label only) or forget a profile. Forgetting only drops it from the
    list (and from sessions' defaults): the dir, its login and its transcripts stay on
    disk."""
    with _LOCK:
        _edit(config, label, forget=forget)


def _edit(config: str, label: str | None, *, forget: bool) -> None:
    profile = find(config)
    if profile is None:
        raise LookupError("unknown account")
    if forget and profile["default"]:
        raise ValueError("the default account can't be removed")
    store = _load()
    entry = store.setdefault(profile["dir"], {"provider": profile["provider"]})
    if forget:
        entry["hidden"] = True
        # A session defaulting to it would keep starting panes there, unbadged once the
        # picker goes: such sessions fall back to the provider's default dir.
        try:
            sessions = {p.session for p in tmux.list_panes()}
        except subprocess.CalledProcessError:  # no tmux: no session defaults either
            sessions = set()
        for session, chosen in session_defaults(sessions).items():  # "": the global env
            if chosen[profile["provider"]] == profile["dir"]:
                with contextlib.suppress(subprocess.CalledProcessError):  # gone meanwhile
                    tmux.set_env(ENV[profile["provider"]], None, session=session or None)
    if label is not None:
        entry["label"] = label.strip() or None
    _save(store)


def set_default(session: str, profile: dict) -> None:
    """Make this profile the session's default: `tmux set-environment`, as by hand, so
    every window created there afterwards (prefix c included) starts on it."""
    tmux.set_env(ENV[profile["provider"]], None if profile["default"] else profile["dir"],
                 session=session)


def files(s: Session, root: Path) -> list[Path]:
    """The session's files (MOVED) under one account's dir."""
    return [p for g in MOVED[s.harness] for p in root.glob(g.format(id=s.session_id))]


def _tree(paths: list[Path]):
    """Each path and everything under it."""
    for p in paths:
        yield p
        yield from p.rglob("*")


def _has_record(log: Path) -> bool:
    """Whether the log holds one whole line yet, read a chunk at a time until it does."""
    with log.open("rb") as f:
        return any(b"\n" in chunk for chunk in iter(lambda: f.read(1 << 16), b""))


def resumable(paths: list[Path]) -> bool:
    """Whether a session's files (from files()) include its transcript with at least one
    whole record in it, which a resume needs."""
    return any(p.suffix == ".jsonl" and p.is_file() and _has_record(p) for p in paths)


def confined(paths: list[Path], target: Path) -> list[Path]:
    """`paths`, refused if any, or anything under one, resolves out of the account's dir: a
    symlinked projects/ (say) would carry a write, a delete or a resume to another
    account's files."""
    dst = target.resolve()
    if any(not p.resolve().is_relative_to(dst) for p in _tree(paths)):
        raise ValueError("a path in that account's dir links outside it")
    return paths


def copied_at(s: Session, target: Path) -> float | None:
    """When the account's copy of the session was made (its files' latest ctime: a copy
    keeps the original's mtime), or None if it has none."""
    return max((p.lstat().st_ctime for p in files(s, target)), default=None)


def running(s: Session, target: Path, pids: list[str] | None = None) -> bool:
    """Whether an agent other than `s` runs this session on `target`, or may: by any of three
    signs: Claude's registration of it there (a live pid with the start time it recorded,
    or a registration that can't be read, which could be it); a
    process holding one of its files there open (Codex's app-server keeps the rollout open);
    or, before either exists, the session id on a command line, as a move's resume has.
    `pids`: look only at these processes (a move's own window), not every one."""
    for path in target.glob("sessions/*.json"):
        try:
            reg = _registration(path)
        except Refused:
            return True
        if (reg.get("sessionId") == s.session_id and reg["pid"] != s.pid
                and _stat(reg["pid"])[19:20] == [reg["procStart"]]):
            return True
    dst, held = target.resolve(), {str(p.resolve()) for p in _tree(files(s, target))}
    for pid in filter(str.isdigit, os.listdir("/proc") if pids is None else pids):
        if int(pid) == s.pid:
            continue
        if s.session_id in tmux.proc_read(pid, "cmdline").split("\0"):
            with contextlib.suppress(Refused):
                if _home(int(pid), ENV[s.harness], f".{s.harness}").resolve() == dst:
                    return True
        with contextlib.suppress(OSError):
            fds = Path(f"/proc/{pid}/fd")
            if any(os.readlink(fds / fd) in held for fd in os.listdir(fds)):
                return True
    return False


def _whole_records(log: Path) -> None:
    """Cut a record the running agent was still appending when the log was copied, so the
    copy ends on a whole JSON line. The agent appends, so everything before it is whole.
    Read backwards a chunk at a time: a Codex rollout can run to hundreds of MB."""
    with log.open("r+b") as f:
        pos = f.seek(0, os.SEEK_END)
        cut = 0
        while pos:
            n = min(pos, 1 << 16)
            pos -= n
            f.seek(pos)
            if (i := f.read(n).rfind(b"\n")) >= 0:
                cut = pos + i + 1
                break
        f.truncate(cut)


def copy_session(s: Session, target: Path, *, replace: bool = False) -> list[str]:
    """Copy the session's files into another account's dir, at the same relative paths, so
    the agent there resumes it; the original stays. Refuses, copying nothing, when there
    is no transcript yet, when either dir holds the other or a folder on the way links out
    of the target, or when the target already has the session, unless `replace`: then that
    session's files there, and nothing else, are deleted first. Undoes a copy that fails
    partway. The caller runs one at a time (server._MOVES), so two moves can never both
    pass the check and then overwrite, or roll back, each other's copy."""
    src, dst = s.root.resolve(), target.resolve()
    if src.is_relative_to(dst) or dst.is_relative_to(src) or src.samefile(dst):  # a bind mount
        raise ValueError("one account's dir is inside the other's")
    paths, old = files(s, s.root), files(s, target)
    if not resumable(paths):  # a session only registered so far
        raise LookupError("this session has no transcript to resume yet")
    confined([*old, *(target / p.relative_to(s.root) for p in paths)], target)
    if any(p.is_symlink() for p in _tree(paths)):
        raise ValueError("the session's files include a link, which could lead out of the account")
    if old and not replace:
        raise FileExistsError("that account already has this session")
    for p in old:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
    made: list[Path] = []
    try:
        for src in paths:
            dst = target / src.relative_to(s.root)
            dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            made.append(dst)
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
            for log in [dst] if dst.suffix == ".jsonl" else dst.rglob("*.jsonl"):
                _whole_records(log)
    except OSError:
        for dst in made:
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink(missing_ok=True)
        raise
    return [p.name for p in paths]
