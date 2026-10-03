package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"
)

func ompTestEnv(t *testing.T) string {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("PI_CONFIG_DIR", ".omp")
	t.Setenv("PI_CODING_AGENT_DIR", "")
	t.Setenv("PI_CODING_AGENT_SESSION_DIR", "")
	t.Setenv("OMP_PROFILE", "")
	t.Setenv("PI_PROFILE", "")
	t.Setenv("XDG_DATA_HOME", "")
	t.Setenv("XDG_STATE_HOME", "")
	return home
}

func ompWrite(t *testing.T, path, body string) string {
	t.Helper()
	must(t, os.MkdirAll(filepath.Dir(path), 0o700))
	must(t, os.WriteFile(path, []byte(body), 0o600))
	return path
}

const ompTestHeader = `{"type":"session","version":3,"id":"real-id","timestamp":"2026-10-02T10:00:00Z","cwd":"/work/a repo","title":"header auto","titleSource":"auto"}` + "\n"

func ompCheckSources(t *testing.T, want ...string) {
	t.Helper()
	files, err := ompSessions()
	must(t, err)
	if len(files) != len(want) {
		t.Fatalf("sources = %v, want %v", files, want)
	}
	for _, path := range want {
		if !slices.ContainsFunc(files, func(file []string) bool { return file[0] == path }) {
			t.Errorf("missing source %s in %v", path, files)
		}
	}
}

func ompCheckResume(t *testing.T, path string, want ...string) {
	t.Helper()
	s, err := ReadOmp(path)
	must(t, err)
	if !reflect.DeepEqual(s.ResumeArgv, want) {
		t.Errorf("resume %s = %v, want %v", path, s.ResumeArgv, want)
	}
}

func TestReadOmpHumanBody(t *testing.T) {
	ompTestEnv(t)
	path := ompWrite(t, filepath.Join(ompLocationFor("", os.Getenv).Sessions, "lossy-slug", "different-filename.jsonl"), ompTestHeader+`{"type":"message","timestamp":"2026-10-02T10:01:00Z","message":{"role":"user","content":"fix https://github.com/o/r/pull/12"}}
{"type":"message","timestamp":"2026-10-02T10:02:00Z","message":{"role":"user","attribution":"user","steering":true,"content":[{"type":"text","text":"also tests"},{"type":"image","data":"secret image"},{"type":"text","text":"please"}]}}
{"type":"message","timestamp":"2026-10-02T10:03:00Z","message":{"role":"user","synthetic":true,"content":"synthetic https://github.com/o/r/pull/13"}}
{"type":"message","timestamp":"2026-10-02T10:04:00Z","message":{"role":"user","attribution":"agent","content":"notification https://github.com/o/r/pull/14"}}
{"type":"message","timestamp":"2026-10-02T10:05:00Z","message":{"role":"assistant","content":"output https://github.com/o/r/pull/15"}}
{"type":"message","timestamp":"2026-10-02T10:06:00Z","message":{"role":"toolResult","content":"tool https://github.com/o/r/pull/16"}}
{"type":"message","timestamp":"2026-10-02T10:00:30Z","message":{"role":"user","content":[{"type":"image","data":"image-only"}]}}
{"type":"message","time`)
	s, err := ReadOmp(path)
	must(t, err)
	check(t, "id", s.ID, "real-id")
	check(t, "cwd", s.Cwd, "/work/a repo")
	check(t, "started", s.Started, "2026-10-02T10:00:00Z")
	check(t, "last_active", s.LastActive, "2026-10-02T10:06:00Z")
	check(t, "entrypoint", s.Entrypoint, "")
	if len(s.Branches) != 0 {
		t.Fatalf("invented branches: %v", s.Branches)
	}
	want := []Message{{"2026-10-02T10:01:00Z", "user", "fix https://github.com/o/r/pull/12"}, {"2026-10-02T10:02:00Z", "user", "also tests\nplease"}}
	if !reflect.DeepEqual(s.Messages, want) {
		t.Fatalf("messages = %+v, want %+v", s.Messages, want)
	}
	if !reflect.DeepEqual(s.PRs, []string{"https://github.com/o/r/pull/12"}) {
		t.Fatalf("prs = %v", s.PRs)
	}
	id, parent := ompIdentity(path)
	check(t, "identity", id, s.ID)
	check(t, "parent", parent, "")
	if !reflect.DeepEqual(s.ResumeArgv, []string{"omp", "--resume", "real-id"}) {
		t.Fatalf("resume = %v", s.ResumeArgv)
	}
}

func TestOmpTitles(t *testing.T) {
	ompTestEnv(t)
	for _, tc := range []struct{ name, prefix, audit, title, active string }{
		{"slot", `{"type":"title","v":1,"title":"current","source":"auto","updatedAt":"2026-10-02T12:00:00Z","pad":""}` + "\n", `{"type":"title_change","title":"old user","source":"user"}`, "current", "2026-10-02T12:00:00Z"},
		{"clear", `{"type":"title","v":1,"title":"","updatedAt":"2026-10-02T12:00:00Z","pad":""}` + "\n", `{"type":"title_change","title":"old user","source":"user"}`, "", "2026-10-02T12:00:00Z"},
		{"invalid slot", `{"type":"title","v":1,"title":"invalid"}` + "\n", "", "header auto", "2026-10-02T10:00:00Z"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := ompWrite(t, filepath.Join(t.TempDir(), "session.jsonl"), tc.prefix+ompTestHeader+tc.audit+"\n")
			s, err := ReadOmp(path)
			must(t, err)
			check(t, "title", s.Title, tc.title)
			check(t, "activity", s.LastActive, tc.active)
		})
	}
}

func TestOmpHeaderTitleIgnoresAuditTrail(t *testing.T) {
	ompTestEnv(t)
	t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
	header := strings.ReplaceAll(ompTestHeader, "header auto", "human name")
	header = strings.Replace(header, `"titleSource":"auto"`, `"titleSource":"user"`, 1)
	path := ompWrite(t, filepath.Join(ompLocationFor("", os.Getenv).Sessions, "bucket", "session.jsonl"), header+
		"{\"type\":\"title_change\",\"title\":\"stale user name\",\"source\":\"user\"}\n"+
		"{\"type\":\"title_change\",\"title\":\"generated name\",\"source\":\"auto\"}\n")
	info, err := os.Stat(path)
	must(t, err)
	dst := indexPath("omp", "", "real-id")
	old := Session{Harness: "omp", ID: "real-id", Source: path, Cwd: "/work/a repo", Title: "stale user name"}
	must(t, writeAtomic(dst, Render(old), info.ModTime()))
	must(t, writeAtomic(stateFile(), []byte("2"), info.ModTime()))
	Reconcile()
	entry, err := ReadEntry(dst)
	must(t, err)
	check(t, "current user title after reconcile", entry.Title, "human name")
}

func TestOmpArtifactNotFork(t *testing.T) {
	ompTestEnv(t)
	dir := filepath.Join(ompLocationFor("", os.Getenv).Sessions, "bucket")
	parent := ompWrite(t, filepath.Join(dir, "2026-10-02T10-00-00Z_real-id.jsonl"), ompTestHeader)
	child := ompWrite(t, strings.TrimSuffix(parent, ".jsonl")+"/Parser.jsonl", strings.ReplaceAll(ompTestHeader, "real-id", "child-uuid")+`{"type":"session_init","timestamp":"2026-10-02T10:00:01Z","task":"Inspect parser https://github.com/o/r/pull/1","systemPrompt":"secret policy"}
{"type":"message","message":{"role":"user","attribution":"agent","content":"Inspect parser https://github.com/o/r/pull/1"}}
{"type":"session_init","task":"replacement revival task"}
{"type":"message","timestamp":"2026-10-02T10:00:02Z","message":{"role":"user","attribution":"user","content":"focus ownership"}}
`)
	s, err := ReadOmp(child)
	must(t, err)
	check(t, "agent name", s.ID, "Parser")
	check(t, "parent", s.Parent, "real-id")
	if len(s.Messages) != 2 || s.Messages[0].Kind != "prompt" || s.Messages[0].Text != "Inspect parser https://github.com/o/r/pull/1" {
		t.Fatalf("child body = %+v", s.Messages)
	}
	if len(s.ResumeArgv) != 0 {
		t.Fatalf("child resume = %v", s.ResumeArgv)
	}
	nested := ompWrite(t, strings.TrimSuffix(child, ".jsonl")+"/Worker.jsonl", ompTestHeader)
	canonical, _ := ompArtifactParent(nested, nil)
	check(t, "canonical sibling header", canonical, "child-uuid")
	ompCheckSources(t, parent, child, nested)
	must(t, os.Remove(nested))
	fork := ompWrite(t, filepath.Join(dir, "fork.jsonl"), strings.Replace(ompTestHeader, `"title":"header auto"`, `"parentSession":"`+parent+`","title":"header auto"`, 1)+`{"type":"session_init","task":"not a child task"}`)
	f, err := ReadOmp(fork)
	must(t, err)
	if f.Parent != "" || len(f.Messages) != 0 {
		t.Fatalf("fork classified as child: %+v", f)
	}
	// Even after its source disappears, the artifact folder retains its parent ID.
	must(t, os.Remove(parent))
	id, p := ompIdentity(child)
	check(t, "orphan agent", id, "Parser")
	check(t, "orphan parent", p, "real-id")
}

func TestOmpDiscoveryAndResume(t *testing.T) {
	home := ompTestEnv(t)
	defaultPath := ompWrite(t, filepath.Join(home, ".omp", "agent", "sessions", "bucket", "default.jsonl"), ompTestHeader)
	profilePath := ompWrite(t, filepath.Join(home, ".omp", "profiles", "review", "agent", "sessions", "bucket", "named.jsonl"), strings.ReplaceAll(ompTestHeader, "real-id", "profile-id"))
	customPath := ompWrite(t, filepath.Join(t.TempDir(), "relocated.jsonl"), strings.ReplaceAll(ompTestHeader, "real-id", "relocated-id"))
	state := ompLocationFor("", os.Getenv).State
	ompWrite(t, filepath.Join(state, "custom-session-files", "hash"), customPath)
	profileCustom := ompWrite(t, filepath.Join(t.TempDir(), "profile-custom.jsonl"), strings.ReplaceAll(ompTestHeader, "real-id", "profile-custom-id"))
	ompWrite(t, filepath.Join(ompLocationFor("review", os.Getenv).State, "custom-session-files", "profile-hash"), profileCustom)
	crumbPath := ompWrite(t, filepath.Join(t.TempDir(), "crumb.jsonl"), strings.ReplaceAll(ompTestHeader, "real-id", "crumb-id"))
	ompWrite(t, filepath.Join(state, "terminal-sessions", "pts-7"), filepath.Dir(crumbPath)+"\ncrumb.jsonl\nfresh\n")
	flat := t.TempDir()
	t.Setenv("PI_CODING_AGENT_SESSION_DIR", flat)
	flatPath := ompWrite(t, filepath.Join(flat, "flat.jsonl"), strings.ReplaceAll(ompTestHeader, "real-id", "flat-id"))
	for _, sidecar := range []string{"result.json", "state.db", "image.png", "backup.jsonl.bak", "__advisor.jsonl", "tool.jsonl"} {
		body := ompTestHeader
		if sidecar == "tool.jsonl" {
			body = `{"type":"message","message":{"role":"toolResult","content":"noise"}}`
		}
		ompWrite(t, filepath.Join(filepath.Dir(defaultPath), sidecar), body)
	}
	ompCheckSources(t, defaultPath, profilePath, customPath, profileCustom, crumbPath, flatPath)
	ompCheckResume(t, customPath, "omp", "--resume", customPath)
	ompCheckResume(t, profilePath, "omp", "--profile", "review", "--resume", profilePath)
	ompCheckResume(t, flatPath, "omp", "--resume", flatPath)
	ompCheckResume(t, profileCustom, "omp", "--profile", "review", "--resume", profileCustom)
	t.Setenv("OMP_PROFILE", "review")
	ompCheckResume(t, defaultPath, "omp", "--profile", "default", "--resume", defaultPath)
	ompCheckResume(t, customPath, "omp", "--profile", "default", "--resume", customPath)
	ompCheckResume(t, flatPath, "omp", "--profile", "review", "--resume", flatPath)
	ompCheckResume(t, profileCustom, "omp", "--profile", "review", "--resume", profileCustom)
}

func TestOmpRebuildTitleAndRetainMissing(t *testing.T) {
	ompTestEnv(t)
	t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
	path := ompWrite(t, filepath.Join(ompLocationFor("", os.Getenv).Sessions, "bucket", "session.jsonl"), ompTestHeader)
	must(t, indexFile(omp, []string{path}, false, nil))
	dst := indexPath("omp", "", "real-id")
	first, err := ReadEntry(dst)
	must(t, err)
	check(t, "initial title", first.Title, "header auto")
	ompWrite(t, path, `{"type":"title","v":1,"title":"renamed","updatedAt":"2026-10-02T12:00:00Z","pad":""}`+"\n"+ompTestHeader)
	stamp := time.Now().Add(time.Minute)
	must(t, os.Chtimes(path, stamp, stamp))
	must(t, indexFile(omp, []string{path}, false, nil))
	updated, err := ReadEntry(dst)
	must(t, err)
	check(t, "rewritten title", updated.Title, "renamed")
	must(t, os.Remove(path))
	must(t, indexFile(omp, []string{path}, false, nil))
	must(t, MarkMissing(dst))
	retained, err := ReadEntry(dst)
	must(t, err)
	check(t, "retained title", retained.Title, updated.Title)
	check(t, "retained cwd", retained.Cwd, updated.Cwd)
	if !retained.SourceMissing || len(retained.ResumeArgv) != 0 || retained.Resume != "" {
		t.Fatalf("missing source still resumable: %+v", retained)
	}
}

func TestOmpDiscoveryThroughXDGTransition(t *testing.T) {
	home := ompTestEnv(t)
	xdg := filepath.Join(home, "data")
	t.Setenv("XDG_DATA_HOME", xdg)
	legacy := ompWrite(t, filepath.Join(home, ".omp", "agent", "sessions", "bucket", "legacy.jsonl"), ompTestHeader)
	ompCheckSources(t, legacy)
	migrated := ompWrite(t, filepath.Join(xdg, "omp", "sessions", "bucket", "migrated.jsonl"), strings.ReplaceAll(ompTestHeader, "real-id", "migrated-id"))
	ompCheckSources(t, legacy, migrated)
	ompCheckResume(t, legacy, "omp", "--resume", legacy)
	ompCheckResume(t, migrated, "omp", "--resume", "migrated-id")
	profile := ompWrite(t, filepath.Join(xdg, "omp", "profiles", "review", "sessions", "bucket", "profile.jsonl"), strings.ReplaceAll(ompTestHeader, "real-id", "profile-id"))
	t.Setenv("OMP_PROFILE", "")
	t.Setenv("PI_PROFILE", "review")
	ompCheckSources(t, legacy, migrated, profile)
	// An explicitly empty OMP_PROFILE takes precedence over PI_PROFILE.
	ompCheckResume(t, migrated, "omp", "--resume", "migrated-id")
	ompCheckResume(t, profile, "omp", "--profile", "review", "--resume", profile)
}

func TestOmpNoResumeWithoutCwd(t *testing.T) {
	ompTestEnv(t)
	path := ompWrite(t, filepath.Join(t.TempDir(), "session.jsonl"), strings.Replace(ompTestHeader, `"cwd":"/work/a repo"`, `"cwd":""`, 1))
	s, err := ReadOmp(path)
	must(t, err)
	if len(s.ResumeArgv) != 0 {
		t.Fatalf("resume without historical cwd = %v", s.ResumeArgv)
	}
}

func TestOmpProfileChangeInvalidatesResumeCache(t *testing.T) {
	ompTestEnv(t)
	normal := ompWrite(t, filepath.Join(ompLocationFor("", os.Getenv).Sessions, "bucket", "normal.jsonl"), ompTestHeader)
	flat := t.TempDir()
	t.Setenv("PI_CODING_AGENT_SESSION_DIR", flat)
	custom := ompWrite(t, filepath.Join(flat, "custom.jsonl"), strings.ReplaceAll(ompTestHeader, "real-id", "custom-id"))
	Reconcile()
	cachedArgv := func(id string) []string {
		data, err := os.ReadFile(indexPath("omp", "", id))
		must(t, err)
		for line := range strings.Lines(string(data)) {
			if value, ok := strings.CutPrefix(line, "resume_argv: "); ok {
				var argv []string
				must(t, json.Unmarshal([]byte(value), &argv))
				return argv
			}
		}
		t.Fatalf("cached %s has no resume command", id)
		return nil
	}
	for _, profile := range []string{"review", "other", ""} {
		t.Setenv("OMP_PROFILE", profile)
		if !reconcileDue() {
			t.Fatal("profile change did not schedule a reconcile")
		}
		normalArgv := []string{"omp", "--profile", "default", "--resume", normal}
		customArgv := []string{"omp", "--profile", profile, "--resume", custom}
		if profile == "" {
			normalArgv = []string{"omp", "--resume", "real-id"}
			customArgv = []string{"omp", "--resume", custom}
		}
		// Consumers must not receive another profile's cached command while the
		// background reconcile is pending.
		for id, want := range map[string][]string{"real-id": normalArgv, "custom-id": customArgv} {
			e, err := ReadEntry(indexPath("omp", "", id))
			must(t, err)
			if !reflect.DeepEqual(e.ResumeArgv, want) {
				t.Errorf("profile %q get %s = %v, want %v", profile, id, e.ResumeArgv, want)
			}
		}
		Reconcile()
		if !reflect.DeepEqual(cachedArgv("real-id"), normalArgv) || !reflect.DeepEqual(cachedArgv("custom-id"), customArgv) {
			t.Fatalf("profile %q retained a cached command for another profile", profile)
		}
	}
}

func ompCheckCachedResume(t *testing.T, id string, want ...string) {
	t.Helper()
	e, err := ReadEntry(indexPath("omp", "", id))
	must(t, err)
	if !slices.Equal(e.ResumeArgv, want) {
		t.Errorf("first cached read = %v, want %v", e.ResumeArgv, want)
	}
	Reconcile()
	data, err := os.ReadFile(indexPath("omp", "", id))
	must(t, err)
	for line := range strings.Lines(string(data)) {
		if value, ok := strings.CutPrefix(line, "resume_argv: "); ok {
			var argv []string
			must(t, json.Unmarshal([]byte(value), &argv))
			if !slices.Equal(argv, want) {
				t.Errorf("persisted resume = %v, want %v", argv, want)
			}
			return
		}
	}
	t.Fatal("cached entry has no resume command")
}

func TestOmpCachedResumeTracksLocationChanges(t *testing.T) {
	home := ompTestEnv(t)
	t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
	xdg := filepath.Join(home, "data", "omp")
	t.Setenv("XDG_DATA_HOME", filepath.Dir(xdg))
	path := ompWrite(t, filepath.Join(home, ".omp", "agent", "sessions", "bucket", "session.jsonl"), ompTestHeader)
	Reconcile()
	must(t, os.MkdirAll(xdg, 0o700))
	ompCheckCachedResume(t, "real-id", "omp", "--resume", path)
	must(t, os.Remove(xdg))
	ompCheckCachedResume(t, "real-id", "omp", "--resume", "real-id")
	t.Setenv("PI_CODING_AGENT_DIR", filepath.Join(home, "override"))
	ompCheckCachedResume(t, "real-id", "omp", "--resume", path)
	t.Setenv("PI_CODING_AGENT_DIR", "")
	ompCheckCachedResume(t, "real-id", "omp", "--resume", "real-id")
}

func TestOmpCachedResumeTracksRegistryChanges(t *testing.T) {
	for _, kind := range []string{"custom-session-files", "terminal-sessions"} {
		t.Run(kind, func(t *testing.T) {
			home := ompTestEnv(t)
			t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
			t.Setenv("OMP_PROFILE", "review")
			flat := t.TempDir()
			t.Setenv("PI_CODING_AGENT_SESSION_DIR", flat)
			path := ompWrite(t, filepath.Join(flat, "session.jsonl"), ompTestHeader)
			Reconcile()
			body := func(target string) string {
				if kind == "terminal-sessions" {
					return filepath.Dir(target) + "\n" + filepath.Base(target) + "\n"
				}
				return target + "\n"
			}
			marker := ompWrite(t, filepath.Join(home, ".omp", "agent", kind, "marker"), body(path))
			ompCheckCachedResume(t, "real-id", "omp", "--profile", "default", "--resume", path)
			stamp, err := os.Stat(marker)
			must(t, err)
			ompWrite(t, marker, body(filepath.Join(flat, "absent.jsonl")))
			must(t, os.Chtimes(marker, stamp.ModTime(), stamp.ModTime()))
			ompCheckCachedResume(t, "real-id", "omp", "--profile", "review", "--resume", path)
			ompWrite(t, filepath.Join(home, ".omp", "profiles", "other", "agent", kind, "marker"), body(path))
			ompCheckCachedResume(t, "real-id", "omp", "--profile", "other", "--resume", path)
		})
	}
}

func TestOmpOrphanedNestedArtifactIsNotResumable(t *testing.T) {
	for _, broken := range []string{"deleted", "truncated"} {
		t.Run(broken, func(t *testing.T) {
			ompTestEnv(t)
			t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
			parent := ompWrite(t, filepath.Join(ompLocationFor("", os.Getenv).Sessions, "bucket", "2026-10-02T10-00-00Z_real-id.jsonl"), ompTestHeader)
			agent := ompWrite(t, strings.TrimSuffix(parent, ".jsonl")+"/Agent.jsonl", strings.ReplaceAll(ompTestHeader, "real-id", "agent-id"))
			worker := ompWrite(t, strings.TrimSuffix(agent, ".jsonl")+"/Worker.jsonl", strings.ReplaceAll(ompTestHeader, "real-id", "worker-id"))
			if broken == "deleted" {
				must(t, os.Remove(agent))
			} else {
				must(t, os.WriteFile(agent, []byte(`{"type":"session"`), 0o600))
			}
			if s, err := ReadOmp(worker); !errors.Is(err, errNotIndexed) {
				t.Errorf("orphaned artifact promoted: %+v, %v", s, err)
			}
			ompCheckSources(t, parent)
			if id, err := (&ompPlacement{}).liveIdentity(worker); err == nil {
				t.Errorf("orphaned artifact identifies a live host: %q", id)
			}
			cached := Session{Harness: "omp", ID: "worker-id", Source: worker, Cwd: "/work/a repo", ResumeArgv: []string{"omp", "--resume", "worker-id"}}
			dst := indexPath("omp", "", cached.ID)
			must(t, writeAtomic(dst, Render(cached), time.Now()))
			if e, err := ReadEntry(dst); !errors.Is(err, errNotIndexed) {
				t.Errorf("cached orphaned artifact promoted: %+v, %v", e, err)
			}
		})
	}
}

func TestOmpRelocationPreservingMtime(t *testing.T) {
	for _, mode := range []string{"managed move", "custom pointer"} {
		t.Run(mode, func(t *testing.T) {
			ompTestEnv(t)
			t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
			root := ompLocationFor("", os.Getenv).Sessions
			if mode == "custom pointer" {
				root = t.TempDir()
			}
			old := ompWrite(t, filepath.Join(root, "old", "transcript.jsonl"), ompTestHeader)
			marker := filepath.Join(ompLocationFor("", os.Getenv).State, "custom-session-files", "relocated")
			if mode == "custom pointer" {
				ompWrite(t, marker, old)
			}
			Reconcile()
			info, err := os.Stat(old)
			must(t, err)
			moved := filepath.Join(root, "new", "transcript.jsonl")
			if mode == "managed move" {
				must(t, os.MkdirAll(filepath.Dir(moved), 0o700))
				must(t, os.Rename(old, moved))
			} else {
				ompWrite(t, moved, ompTestHeader) // old copy survives, but the pointer chooses new
				must(t, os.Chtimes(moved, info.ModTime(), info.ModTime()))
				ompWrite(t, marker, moved)
			}
			Reconcile()
			e, err := ReadEntry(indexPath("omp", "", "real-id"))
			must(t, err)
			check(t, "relocated source", e.Source, moved)
			want := []string{"omp", "--resume", "real-id"}
			if mode == "custom pointer" {
				want[2] = moved
			}
			if e.SourceMissing || !slices.Equal(e.ResumeArgv, want) {
				t.Errorf("relocated resume = %v, missing=%v; want %v", e.ResumeArgv, e.SourceMissing, want)
			}
		})
	}
}

func TestOmpCachedArtifactClassificationPrecedesResume(t *testing.T) {
	for _, mode := range []string{"missing", "no argv"} {
		t.Run(mode, func(t *testing.T) {
			ompTestEnv(t)
			t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
			worker := ompWrite(t, filepath.Join(t.TempDir(), "2026-10-02T10-00-00Z_root-id", "Agent", "Worker.jsonl"), ompTestHeader)
			cached := Session{Harness: "omp", ID: "worker-id", Source: worker, Cwd: "/work/a repo"}
			if mode == "missing" {
				cached.ResumeArgv = []string{"omp", "--resume", "worker-id"}
			}
			dst := indexPath("omp", "", cached.ID)
			must(t, writeAtomic(dst, Render(cached), time.Now()))
			if mode == "missing" {
				must(t, os.Remove(worker))
				must(t, MarkMissing(dst))
			}
			if e, err := ReadEntry(dst); !errors.Is(err, errNotIndexed) {
				t.Errorf("non-resumable artifact promoted: %+v, %v", e, err)
			}
		})
	}
}

func TestOmpRegisteredArtifactWithLostParent(t *testing.T) {
	for _, broken := range []string{"deleted", "truncated"} {
		t.Run(broken, func(t *testing.T) {
			ompTestEnv(t)
			t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
			root := t.TempDir()
			alias := filepath.Join(t.TempDir(), "alias")
			must(t, os.Symlink(root, alias))
			parent := ompWrite(t, filepath.Join(alias, "custom.jsonl"), ompTestHeader)
			agent := ompWrite(t, strings.TrimSuffix(parent, ".jsonl")+"/Agent.jsonl", strings.ReplaceAll(ompTestHeader, "real-id", "agent-id"))
			ompWrite(t, filepath.Join(ompLocationFor("", os.Getenv).State, "custom-session-files", "custom"), parent)
			good := ompWrite(t, filepath.Join(alias, "good.jsonl"), strings.ReplaceAll(ompTestHeader, "real-id", "good-id"))
			ompWrite(t, filepath.Join(ompLocationFor("", os.Getenv).State, "custom-session-files", "good"), good)
			if broken == "deleted" {
				must(t, os.Remove(parent))
			} else {
				must(t, os.WriteFile(parent, []byte(`{"type":"session"`), 0o600))
			}
			realAgent := filepath.Join(root, "custom", "Agent.jsonl")
			for _, source := range []string{agent, realAgent} {
				if s, err := ReadOmp(source); !errors.Is(err, errNotIndexed) {
					t.Errorf("registered artifact %s promoted: %+v, %v", source, s, err)
				}
				cached := Session{Harness: "omp", ID: "agent-id", Source: source, Cwd: "/work/a repo", ResumeArgv: []string{"omp", "--resume", source}}
				dst := indexPath("omp", "", cached.ID)
				must(t, writeAtomic(dst, Render(cached), time.Now()))
				if e, err := ReadEntry(dst); !errors.Is(err, errNotIndexed) {
					t.Errorf("cached registered artifact %s promoted: %+v, %v", source, e, err)
				}
			}
			if id, err := (&ompPlacement{}).liveIdentity(realAgent); err == nil {
				t.Errorf("registered artifact identifies a live host through target: %q", id)
			}
			ompCheckSources(t, good)
			ompCheckResume(t, good, "omp", "--resume", good)
		})
	}
}

func TestOmpFlatRootOrphanArtifacts(t *testing.T) {
	for _, broken := range []string{"deleted", "truncated"} {
		t.Run(broken, func(t *testing.T) {
			ompTestEnv(t)
			t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
			root := t.TempDir()
			alias := filepath.Join(t.TempDir(), "alias")
			must(t, os.Symlink(root, alias))
			t.Setenv("PI_CODING_AGENT_SESSION_DIR", alias)
			parent := ompWrite(t, filepath.Join(alias, "session.jsonl"), ompTestHeader)
			agent := ompWrite(t, filepath.Join(alias, "session", "Agent.jsonl"), strings.ReplaceAll(ompTestHeader, "real-id", "agent-id"))
			good := ompWrite(t, filepath.Join(alias, "good.jsonl"), strings.ReplaceAll(ompTestHeader, "real-id", "good-id"))
			if broken == "deleted" {
				must(t, os.Remove(parent))
			} else {
				must(t, os.WriteFile(parent, []byte(`{"type":"session"`), 0o600))
			}
			realAgent := filepath.Join(root, "session", "Agent.jsonl")
			for _, source := range []string{agent, realAgent} {
				if s, err := ReadOmp(source); !errors.Is(err, errNotIndexed) {
					t.Errorf("flat-root orphan %s promoted: %+v, %v", source, s, err)
				}
				cached := Session{Harness: "omp", ID: "agent-id", Source: source, Cwd: "/work/a repo"}
				dst := indexPath("omp", "", cached.ID)
				must(t, writeAtomic(dst, Render(cached), time.Now()))
				if e, err := ReadEntry(dst); !errors.Is(err, errNotIndexed) {
					t.Errorf("cached flat-root orphan %s promoted: %+v, %v", source, e, err)
				}
			}
			if id, err := (&ompPlacement{}).liveIdentity(realAgent); err == nil {
				t.Errorf("flat-root artifact identifies a live host through target: %q", id)
			}
			ompCheckSources(t, good)
			ompCheckResume(t, good, "omp", "--resume", good)
		})
	}
}

func TestOmpExactPointersOverrideArtifactPlacement(t *testing.T) {
	for _, kind := range []string{"custom-session-files", "terminal-sessions"} {
		for _, location := range []string{"timestamp folder", "pointed artifact tree", "flat descendant"} {
			t.Run(kind+"/"+location, func(t *testing.T) {
				ompTestEnv(t)
				t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
				root := t.TempDir()
				alias := filepath.Join(t.TempDir(), "alias")
				must(t, os.Symlink(root, alias))
				state := ompLocationFor("", os.Getenv).State
				dir := "2026-10-02T10-00-00Z_heuristic-parent"
				if location == "pointed artifact tree" {
					outer := ompWrite(t, filepath.Join(alias, "outer.jsonl"), strings.ReplaceAll(ompTestHeader, "real-id", "outer-id"))
					ompWrite(t, filepath.Join(state, "custom-session-files", "outer"), outer)
					dir = "outer"
				} else if location == "flat descendant" {
					t.Setenv("PI_CODING_AGENT_SESSION_DIR", alias)
					dir = "nested"
				}
				target := ompWrite(t, filepath.Join(alias, dir, "interactive.jsonl"), ompTestHeader)
				body := target + "\n"
				if kind == "terminal-sessions" {
					body = filepath.Dir(target) + "\n" + filepath.Base(target) + "\n"
				}
				ompWrite(t, filepath.Join(state, kind, "interactive"), body)
				child := ompWrite(t, strings.TrimSuffix(target, ".jsonl")+"/Worker.jsonl", strings.ReplaceAll(ompTestHeader, "real-id", "worker-header"))
				nested := ompWrite(t, strings.TrimSuffix(child, ".jsonl")+"/Nested.jsonl", strings.ReplaceAll(ompTestHeader, "real-id", "nested-header"))
				placement := &ompPlacement{}
				for _, source := range []string{target, filepath.Join(root, dir, "interactive.jsonl")} {
					s, err := readOmp(source, placement)
					must(t, err)
					check(t, "main header ID", s.ID, "real-id")
					check(t, "main parent", s.Parent, "")
					ompCheckResume(t, source, "omp", "--resume", source)
					id, parent := ompIdentity(source)
					check(t, "index identity", id, "real-id")
					check(t, "index parent", parent, "")
					id, err = placement.liveIdentity(source)
					must(t, err)
					check(t, "live main identity", id, "real-id")
				}
				must(t, indexFile(omp, []string{target}, false, placement))
				for _, parent := range []string{"heuristic-parent", "outer-id"} {
					if _, err := os.Stat(indexPath("omp", parent, "interactive")); !errors.Is(err, os.ErrNotExist) {
						t.Errorf("interactive main written under heuristic parent %q: %v", parent, err)
					}
				}
				entry, err := ReadEntry(indexPath("omp", "", "real-id"))
				must(t, err)
				check(t, "get-facing ID", entry.ID, "real-id")
				check(t, "get-facing parent", entry.Parent, "")
				check(t, "original pointer source", entry.Source, target)
				if !reflect.DeepEqual(entry.ResumeArgv, []string{"omp", "--resume", target}) {
					t.Errorf("get-facing resume = %v", entry.ResumeArgv)
				}
				for _, artifact := range []struct {
					path, id, parent string
				}{
					{child, "Worker", "real-id"},
					{nested, "Nested", "worker-header"},
				} {
					s, err := readOmp(artifact.path, placement)
					must(t, err)
					check(t, "descendant basename", s.ID, artifact.id)
					check(t, "descendant parent", s.Parent, artifact.parent)
					id, parent := placement.identity(artifact.path)
					check(t, "descendant index ID", id, artifact.id)
					check(t, "descendant index parent", parent, artifact.parent)
					if len(s.ResumeArgv) != 0 {
						t.Errorf("artifact has resume argv: %v", s.ResumeArgv)
					}
					if id, err := placement.liveIdentity(artifact.path); err == nil {
						t.Errorf("artifact identifies live main: %q", id)
					}
				}
			})
		}
	}
}

func TestOmpSymlinkArtifactCanonicalParent(t *testing.T) {
	for _, mode := range []string{"flat root", "registered custom root"} {
		t.Run(mode, func(t *testing.T) {
			ompTestEnv(t)
			root := t.TempDir()
			alias := filepath.Join(t.TempDir(), "alias")
			must(t, os.Symlink(root, alias))
			parent := ompWrite(t, filepath.Join(alias, "arbitrary.jsonl"), ompTestHeader)
			child := ompWrite(t, filepath.Join(alias, "arbitrary", "Agent.jsonl"), strings.ReplaceAll(ompTestHeader, "real-id", "child-header-id"))
			if mode == "flat root" {
				t.Setenv("PI_CODING_AGENT_SESSION_DIR", alias)
			} else {
				ompWrite(t, filepath.Join(ompLocationFor("", os.Getenv).State, "custom-session-files", "custom"), parent)
			}
			placement := &ompPlacement{}
			for _, source := range []string{child, filepath.Join(root, "arbitrary", "Agent.jsonl")} {
				s, err := readOmp(source, placement)
				must(t, err)
				check(t, "canonical parent", s.Parent, "real-id")
				check(t, "artifact basename", s.ID, "Agent")
				if len(s.ResumeArgv) != 0 {
					t.Errorf("child %s has resume argv: %v", source, s.ResumeArgv)
				}
				if id, err := placement.liveIdentity(source); err == nil {
					t.Errorf("artifact %s identifies a live host: %q", source, id)
				}
			}
			ompCheckSources(t, parent, child)
			ompCheckResume(t, parent, "omp", "--resume", parent)
			id, err := placement.liveIdentity(filepath.Join(root, "arbitrary.jsonl"))
			must(t, err)
			check(t, "main live identity", id, "real-id")
		})
	}
}

func TestOmpSymlinkMissingMainClassification(t *testing.T) {
	for _, mode := range []string{"flat root", "registered custom root"} {
		t.Run(mode, func(t *testing.T) {
			ompTestEnv(t)
			t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
			root := t.TempDir()
			alias := filepath.Join(t.TempDir(), "alias")
			must(t, os.Symlink(root, alias))
			source := filepath.Join(alias, "main.jsonl")
			if mode == "flat root" {
				t.Setenv("PI_CODING_AGENT_SESSION_DIR", alias)
			} else {
				// Both the pointed transcript and its containing directory vanish.
				source = filepath.Join(alias, "removed", "main.jsonl")
				ompWrite(t, filepath.Join(ompLocationFor("", os.Getenv).State, "custom-session-files", "custom"), source)
			}
			ompWrite(t, source, ompTestHeader)
			cached, err := ReadOmp(source)
			must(t, err)
			dst := indexPath("omp", "", cached.ID)
			must(t, writeAtomic(dst, Render(cached), time.Now()))
			must(t, os.Remove(source))
			if mode == "registered custom root" {
				must(t, os.Remove(filepath.Dir(source)))
			}
			must(t, MarkMissing(dst))
			parent, artifact, err := (&ompPlacement{}).artifactParent(source)
			must(t, err)
			if artifact || parent != "" {
				t.Errorf("missing main misclassified: parent=%q, artifact=%v", parent, artifact)
			}
			e, err := ReadEntry(dst)
			must(t, err)
			if !e.SourceMissing || len(e.ResumeArgv) != 0 {
				t.Errorf("missing main not retained as searchable: %+v", e)
			}
			ompCheckSources(t)
		})
	}
}

func TestOmpDuplicateXDGSourcePrecedence(t *testing.T) {
	for _, profile := range []string{"", "review"} {
		t.Run("profile="+profile, func(t *testing.T) {
			home := ompTestEnv(t)
			t.Setenv("PI_CONFIG_DIR", "z-omp")
			t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
			legacyRoot := ompLocationFor(profile, os.Getenv).Sessions
			legacy := ompWrite(t, filepath.Join(legacyRoot, "bucket", "legacy.jsonl"), ompTestHeader)
			survivor := ompWrite(t, filepath.Join(legacyRoot, "bucket", "survivor.jsonl"), strings.ReplaceAll(ompTestHeader, "real-id", "legacy-only"))
			if !reconcileAll(false, false) {
				t.Fatal("initial reconcile failed")
			}
			info, err := os.Stat(legacy)
			must(t, err)
			xdgRoot := filepath.Join(home, "a-data", "omp")
			if profile != "" {
				xdgRoot = filepath.Join(xdgRoot, "profiles", profile)
			}
			current := ompWrite(t, filepath.Join(xdgRoot, "sessions", "bucket", "current.jsonl"), strings.ReplaceAll(ompTestHeader, "header auto", "current copy"))
			must(t, os.Chtimes(current, info.ModTime(), info.ModTime()))
			t.Setenv("XDG_DATA_HOME", filepath.Join(home, "a-data"))
			ompCheckSources(t, current, survivor)
			if !reconcileAll(false, false) {
				t.Fatal("migration reconcile failed")
			}
			e, err := ReadEntry(indexPath("omp", "", "real-id"))
			must(t, err)
			check(t, "selected current source", e.Source, current)
			check(t, "selected current title", e.Title, "current copy")
			want := []string{"omp", "--resume", "real-id"}
			if profile != "" {
				want = []string{"omp", "--profile", profile, "--resume", current}
			}
			if e.SourceMissing || !slices.Equal(e.ResumeArgv, want) {
				t.Fatalf("current resume=%v missing=%v, want %v", e.ResumeArgv, e.SourceMissing, want)
			}
			e, err = ReadEntry(indexPath("omp", "", "legacy-only"))
			must(t, err)
			check(t, "surviving legacy source", e.Source, survivor)
			want = []string{"omp", "--resume", survivor}
			if profile != "" {
				want = []string{"omp", "--profile", profile, "--resume", survivor}
			}
			if e.SourceMissing || !slices.Equal(e.ResumeArgv, want) {
				t.Fatalf("legacy resume=%v missing=%v, want %v", e.ResumeArgv, e.SourceMissing, want)
			}
		})
	}
}

func TestOmpDuplicatePointerAndFlatSourcePrecedence(t *testing.T) {
	home := ompTestEnv(t)
	t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
	managed := ompWrite(t, filepath.Join(ompLocationFor("", os.Getenv).Sessions, "z-managed", "main.jsonl"), ompTestHeader)
	must(t, indexFile(omp, []string{managed}, false, nil))
	flat := filepath.Join(home, "a-flat")
	t.Setenv("PI_CODING_AGENT_SESSION_DIR", flat)
	flatPath := ompWrite(t, filepath.Join(flat, "main.jsonl"), ompTestHeader)
	selected := ompWrite(t, filepath.Join(home, "a-custom", "main.jsonl"), ompTestHeader)
	marker := ompWrite(t, filepath.Join(ompLocationFor("review", os.Getenv).State, "custom-session-files", "selected"), selected)
	ompCheckSources(t, selected)
	if !reconcileAll(false, false) {
		t.Fatal("pointer reconcile failed")
	}
	e, err := ReadEntry(indexPath("omp", "", "real-id"))
	must(t, err)
	check(t, "pointer-selected source", e.Source, selected)
	want := []string{"omp", "--profile", "review", "--resume", selected}
	if e.SourceMissing || !slices.Equal(e.ResumeArgv, want) {
		t.Fatalf("pointer resume=%v missing=%v, want %v", e.ResumeArgv, e.SourceMissing, want)
	}
	must(t, os.Remove(marker))
	ompCheckSources(t, flatPath)
	if !reconcileAll(false, false) {
		t.Fatal("flat-root reconcile failed")
	}
	e, err = ReadEntry(indexPath("omp", "", "real-id"))
	must(t, err)
	check(t, "flat-selected source", e.Source, flatPath)
	want = []string{"omp", "--resume", flatPath}
	if e.SourceMissing || !slices.Equal(e.ResumeArgv, want) {
		t.Fatalf("flat resume=%v missing=%v, want %v", e.ResumeArgv, e.SourceMissing, want)
	}
}

func TestOmpDuplicateSymlinkSourceIdentity(t *testing.T) {
	ompTestEnv(t)
	t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
	root := t.TempDir()
	t.Setenv("PI_CODING_AGENT_SESSION_DIR", root)
	target := ompWrite(t, filepath.Join(root, "z-main.jsonl"), ompTestHeader)
	alias := filepath.Join(root, "a-main.jsonl")
	must(t, os.Symlink(target, alias))
	ompCheckSources(t, alias)
	if !reconcileAll(false, false) {
		t.Fatal("alias reconcile failed")
	}
	e, err := ReadEntry(indexPath("omp", "", "real-id"))
	must(t, err)
	check(t, "lexical same-tier source", e.Source, alias)
	if !slices.Equal(e.ResumeArgv, []string{"omp", "--resume", alias}) {
		t.Fatalf("alias resume = %v", e.ResumeArgv)
	}
	ompWrite(t, filepath.Join(ompLocationFor("review", os.Getenv).State, "custom-session-files", "selected"), target)
	ompCheckSources(t, target)
	if !reconcileAll(false, false) {
		t.Fatal("exact-target reconcile failed")
	}
	e, err = ReadEntry(indexPath("omp", "", "real-id"))
	must(t, err)
	check(t, "original pointer source", e.Source, target)
	if !slices.Equal(e.ResumeArgv, []string{"omp", "--profile", "review", "--resume", target}) {
		t.Fatalf("exact pointer resume = %v", e.ResumeArgv)
	}
}

func TestOmpSameArtifactBasenameDifferentParents(t *testing.T) {
	ompTestEnv(t)
	t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
	root := t.TempDir()
	t.Setenv("PI_CODING_AGENT_SESSION_DIR", root)
	var sources []string
	for _, parentID := range []string{"parent-a", "parent-b"} {
		parent := ompWrite(t, filepath.Join(root, "arbitrary-"+parentID+".jsonl"), strings.ReplaceAll(ompTestHeader, "real-id", parentID))
		child := ompWrite(t, filepath.Join(strings.TrimSuffix(parent, ".jsonl"), "Agent.jsonl"), strings.ReplaceAll(ompTestHeader, "real-id", "same-child-header"))
		sources = append(sources, parent, child)
	}
	ompCheckSources(t, sources...)
	if !reconcileAll(false, false) {
		t.Fatal("artifact reconcile failed")
	}
	for i, parentID := range []string{"parent-a", "parent-b"} {
		e, err := ReadEntry(indexPath("omp", parentID, "Agent"))
		must(t, err)
		check(t, "child source", e.Source, sources[2*i+1])
		check(t, "canonical immediate parent", e.Parent, parentID)
		check(t, "child basename identity", e.ID, "Agent")
		if e.SourceMissing || len(e.ResumeArgv) != 0 {
			t.Fatalf("child missing=%v resume=%v", e.SourceMissing, e.ResumeArgv)
		}
		e, err = ReadEntry(indexPath("omp", "", parentID))
		must(t, err)
		check(t, "parent source", e.Source, sources[2*i])
		if e.SourceMissing || !slices.Equal(e.ResumeArgv, []string{"omp", "--resume", sources[2*i]}) {
			t.Fatalf("parent missing=%v resume=%v", e.SourceMissing, e.ResumeArgv)
		}
	}
}
