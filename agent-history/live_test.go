package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"testing"
	"time"
	"unsafe"
)

func TestRunningClaude(t *testing.T) {
	t.Setenv("CLAUDE_CONFIG_DIR", t.TempDir())
	dir := filepath.Join(claudeDir(), "sessions")
	must(t, os.MkdirAll(dir, 0o700))
	register := func(name string, pid int, start, tmux string) {
		body := fmt.Sprintf(`{"pid":%d,"sessionId":%q,"procStart":%q,"tmux":%q,"status":"idle"}`, pid, name, start, tmux)
		must(t, os.WriteFile(filepath.Join(dir, fmt.Sprint(pid)+".json"), []byte(body), 0o600))
	}
	me := os.Getpid()
	start, err := procStat(me, 22)
	must(t, err)
	register("live", me, start, "work:@3.%12")
	register("reused-pid", os.Getppid(), "1", "work:@4.%13") // pid alive, but a different process
	register("dead", 1<<22+7, "5", "")

	got, err := RunningClaude()
	must(t, err)
	if r, ok := got["live"]; !ok || r.TmuxPane != "%12" || r.PID != me || r.Status != "idle" {
		t.Errorf("live = %+v, %v", r, ok)
	}
	for _, id := range []string{"reused-pid", "dead"} {
		if _, ok := got[id]; ok {
			t.Errorf("%s counted as running", id)
		}
	}
}

func TestResolveMarksRunning(t *testing.T) {
	opt := defaults
	opt.Running = map[string]Running{"on": {PID: 1, TmuxPane: "%4"}}
	got := Resolve([]Entry{
		entry(t, Session{ID: "on", Cwd: "/r"}, "otlp"),
		entry(t, Session{ID: "off", Cwd: "/r"}, "otlp"),
	}, "otlp", opt)
	for _, s := range got[0].Sessions {
		if (s.Running != nil) != (s.ID == "on") {
			t.Errorf("%s running = %+v", s.ID, s.Running)
		}
	}
}

// One harness that can't tell what is running leaves the others' sessions trusted.
func TestResolveUnknownIsPerHarness(t *testing.T) {
	opt := defaults
	opt.RunningErr = map[string]error{"codex": errors.New("unreadable")}
	codex := entry(t, Session{ID: "cx", Cwd: "/r"}, "otlp")
	codex.Harness = "codex"
	got := Resolve([]Entry{entry(t, Session{ID: "cl", Cwd: "/r"}, "otlp"), codex}, "otlp", opt)
	for _, s := range got[0].Sessions {
		if s.RunningUnknown != (s.Harness == "codex") {
			t.Errorf("%s (%s) running_unknown = %v", s.ID, s.Harness, s.RunningUnknown)
		}
	}
}

func TestRunningClaudeUnreadableIsAnError(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("root reads anything")
	}
	t.Setenv("CLAUDE_CONFIG_DIR", t.TempDir())
	dir := filepath.Join(claudeDir(), "sessions")
	must(t, os.MkdirAll(dir, 0o000))
	defer os.Chmod(dir, 0o700)
	if _, err := RunningClaude(); err == nil {
		t.Errorf("unreadable registry reported as nothing running")
	}
	// So is a registration that doesn't parse: it could be a live session.
	must(t, os.Chmod(dir, 0o700))
	for _, body := range []string{`{"pid":9,"sess`, `{"pid":9,"sessionId":"s"}`, `{"sessionId":"s","procStart":"5"}`} {
		must(t, os.WriteFile(filepath.Join(dir, "9.json"), []byte(body), 0o600))
		if _, err := RunningClaude(); err == nil {
			t.Errorf("registration %s reported as nothing running", body)
		}
	}
	must(t, os.Remove(filepath.Join(dir, "9.json")))

	// No registry at all is a real answer: nothing running.
	must(t, os.Chmod(dir, 0o700))
	must(t, os.Remove(dir))
	if got, err := RunningClaude(); err != nil || len(got) != 0 {
		t.Errorf("missing registry = %v, %v", got, err)
	}
}

// These fixtures are real current-user processes and (where requested) isolated
// PTYs. Their files contain only synthetic headers; no omp/API invocation is used.
func ompLiveEnv(t *testing.T) map[string]string {
	t.Helper()
	root := t.TempDir()
	return map[string]string{
		"HOME":                root,
		"PI_CODING_AGENT_DIR": filepath.Join(root, "agent"),
		"OMP_PROFILE":         "",
		"PI_PROFILE":          "",
		"XDG_DATA_HOME":       "",
		"XDG_STATE_HOME":      "",
	}
}

func ompLiveFile(t *testing.T, path, id string) string {
	t.Helper()
	must(t, os.MkdirAll(filepath.Dir(path), 0o700))
	body := fmt.Sprintf("{\"type\":\"session\",\"version\":3,\"id\":%q,\"timestamp\":\"2026-09-03T10:00:00Z\",\"cwd\":\"/synthetic\"}\n", id)
	must(t, os.WriteFile(path, []byte(body), 0o600))
	return path
}

func ompLiveCrumb(t *testing.T, env map[string]string, profile, terminal, cwd, path string) string {
	t.Helper()
	file := filepath.Join(ompStateDir(env, profile), "terminal-sessions", terminal)
	must(t, os.MkdirAll(filepath.Dir(file), 0o700))
	must(t, os.WriteFile(file, []byte(cwd+"\n"+path+"\n"), 0o600))
	return file
}

func ompLivePTY(t *testing.T) (*os.File, *os.File) {
	t.Helper()
	master, err := os.OpenFile("/dev/ptmx", os.O_RDWR|syscall.O_NOCTTY, 0)
	must(t, err)
	t.Cleanup(func() { master.Close() })
	var unlock, number uint32
	_, _, errno := syscall.Syscall(syscall.SYS_IOCTL, master.Fd(), syscall.TIOCSPTLCK, uintptr(unsafe.Pointer(&unlock)))
	if errno != 0 {
		t.Fatal(errno)
	}
	_, _, errno = syscall.Syscall(syscall.SYS_IOCTL, master.Fd(), syscall.TIOCGPTN, uintptr(unsafe.Pointer(&number)))
	if errno != 0 {
		t.Fatal(errno)
	}
	slave, err := os.OpenFile(fmt.Sprintf("/dev/pts/%d", number), os.O_RDWR|syscall.O_NOCTTY, 0)
	must(t, err)
	t.Cleanup(func() { slave.Close() })
	return master, slave
}

func ompLiveHost(t *testing.T, env map[string]string, name, command, hold string, tty bool, args ...string) (*exec.Cmd, func()) {
	t.Helper()
	dir := t.TempDir()
	exe := filepath.Join(dir, name)
	must(t, os.Symlink("/bin/sh", exe))
	script := filepath.Join(dir, command)
	must(t, os.WriteFile(script, []byte("if [ -n \"$HOLD\" ]; then exec 3<\"$HOLD\"; fi\nprintf ready > \"$READY\"\nread -r _\n"), 0o600))
	cmd := exec.Command(exe, append([]string{command}, args...)...)
	if argv0 := env["TEST_ARGV0"]; argv0 != "" {
		cmd.Args[0] = argv0
	}
	cmd.Dir = dir
	for key, value := range env {
		cmd.Env = append(cmd.Env, key+"="+value)
	}
	ready := filepath.Join(dir, "ready")
	cmd.Env = append(cmd.Env, "READY="+ready, "HOLD="+hold)
	var input *os.File
	if tty {
		_, input = ompLivePTY(t)
		cmd.Stdin = input
		cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true, Setctty: true, Ctty: 0}
	} else {
		reader, writer, err := os.Pipe()
		must(t, err)
		t.Cleanup(func() { writer.Close(); reader.Close() })
		cmd.Stdin = reader
	}
	must(t, cmd.Start())
	stopped := false
	stop := func() {
		if !stopped {
			cmd.Process.Kill() // only this fixture, never a tmux pane/process group
			cmd.Wait()
			stopped = true
		}
	}
	t.Cleanup(stop)
	deadline := time.Now().Add(5 * time.Second)
	for {
		if _, err := os.Stat(ready); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("fixture did not become ready")
		}
		time.Sleep(10 * time.Millisecond)
	}
	return cmd, stop
}

// Scope observations to fixture pids after the production host filter. This
// avoids inspecting any real omp transcripts on a developer's machine.
func ompFixtureRunning(t *testing.T, pids ...int) (map[string]Running, error) {
	t.Helper()
	procs, err := harnessProcesses("omp", ompHost)
	must(t, err)
	var fixtures []harnessProcess
	for _, proc := range procs {
		for _, pid := range pids {
			if proc.PID == pid {
				fixtures = append(fixtures, proc)
				break
			}
		}
	}
	return runningOmpProcesses(fixtures)
}

func TestRunningOmpBreadcrumbLifecycle(t *testing.T) {
	env := ompLiveEnv(t)
	env["TMUX_PANE"] = "%991"
	first := ompLiveFile(t, filepath.Join(t.TempDir(), "original.jsonl"), "first")
	secondDir := t.TempDir()
	second := ompLiveFile(t, filepath.Join(secondDir, "relocated.jsonl"), "header-not-filename")
	crumb := ompLiveCrumb(t, env, "", "tmux-%991", secondDir, first)
	if got, err := ompFixtureRunning(t); err != nil || len(got) != 0 {
		t.Fatalf("breadcrumb without a host = %v, %v", got, err)
	}
	cmd, stop := ompLiveHost(t, env, "omp", "launch", first, false, "--resume", first)
	got, err := ompFixtureRunning(t, cmd.Process.Pid)
	must(t, err)
	if got["first"].PID != cmd.Process.Pid || got["first"].TmuxPane != "" {
		t.Fatalf("host breadcrumb = %+v", got)
	}
	// argv still names first. The breadcrumb switches to a relative relocated
	// file whose header ID, not basename, is its index identity.
	must(t, os.WriteFile(crumb, []byte(secondDir+"\n"+filepath.Base(second)+"\n"), 0o600))
	got, err = ompFixtureRunning(t, cmd.Process.Pid)
	must(t, err)
	if len(got) != 1 || got["header-not-filename"].PID != cmd.Process.Pid {
		t.Fatalf("switched breadcrumb = %+v", got)
	}
	must(t, os.WriteFile(crumb, []byte(secondDir+"\nnot-created.jsonl\nfresh\n"), 0o600))
	if got, err := ompFixtureRunning(t, cmd.Process.Pid); err == nil || len(got) != 0 {
		t.Fatalf("fresh switched breadcrumb must not fall back to old fd = %v, %v", got, err)
	}
	stop()
	if got, err := ompFixtureRunning(t, cmd.Process.Pid); err != nil || len(got) != 0 {
		t.Fatalf("stale breadcrumb after exit = %v, %v", got, err)
	}
}

func TestRunningOmpUsesStdinTTYBeforePaneFallback(t *testing.T) {
	env := ompLiveEnv(t)
	env["TMUX_PANE"] = "%992"
	right := ompLiveFile(t, filepath.Join(t.TempDir(), "right.jsonl"), "tty-current")
	wrong := ompLiveFile(t, filepath.Join(t.TempDir(), "wrong.jsonl"), "stale-tmux")
	ompLiveCrumb(t, env, "", "tmux-%992", "/", wrong)
	cmd, _ := ompLiveHost(t, env, "omp", "launch", "", true)
	stdin, err := os.Readlink(fmt.Sprintf("/proc/%d/fd/0", cmd.Process.Pid))
	must(t, err)
	// Use the actual PTY's number, not the implementation's terminal helper.
	terminal := "pts-" + filepath.Base(stdin)
	ompLiveCrumb(t, env, "", terminal, "/", right)
	got, err := ompFixtureRunning(t, cmd.Process.Pid)
	must(t, err)
	if len(got) != 1 || got["tty-current"].PID != cmd.Process.Pid || got["tty-current"].TmuxPane != "%992" {
		t.Fatalf("PTY takes precedence = %+v", got)
	}
}

func TestRunningOmpHostFilteringAndHeadlessFD(t *testing.T) {
	env := ompLiveEnv(t)
	path := ompLiveFile(t, filepath.Join(t.TempDir(), "custom.jsonl"), "headless")
	for _, fixture := range [][2]string{{"editor", "launch"}, {"omp", "__omp_worker_js_eval"}, {"omp", "gc"}, {"omp", "auth-broker"}} {
		t.Run(fixture[0]+"-"+fixture[1], func(t *testing.T) {
			cmd, stop := ompLiveHost(t, env, fixture[0], fixture[1], path, false)
			got, err := ompFixtureRunning(t, cmd.Process.Pid)
			must(t, err)
			if len(got) != 0 {
				t.Fatalf("unrelated/worker/maintenance process counted: %+v", got)
			}
			stop()
		})
	}
	cmd, stop := ompLiveHost(t, env, "omp", "launch", path, false)
	got, err := ompFixtureRunning(t, cmd.Process.Pid)
	must(t, err)
	if len(got) != 1 || got["headless"].PID != cmd.Process.Pid || got["headless"].TmuxPane != "" {
		t.Fatalf("positive headless descriptor = %+v", got)
	}
	stop()
	if got, err := ompFixtureRunning(t, cmd.Process.Pid); err != nil || len(got) != 0 {
		t.Fatalf("closed descriptor after exit = %v, %v", got, err)
	}
	unknown, _ := ompLiveHost(t, env, "omp", "launch", "", false, "--resume", path)
	if got, err := ompFixtureRunning(t, unknown.Process.Pid); err == nil || len(got) != 0 {
		t.Fatalf("headless argv alone = %v, %v; want unknown", got, err)
	}
}

func TestRunningOmpBreadcrumbReadFailuresAreUnknown(t *testing.T) {
	env := ompLiveEnv(t)
	env["TMUX_PANE"] = "%993"
	path := ompLiveFile(t, filepath.Join(t.TempDir(), "target.jsonl"), "target")
	crumb := ompLiveCrumb(t, env, "", "tmux-%993", "/", path)
	cmd, _ := ompLiveHost(t, env, "omp", "launch", "", false)
	for _, body := range []string{"invalid", "/\n/absent/synthetic.jsonl\n", "/\n" + path + "\n"} {
		must(t, os.WriteFile(crumb, []byte(body), 0o600))
		if body == "/\n"+path+"\n" {
			must(t, os.WriteFile(path, []byte("{broken\n"), 0o600))
		}
		if got, err := ompFixtureRunning(t, cmd.Process.Pid); err == nil || len(got) != 0 {
			t.Errorf("unreadable current identity %q = %v, %v", body, got, err)
		}
	}
	must(t, os.Remove(crumb))
	if got, err := ompFixtureRunning(t, cmd.Process.Pid); err == nil || len(got) != 0 {
		t.Errorf("missing breadcrumb/descriptor = %v, %v", got, err)
	}
	must(t, os.Mkdir(crumb, 0o700)) // EISDIR is a read failure even as root
	if got, err := ompFixtureRunning(t, cmd.Process.Pid); err == nil || len(got) != 0 {
		t.Errorf("breadcrumb read failure = %v, %v", got, err)
	}
}

func TestRunningOmpProfileAndTerminalFallback(t *testing.T) {
	env := ompLiveEnv(t)
	env["OMP_PROFILE"] = "named"
	env["PI_PROFILE"] = "wrong"
	env["ZELLIJ_PANE_ID"] = "9"
	env["ZELLIJ_SESSION_NAME"] = `team/work\one`
	env["TMUX_PANE"] = "%994"
	right := ompLiveFile(t, filepath.Join(t.TempDir(), "profile.jsonl"), "profile-current")
	wrong := ompLiveFile(t, filepath.Join(t.TempDir(), "default.jsonl"), "profile-stale")
	ompLiveCrumb(t, env, "", "zellij-team-work-one-9", "/", wrong)
	ompLiveCrumb(t, env, "named", "tmux-%994", "/", wrong)
	ompLiveCrumb(t, env, "named", "zellij-team-work-one-9", "/", right)
	cmd, stop := ompLiveHost(t, env, "omp", "launch", "", false)
	got, err := ompFixtureRunning(t, cmd.Process.Pid)
	must(t, err)
	if len(got) != 1 || got["profile-current"].PID != cmd.Process.Pid {
		t.Fatalf("profile/Zellij precedence = %+v", got)
	}
	stop()
	// Explicitly empty OMP_PROFILE overrides PI_PROFILE; CLI profile then
	// overrides both, including an inherited named profile.
	env["OMP_PROFILE"] = ""
	cmd, stop = ompLiveHost(t, env, "omp", "launch", "", false)
	got, err = ompFixtureRunning(t, cmd.Process.Pid)
	must(t, err)
	if len(got) != 1 || got["profile-stale"].PID != cmd.Process.Pid {
		t.Fatalf("empty OMP_PROFILE should select default = %+v", got)
	}
	stop()
	cmd, _ = ompLiveHost(t, env, "omp", "launch", "", false, "--profile", "named")
	got, err = ompFixtureRunning(t, cmd.Process.Pid)
	must(t, err)
	if len(got) != 1 || got["profile-current"].PID != cmd.Process.Pid {
		t.Fatalf("CLI profile precedence = %+v", got)
	}
}

func TestRunningOmpAmbiguousTerminalIsUnknown(t *testing.T) {
	env := ompLiveEnv(t)
	env["TMUX_PANE"] = "%995"
	path := ompLiveFile(t, filepath.Join(t.TempDir(), "shared.jsonl"), "ambiguous")
	ompLiveCrumb(t, env, "", "tmux-%995", "/", path)
	first, _ := ompLiveHost(t, env, "omp", "launch", "", false)
	second, _ := ompLiveHost(t, env, "omp", "launch", "", false)
	if got, err := ompFixtureRunning(t, first.Process.Pid, second.Process.Pid); err == nil || len(got) != 0 {
		t.Fatalf("two hosts share one breadcrumb = %v, %v; want unknown", got, err)
	}
}

func TestRunningOmpChildFDDoesNotIdentifyParent(t *testing.T) {
	env := ompLiveEnv(t)
	child := ompLiveFile(t, filepath.Join(t.TempDir(), "2026-09-03T10-00-00_parent", "Scout.jsonl"), "child-uuid")
	cmd, _ := ompLiveHost(t, env, "omp", "launch", child, false)
	if got, err := ompFixtureRunning(t, cmd.Process.Pid); err == nil || len(got) != 0 {
		t.Fatalf("only a child descriptor = %v, %v; want parent unknown", got, err)
	}
}

func TestRunningOmpBunHostsAndTitleSlot(t *testing.T) {
	env := ompLiveEnv(t)
	env["TEST_ARGV0"] = "bun"
	path := ompLiveFile(t, filepath.Join(t.TempDir(), "bun.jsonl"), "bun-main")
	header, err := os.ReadFile(path)
	must(t, err)
	slot := []byte("{\"type\":\"title\",\"v\":1,\"title\":\"Synthetic\",\"pad\":\"\"}\n")
	must(t, os.WriteFile(path, append(slot, header...), 0o600))
	cmd, stop := ompLiveHost(t, env, "omp", "entrypoint", path, false)
	got, err := ompFixtureRunning(t, cmd.Process.Pid)
	must(t, err)
	if len(got) != 1 || got["bun-main"].PID != cmd.Process.Pid {
		t.Fatalf("interpreted bun host with title slot = %+v", got)
	}
	stop()
	worker, _ := ompLiveHost(t, env, "omp", "entrypoint", path, false, "__omp_worker_stats_sync")
	if got, err := ompFixtureRunning(t, worker.Process.Pid); err != nil || len(got) != 0 {
		t.Fatalf("interpreted worker host = %v, %v", got, err)
	}
}
