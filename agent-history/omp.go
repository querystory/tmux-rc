package main

import (
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"time"
)

// Storage and record contracts follow oh-my-pi's session-loader.ts,
// session-paths.ts, session-entries.ts and utils/src/dirs.ts.
type ompRecord struct {
	Type      string `json:"type"`
	ID        string `json:"id"`
	Timestamp string `json:"timestamp"`
	Cwd       string `json:"cwd"`
	Title     string `json:"title"`
	UpdatedAt string `json:"updatedAt"`
	Task      string `json:"task"`
	Message   struct {
		Role        string `json:"role"`
		Attribution string `json:"attribution"`
		Synthetic   bool   `json:"synthetic"`
	} `json:"message"`
}

func ompHeader(path string) (ompRecord, error) {
	var header ompRecord
	err := scanLines(path, func(line []byte) error {
		var r ompRecord
		if json.Unmarshal(line, &r) != nil || r.Type == "title" {
			return nil
		}
		if r.Type == "session" {
			header = r
		}
		return io.EOF
	})
	if errors.Is(err, io.EOF) {
		err = nil
	}
	return header, err
}

// Artifact directories are the transcript basename without .jsonl. A sibling
// header supplies the canonical parent ID even after relocation or renaming.
// The timestamp_ID fallback keeps orphaned artifact transcripts discoverable.
var ompArtifactName = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T[^_]+_(.+)$`)

func ompArtifactParent(path string) string {
	dir := filepath.Dir(path)
	if h, err := ompHeader(dir + ".jsonl"); err == nil && h.ID != "" {
		return h.ID
	}
	if m := ompArtifactName.FindStringSubmatch(filepath.Base(dir)); m != nil {
		return m[1]
	}
	return ""
}

func ompIdentity(path string) (id, parent string) {
	if parent = ompArtifactParent(path); parent != "" {
		return strings.TrimSuffix(filepath.Base(path), ".jsonl"), parent
	}
	h, _ := ompHeader(path)
	return h.ID, ""
}

func ReadOmp(path string) (Session, error) {
	s := Session{Harness: "omp", Source: path}
	s.Parent = ompArtifactParent(path)
	if s.Parent != "" {
		s.ID = strings.TrimSuffix(filepath.Base(path), ".jsonl")
	}
	var header ompRecord
	var slotTitle string
	var hasSlot, taskSeen bool
	lineNumber := 0
	latest := time.Time{}
	observe := func(stamp string) {
		if t, err := time.Parse(time.RFC3339Nano, stamp); err == nil && (latest.IsZero() || t.After(latest)) {
			latest, s.LastActive = t, stamp
		}
	}
	err := scanLines(path, func(line []byte) error {
		lineNumber++
		var r ompRecord
		if json.Unmarshal(line, &r) != nil {
			return nil
		} // tolerate crash-truncated tails
		observe(r.Timestamp)
		switch r.Type {
		case "title":
			if lineNumber == 1 && ompValidTitleSlot(line) {
				hasSlot, slotTitle = true, r.Title
				observe(r.UpdatedAt)
			}
		case "session":
			if header.Type == "" {
				header = r
				s.Cwd, s.Started = r.Cwd, r.Timestamp
				s.Title = r.Title
				if s.Parent == "" {
					s.ID = r.ID
				}
			}
		case "session_init":
			if s.Parent != "" && !taskSeen {
				taskSeen = true
				if text := strings.TrimSpace(r.Task); text != "" {
					s.Messages = append(s.Messages, Message{r.Timestamp, "prompt", text})
				}
			}
		case "message":
			if r.Message.Role == "user" && !r.Message.Synthetic && r.Message.Attribution != "agent" {
				var body struct {
					Message struct {
						Content json.RawMessage `json:"content"`
					} `json:"message"`
				}
				if json.Unmarshal(line, &body) == nil {
					s.said(r.Timestamp, "user", body.Message.Content)
				}
			}
		}
		return nil
	})
	if err != nil {
		return Session{}, err
	}
	if header.Type == "" || header.ID == "" {
		return Session{}, errNotIndexed
	}
	if hasSlot {
		s.Title = slotTitle
	}
	for _, m := range s.Messages {
		for _, pr := range ompPR.FindAllString(m.Text, -1) {
			if !slices.Contains(s.PRs, pr) {
				s.PRs = append(s.PRs, pr)
			}
		}
	}
	if s.Parent == "" && s.Cwd != "" {
		absolute, err := filepath.Abs(path)
		if err != nil {
			return Session{}, err
		}
		arg, profile := absolute, ""
		knownProfile := false
		active := ompActiveProfile()
		locations, err := ompLocations(active)
		if err != nil {
			return Session{}, err
		}
		for _, loc := range locations {
			if ompWithin(loc.Sessions, absolute) {
				profile, knownProfile = loc.Profile, true
				if loc == locations[0] && (active == "" || active == "default") {
					arg = s.ID
				}
				break
			}
		}
		if !knownProfile {
			for _, loc := range locations {
				matched := false
				if err := ompPointers(loc, func(path string) { matched = matched || path == absolute }); err != nil {
					return Session{}, err
				}
				if matched {
					profile, knownProfile = loc.Profile, true
					break
				}
			}
		}
		if profile == "" && active != "" && active != "default" {
			profile = active
			if knownProfile {
				profile = "default"
			}
		}
		s.ResumeArgv = []string{"omp"}
		if profile != "" {
			s.ResumeArgv = append(s.ResumeArgv, "--profile", profile)
		}
		s.ResumeArgv = append(s.ResumeArgv, "--resume", arg)
	}
	return s, nil
}
func ompValidTitleSlot(line []byte) bool {
	var slot struct {
		Type      string  `json:"type"`
		V         int     `json:"v"`
		Title     *string `json:"title"`
		UpdatedAt *string `json:"updatedAt"`
		Pad       *string `json:"pad"`
		Source    *string `json:"source"`
	}
	return json.Unmarshal(line, &slot) == nil && slot.Type == "title" && slot.V == 1 &&
		slot.Title != nil && slot.UpdatedAt != nil && slot.Pad != nil &&
		(slot.Source == nil || *slot.Source == "auto" || *slot.Source == "user")
}

var ompPR = regexp.MustCompile(`https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+`)

type ompLocation struct{ Sessions, State, Profile string }

func ompWithin(root, path string) bool {
	rel, err := filepath.Rel(root, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

func ompProfileEnv(env map[string]string) string {
	if p, ok := env["OMP_PROFILE"]; ok {
		return strings.TrimSpace(p)
	}
	return strings.TrimSpace(env["PI_PROFILE"])
}

func ompActiveProfile() string {
	profile, ok := os.LookupEnv("OMP_PROFILE")
	if !ok {
		profile = os.Getenv("PI_PROFILE")
	}
	return strings.TrimSpace(profile)
}

func ompLocationFor(profile string, getenv func(string) string) ompLocation {
	if profile == "default" {
		profile = ""
	}
	home := getenv("HOME")
	if home == "" {
		home, _ = os.UserHomeDir()
	}
	config := getenv("PI_CONFIG_DIR")
	if config == "" {
		config = ".omp"
	}
	base := filepath.Join(home, config)
	if profile != "" {
		base = filepath.Join(base, "profiles", profile)
	}
	agent := filepath.Join(base, "agent")
	override := getenv("PI_CODING_AGENT_DIR")
	if profile == "" && override != "" {
		agent, _ = filepath.Abs(override)
	}
	data, state := agent, agent
	if agent == filepath.Join(base, "agent") {
		for _, category := range []struct {
			env    string
			target *string
		}{{"XDG_DATA_HOME", &data}, {"XDG_STATE_HOME", &state}} {
			if x := getenv(category.env); x != "" {
				root := filepath.Join(x, "omp")
				if profile != "" {
					root = filepath.Join(root, "profiles", profile)
				}
				if info, err := os.Stat(root); err == nil && info.IsDir() {
					*category.target = root
				}
			}
		}
	}
	return ompLocation{filepath.Join(data, "sessions"), state, profile}
}

func ompStateDir(env map[string]string, profile string) string {
	return ompLocationFor(profile, func(key string) string { return env[key] }).State
}

func ompLocations(profile string) ([]ompLocation, error) {
	var out []ompLocation
	home, _ := os.UserHomeDir()
	config := os.Getenv("PI_CONFIG_DIR")
	if config == "" {
		config = ".omp"
	}
	// Retain pre-migration/default roots as well as today's effective roots.
	legacyEnv := func(key string) string {
		if key == "XDG_DATA_HOME" || key == "XDG_STATE_HOME" || key == "PI_CODING_AGENT_DIR" {
			return ""
		}
		return os.Getenv(key)
	}
	profiles := []string{""}
	for _, root := range []string{filepath.Join(home, config, "profiles"), filepath.Join(os.Getenv("XDG_DATA_HOME"), "omp", "profiles"), filepath.Join(os.Getenv("XDG_STATE_HOME"), "omp", "profiles")} {
		if !filepath.IsAbs(root) {
			continue
		}
		entries, err := os.ReadDir(root)
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
		for _, e := range entries {
			if e.IsDir() && !slices.Contains(profiles, e.Name()) {
				profiles = append(profiles, e.Name())
			}
		}
	}
	if profile != "" && !slices.Contains(profiles, profile) {
		profiles = append(profiles, profile)
	}
	for _, p := range profiles {
		out = append(out, ompLocationFor(p, os.Getenv))
		legacy := ompLocationFor(p, legacyEnv)
		if !slices.Contains(out, legacy) {
			out = append(out, legacy)
		}
	}
	return out, nil
}

// Discovery reads only sessions, artifact transcripts, and exact pointers in
// omp's custom-file registry/breadcrumbs; blobs, logs and results are excluded.
func ompSessions() ([][]string, error) {
	locations, err := ompLocations(ompActiveProfile())
	if err != nil {
		return nil, err
	}
	var paths []string
	var errs []error
	addTree := func(root string) {
		found, err := find(root, ".jsonl", 1, 2, 3)
		paths = append(paths, found...)
		errs = append(errs, err)
	}
	for _, loc := range locations {
		addTree(loc.Sessions)
		errs = append(errs, ompPointers(loc, func(path string) {
			paths = append(paths, path)
			addTree(strings.TrimSuffix(path, ".jsonl"))
		}))
	}
	if root := os.Getenv("PI_CODING_AGENT_SESSION_DIR"); root != "" {
		addTree(root)
	}
	slices.Sort(paths)
	paths = slices.Compact(paths)
	var out [][]string
	for _, path := range paths {
		if strings.HasPrefix(filepath.Base(path), "__") {
			continue
		}
		h, err := ompHeader(path)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			errs = append(errs, err)
			continue
		}
		if h.ID == "" {
			continue
		}
		out = append(out, []string{path})
	}
	return out, errors.Join(errs...)
}

// Pointer reads retain partial discovery results alongside I/O errors.
func ompPointers(loc ompLocation, visit func(string)) error {
	var errs []error
	for _, kind := range []string{"custom-session-files", "terminal-sessions"} {
		markers, err := find(filepath.Join(loc.State, kind), "", 1)
		errs = append(errs, err)
		for _, marker := range markers {
			b, err := os.ReadFile(marker)
			if err != nil {
				errs = append(errs, err)
				continue
			}
			if path := ompMarkerPath(kind, b); path != "" {
				visit(path)
			}
		}
	}
	return errors.Join(errs...)
}

func ompMarkerPath(kind string, b []byte) string {
	lines := strings.Split(strings.TrimSpace(string(b)), "\n")
	path := lines[0]
	if kind == "terminal-sessions" {
		if len(lines) < 2 || lines[0] == "" || lines[1] == "" {
			return ""
		}
		path = lines[1]
		if !filepath.IsAbs(path) {
			path = filepath.Join(lines[0], path)
		}
	}
	if !filepath.IsAbs(path) || !strings.HasSuffix(path, ".jsonl") {
		return ""
	}
	return filepath.Clean(path)
}
