package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
)

// Running is a live harness process attached to a session.
type Running struct {
	PID      int    `json:"pid"`
	TmuxPane string `json:"tmux_pane,omitempty"` // e.g. "%48"; empty when not in tmux
	Status   string `json:"status,omitempty"`    // the harness's own word: idle, busy, ...
}

// LiveSessions merges every harness's live sessions, with the errors of any harness
// that can't tell: only that harness's sessions are unknown.
func LiveSessions() (map[string]Running, map[string]error) {
	out, errs := map[string]Running{}, map[string]error{}
	for _, h := range harnesses {
		running, err := h.running()
		if err != nil {
			errs[h.name] = err
			report(fmt.Errorf("%s liveness: %w", h.name, err))
		}
		maps.Copy(out, running)
	}
	return out, errs
}

// RunningClaude lists live Claude Code sessions by session ID. Claude Code registers
// each running session in <config>/sessions/<pid>.json with its tmux pane. A file
// outlives a crashed process, and pids get reused, so an entry counts only while
// its pid is alive with the same kernel start time it registered. An error means
// liveness is unknown, which callers must not read as "nothing is running".
func RunningClaude() (map[string]Running, error) {
	out := map[string]Running{}
	files, err := find(filepath.Join(claudeDir(), "sessions"), ".json", 1)
	if err != nil {
		return nil, err
	}
	for _, f := range files {
		var reg struct {
			PID       int    `json:"pid"`
			SessionID string `json:"sessionId"`
			ProcStart string `json:"procStart"`
			Tmux      string `json:"tmux"` // "<session>:@<window>.%<pane>"
			Status    string `json:"status"`
		}
		data, err := os.ReadFile(f)
		if errors.Is(err, os.ErrNotExist) {
			continue // the process exited and removed it between listing and reading
		}
		if err != nil {
			return nil, err
		}
		// A registration we can't read (half-written, or a format change) could be a
		// live session; say so rather than drop it.
		if err := json.Unmarshal(data, &reg); err != nil || reg.SessionID == "" || reg.ProcStart == "" || reg.PID <= 0 {
			return nil, fmt.Errorf("%s: unreadable registration", f)
		}
		start, err := procStat(reg.PID, 22)
		if err != nil {
			return nil, err
		}
		if start != reg.ProcStart {
			continue // a verified mismatch: that process is gone
		}
		pane := ""
		if i := strings.LastIndex(reg.Tmux, ".%"); i >= 0 {
			pane = reg.Tmux[i+1:]
		}
		out[reg.SessionID] = Running{PID: reg.PID, TmuxPane: pane, Status: reg.Status}
	}
	return out, nil
}

// procStat is one numbered field of /proc/<pid>/stat (22 is the start time in clock
// ticks, 7 the controlling terminal), or "" if there is no such process. Any other
// failure is an error: it proves nothing about whether the process is alive. The
// command name (field 2) may contain spaces and parentheses, so fields are counted
// from its closing parenthesis.
func procStat(pid, field int) (string, error) {
	data, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "stat"))
	if errors.Is(err, os.ErrNotExist) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	i := strings.LastIndexByte(string(data), ')')
	fields := strings.Fields(string(data)[i+1:])
	if i < 0 || len(fields) < 20 {
		return "", fmt.Errorf("pid %d: unrecognized /proc stat", pid)
	}
	return fields[field-3], nil
}

type harnessProcess struct {
	PID  int
	Args []string
}

// Filter before inspecting private process data: unrelated programs, other users,
// and harness helpers must never contribute transcript descriptors.
func harnessProcesses(comms []string, keep func(int, string, []string) (bool, error)) ([]harnessProcess, error) {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return nil, err
	}
	var out []harnessProcess
	uidPrefix := strconv.Itoa(os.Getuid()) + "\t"
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil {
			continue
		}
		read := func(name string) ([]byte, error) {
			return os.ReadFile(filepath.Join("/proc", entry.Name(), name))
		}
		name, err := read("comm")
		comm := strings.TrimSpace(string(name))
		if err == nil && !slices.Contains(comms, comm) {
			continue
		}
		var status, cmdline []byte
		if err == nil {
			status, err = read("status")
		}
		if err == nil {
			_, uid, _ := strings.Cut(string(status), "\nUid:\t")
			if !strings.HasPrefix(uid, uidPrefix) {
				continue
			}
			cmdline, err = read("cmdline")
		}
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return nil, err
		}
		if len(cmdline) == 0 {
			// Zombies lose argv; an exited, unreaped child is not an unknown live host.
			state, err := procStat(pid, 3)
			if err != nil {
				return nil, err
			}
			if state == "" || state == "Z" || state == "X" {
				continue
			}
		}
		args := strings.Split(strings.TrimRight(string(cmdline), "\x00"), "\x00")
		match, err := keep(pid, comm, args)
		if err != nil {
			return nil, err
		}
		if match {
			out = append(out, harnessProcess{pid, args})
		}
	}
	return out, nil
}

// Missing descriptors/processes are ordinary close/exit races. Other failures
// are not negative evidence: the caller must preserve unknown liveness.
func processFiles(pid int) (map[string]string, error) {
	dir := filepath.Join("/proc", strconv.Itoa(pid), "fd")
	fds, err := os.ReadDir(dir)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	out := make(map[string]string, len(fds))
	for _, fd := range fds {
		target, err := os.Readlink(filepath.Join(dir, fd.Name()))
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return out, err
		}
		out[fd.Name()] = target
	}
	return out, nil
}

func processEnv(pid int) (map[string]string, error) {
	data, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "environ"))
	if err != nil {
		return nil, err
	}
	out := map[string]string{}
	for item := range strings.SplitSeq(string(data), "\x00") {
		key, value, ok := strings.Cut(item, "=")
		if ok {
			out[key] = value
		}
	}
	return out, nil
}

// The CLI names worker processes omp too. Only launch/acp hosts can own live
// conversations (cli.ts:182-191,484-553; cli-commands.ts:27-287).
var ompMaintenance = strings.Fields("auth-broker auth-gateway agents bench browser-relay cleanse collab commit completions __complete compress config dry-balance daemon broker help find gc grep gallery git grievances images img if-bench install join login models plugin plugins predict ps say clip play share setup shell read render skill skills ssh stats stream update usage tiny-models token toks ttsr q search web-search wt worktree --help -h --version -v --license --smoke-test --alias")

func ompHost(pid int, comm string, args []string) (bool, error) {
	if len(args) == 0 {
		return comm == "omp", nil
	}
	exe := filepath.Base(args[0])
	if comm != "bun" && exe == "node" {
		return false, nil // the CLI requires Bun, not Node
	}
	args = args[1:]
	if comm == "bun" || exe == "bun" {
		if len(args) == 0 {
			return false, nil
		}
		script := args[0]
		if !filepath.IsAbs(script) {
			cwd, err := os.Readlink(filepath.Join("/proc", strconv.Itoa(pid), "cwd"))
			if errors.Is(err, os.ErrNotExist) {
				return false, nil
			}
			if err != nil {
				return false, err
			}
			script = filepath.Join(cwd, script)
		}
		if !ompEntrypoint(script) {
			if filepath.Base(script) != "omp" {
				return false, nil
			}
			resolved, err := filepath.EvalSymlinks(script)
			if err != nil {
				return false, err
			}
			if !ompEntrypoint(resolved) {
				return false, nil
			}
		}
		args = args[1:]
	}
	for len(args) > 0 {
		if args[0] == "--profile" {
			args = args[min(2, len(args)):]
		} else if strings.HasPrefix(args[0], "--profile=") {
			args = args[1:]
		} else {
			break
		}
	}
	return len(args) == 0 || (!strings.HasPrefix(args[0], "__omp_worker_") && !slices.Contains(ompMaintenance, args[0])), nil
}

func ompEntrypoint(path string) bool {
	return strings.HasSuffix(path, "/coding-agent/src/cli.ts") ||
		strings.HasSuffix(path, "/coding-agent/dist/cli.js") ||
		strings.HasSuffix(path, "/pi-coding-agent/src/cli.ts") ||
		strings.HasSuffix(path, "/pi-coding-agent/dist/cli.js")
}

func ompProcessProfile(args []string, env map[string]string) string {
	profile := ompProfileEnv(env)
	for i, arg := range args {
		if arg == "--" {
			break
		}
		if arg == "--profile" && i+1 < len(args) {
			profile = args[i+1]
		} else if value, ok := strings.CutPrefix(arg, "--profile="); ok {
			profile = value
		}
	}
	return profile
}

// Match ttyid.ts:42-83: stdin's TTY wins, then the same ordered environment
// fallbacks. /dev/null is a character device but not a TTY.
func ompTerminal(stdin string, env map[string]string) string {
	if strings.HasPrefix(stdin, "/dev/pts/") || strings.HasPrefix(stdin, "/dev/tty") || stdin == "/dev/console" {
		return strings.ReplaceAll(strings.TrimPrefix(stdin, "/dev/"), "/", "-")
	}
	if pane := env["ZELLIJ_PANE_ID"]; pane != "" {
		session := strings.NewReplacer("/", "-", "\\", "-").Replace(env["ZELLIJ_SESSION_NAME"])
		if session != "" {
			return "zellij-" + session + "-" + pane
		}
		return "zellij-" + pane
	}
	for _, pair := range [][2]string{{"TMUX_PANE", "tmux"}, {"CMUX_SURFACE_ID", "cmux"}, {"KITTY_WINDOW_ID", "kitty"}, {"WEZTERM_PANE", "wezterm"}, {"TERM_SESSION_ID", "apple"}, {"WT_SESSION", "wt"}} {
		if value := env[pair[0]]; value != "" {
			return pair[1] + "-" + value
		}
	}
	return ""
}

// A live identity must name a main session: child transcripts cannot identify
// their host, and arbitrary JSONL opened by a tool is not an omp session.
func (p *ompPlacement) liveIdentity(path string) (string, error) {
	header, err := ompHeader(path)
	artifact := false
	if err == nil && header.ID != "" {
		_, artifact, err = p.artifactParent(path)
	}
	if err == nil && (header.ID == "" || artifact) {
		err = fmt.Errorf("%s: not an omp main session", path)
	}
	return header.ID, err
}

// RunningOmp uses a real conversation host plus its current terminal breadcrumb,
// not argv's initial --resume. Breadcrumbs survive exit, and lazy/headless hosts
// need not hold any transcript open: absence of either signal is unknown.
// Source: session-paths.ts:357-389; session-manager.ts:2136-2164,2656-2672;
// session-storage.ts:210-250; task/executor.ts:4046-4053 (children suppress crumbs).
func RunningOmp() (map[string]Running, error) {
	procs, err := harnessProcesses([]string{"omp", "bun"}, ompHost)
	if err != nil {
		return map[string]Running{}, err
	}
	return runningOmpProcesses(procs)
}

func runningOmpProcesses(procs []harnessProcess) (map[string]Running, error) {
	out := map[string]Running{}
	crumbOwners := map[string]int{}
	var unknown error
	for _, proc := range procs {
		start, err := procStat(proc.PID, 22)
		if err != nil {
			unknown = errors.Join(unknown, err)
			continue
		}
		if start == "" {
			continue
		}
		fds, fdErr := processFiles(proc.PID)
		env, envErr := processEnv(proc.PID)
		if errors.Is(envErr, os.ErrNotExist) {
			continue
		}
		var placement ompPlacement
		if envErr == nil {
			placement.loaded = true
			var cwd string
			for _, key := range []string{"PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR"} {
				root := env[key]
				if root == "" || filepath.IsAbs(root) {
					continue
				}
				if cwd == "" {
					cwd, placement.err = os.Readlink(filepath.Join("/proc", strconv.Itoa(proc.PID), "cwd"))
					if placement.err != nil {
						break
					}
				}
				env[key] = filepath.Join(cwd, root)
			}
			placement.locations = []ompLocation{ompLocationFor(ompProcessProfile(proc.Args, env), func(key string) string { return env[key] })}
			placement.pointers = make([]ompPointerSet, 1)
			if root := env["PI_CODING_AGENT_SESSION_DIR"]; root != "" && placement.err == nil {
				placement.flatRoot, placement.err = ompCanonicalPath(root)
			}
		}
		host := map[string]Running{}
		var hostErr error
		for _, target := range fds {
			if !strings.HasSuffix(target, ".jsonl") {
				continue
			}
			id, err := placement.liveIdentity(target)
			if err != nil {
				// Children suppress breadcrumbs and cannot identify the main host.
				// Unrelated JSONL opened by tools cannot identify it either.
				continue
			}
			host[id] = Running{PID: proc.PID}
		}
		if envErr == nil {
			terminal := ompTerminal(fds["0"], nil)
			if terminal == "" {
				tty, err := procStat(proc.PID, 7)
				hostErr = errors.Join(hostErr, err)
				if tty != "" && tty != "0" {
					terminal = ompTerminal("", env)
				}
			}
			if terminal != "" && filepath.Base(terminal) == terminal {
				crumb := filepath.Join(placement.locations[0].State, "terminal-sessions", terminal)
				if owner, ok := crumbOwners[crumb]; ok && owner != proc.PID {
					unknown = errors.Join(unknown, fmt.Errorf("%s: multiple omp hosts share a terminal", crumb))
					for id, running := range out {
						if running.PID == owner {
							delete(out, id)
						}
					}
					continue
				}
				crumbOwners[crumb] = proc.PID
				data, err := os.ReadFile(crumb)
				if err == nil {
					// An existing breadcrumb is authoritative even for a fresh,
					// not-yet-materialized target. Never substitute an older fd.
					clear(host)
					path := ompMarkerPath("terminal-sessions", data)
					if path == "" {
						err = fmt.Errorf("%s: unreadable omp breadcrumb", crumb)
					} else {
						var id string
						id, err = placement.liveIdentity(path)
						if err == nil {
							host[id] = Running{PID: proc.PID}
						}
					}
				}
				if err != nil && (!errors.Is(err, os.ErrNotExist) || len(host) == 0) {
					hostErr = errors.Join(hostErr, err)
				}
			}
		}
		end, statErr := procStat(proc.PID, 22)
		if statErr == nil && end != start {
			continue // exit or pid reuse during observation
		}
		unknown = errors.Join(unknown, hostErr, fdErr, envErr, statErr)
		if len(host) != 1 {
			unknown = errors.Join(unknown, fmt.Errorf("omp pid %d: current session is unknown", proc.PID))
			continue
		}
		for id, running := range host {
			running.TmuxPane = pane(proc.PID)
			out[id] = running
		}
	}
	return out, unknown
}
