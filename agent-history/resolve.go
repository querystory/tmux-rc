package main

import (
	"cmp"
	"encoding/json"
	"errors"
	"math"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"
	"unicode"
)

// Entry is an index entry read back from disk: its front matter plus the body text.
type Entry struct {
	Harness       string   `json:"harness"`
	ID            string   `json:"session_id"`
	Parent        string   `json:"parent_session,omitempty"`
	Source        string   `json:"source"`
	SourceMissing bool     `json:"source_missing,omitempty"`
	Cwd           string   `json:"cwd"`
	Branches      []string `json:"branches,omitempty"`
	Entrypoint    string   `json:"entrypoint,omitempty"`
	Title         string   `json:"title,omitempty"`
	LastActive    string   `json:"last_active"`
	PRs           []string `json:"prs,omitempty"`
	ResumeArgv    []string `json:"resume_argv,omitempty"`
	Resume        string   `json:"resume,omitempty"`
	Path          string   `json:"entry"`
	named, body   string   // lowercased once for scoring
}

// ReadEntry parses the format Render writes. Each front-matter value is JSON, so the
// header decodes by assembling it into one object.
func ReadEntry(path string) (Entry, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Entry{}, err
	}
	rest, opened := strings.CutPrefix(string(data), "---\n")
	header, body, closed := strings.Cut(rest, "\n---\n")
	if !opened || !closed { // entries are written whole, so this one is damaged or not ours
		return Entry{}, errors.New("no front matter")
	}
	fields := map[string]json.RawMessage{}
	for line := range strings.Lines(header) {
		if key, value, ok := strings.Cut(strings.TrimSpace(line), ": "); ok {
			fields[key] = json.RawMessage(value)
		}
	}
	obj, _ := json.Marshal(fields)
	e := Entry{Path: path}
	err = json.Unmarshal(obj, &e)
	if e.SourceMissing { // the harness deleted it: still findable, no longer resumable
		e.ResumeArgv, e.Resume = nil, ""
	} else if e.ResumeArgv == nil && e.Resume != "" { // format 1, until reconcile rebuilds it
		e.ResumeArgv = claudeResume(e.ID)
	}
	e.named = normalize(e.Title + " " + strings.Join(e.Branches, " ") + " " + strings.Join(e.PRs, " "))
	e.body = normalize(body)
	return e, err
}

type Project struct {
	Repo     string   `json:"repo"`
	Score    float64  `json:"score"`
	Sessions []Scored `json:"sessions"`
}

type Scored struct {
	Entry
	Score   float64  `json:"score"`
	Running *Running `json:"running,omitempty"` // set when a process has it open now
	// RunningUnknown: the live-process registry couldn't be read, so no Running here
	// does not mean it's stopped. Callers must not resume on that basis.
	RunningUnknown bool `json:"running_unknown,omitempty"`
}

type ResolveOptions struct {
	Harness     string // empty means any
	All         bool   // include headless runs and subagents
	MaxProjects int
	MaxSessions int
	Now         time.Time
	Running     map[string]Running // by session ID; see RunningClaude
	RunningErr  error              // set when Running could not be determined
}

// Resolve ranks where a request like "fix live mode" most likely belongs: repos, and
// within each the sessions to resume. It reads only the index, never calls a model,
// and never touches a session; acting on the answer is the caller's decision.
func Resolve(entries []Entry, query string, opt ResolveOptions) []Project {
	terms := terms(query)
	entries = slices.DeleteFunc(slices.Clone(entries), func(e Entry) bool {
		return (opt.Harness != "" && e.Harness != opt.Harness) ||
			(!opt.All && (e.Parent != "" || e.Entrypoint == "sdk-cli"))
	})
	weights := idf(entries, terms)

	byRepo := map[string]*Project{}
	for _, e := range entries {
		score := relevance(e, weights) * recency(e.LastActive, opt.Now)
		if score == 0 {
			continue
		}
		repo := repoOf(e.Cwd)
		if byRepo[repo] == nil {
			byRepo[repo] = &Project{Repo: repo}
		}
		p := byRepo[repo]
		scored := Scored{Entry: e, Score: score, RunningUnknown: opt.RunningErr != nil}
		if r, ok := opt.Running[e.ID]; ok {
			scored.Running = &r
		}
		p.Sessions = append(p.Sessions, scored)
	}
	projects := []Project{}
	for _, p := range byRepo {
		slices.SortFunc(p.Sessions, func(a, b Scored) int { return cmp.Compare(b.Score, a.Score) })
		// A repo ranks by its best few sessions, not its volume: a busy repo with many
		// passing mentions shouldn't beat the one where the work actually happened.
		for _, s := range p.Sessions[:min(len(p.Sessions), 3)] {
			p.Score += s.Score
		}
		p.Sessions = p.Sessions[:min(len(p.Sessions), max(opt.MaxSessions, 0))]
		projects = append(projects, *p)
	}
	slices.SortFunc(projects, func(a, b Project) int { return cmp.Or(cmp.Compare(b.Score, a.Score), strings.Compare(a.Repo, b.Repo)) })
	projects = projects[:min(len(projects), max(opt.MaxProjects, 0))]
	// Round only after ranking and selecting projects; tiny differences still decide.
	for i := range projects {
		projects[i].Score = round(projects[i].Score)
		for j := range projects[i].Sessions {
			projects[i].Sessions[j].Score = round(projects[i].Sessions[j].Score)
		}
	}
	return projects
}

// idf weights each query term by how rare it is across sessions, so a word like "fix"
// that appears everywhere barely counts and "live" or "otlp" decide the answer.
func idf(entries []Entry, terms []string) map[string]float64 {
	weights := map[string]float64{}
	for _, t := range terms {
		df := 0
		for _, e := range entries {
			if strings.Contains(e.named, " "+t+" ") || strings.Contains(e.body, " "+t+" ") {
				df++
			}
		}
		if df > 0 {
			weights[t] = math.Log(1 + float64(len(entries))/float64(df))
		}
	}
	return weights
}

// relevance counts term hits in what the human said, weighting the session's name and
// its branches and PRs more, since those were chosen to describe the work.
func relevance(e Entry, weights map[string]float64) float64 {
	score := 0.0
	for t, w := range weights {
		// Hits saturate so one long session repeating a word doesn't bury the rest.
		hits := min(strings.Count(e.body, " "+t+" "), 5)
		if strings.Contains(e.named, " "+t+" ") {
			hits += 5
		}
		score += w * float64(hits)
	}
	return score
}

// recency halves a session's weight every two weeks, so the latest work on a topic
// ranks first without old work disappearing.
func recency(lastActive string, now time.Time) float64 {
	t, err := time.Parse(time.RFC3339, lastActive)
	if err != nil {
		return 0.5
	}
	// Clock skew can put a session in the future; that is "now", not a bonus.
	return math.Exp2(-max(now.Sub(t).Hours(), 0) / (24 * 14))
}

// terms are the query's words plus each adjacent pair as a phrase. A phrase is rarer
// than its words, so "live mode" outweighs sessions that merely say "live" and "mode".
func terms(query string) []string {
	words := strings.Fields(normalize(query))
	out := slices.Clone(words)
	for i := 1; i < len(words); i++ {
		out = append(out, words[i-1]+" "+words[i])
	}
	slices.Sort(out)
	return slices.Compact(out)
}

// normalize lowercases and turns every run of non-alphanumerics into one space, so
// "live-mode", "Live Mode" and "live_mode" all match the phrase "live mode". The
// padding makes every word, first and last included, space-delimited.
func normalize(text string) string {
	fields := strings.FieldsFunc(strings.ToLower(text), func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsDigit(r)
	})
	return " " + strings.Join(fields, " ") + " "
}

// repoOf maps a session's cwd to the repository it belongs to, folding git worktrees
// into their main repo so a feature worked across PR worktrees groups as one project.
// A cwd that no longer exists (a removed worktree) falls back to its nearest ancestor.
func repoOf(cwd string) string {
	for dir := cwd; dir != "/" && dir != "."; dir = filepath.Dir(dir) {
		info, err := os.Stat(filepath.Join(dir, ".git"))
		if err != nil {
			continue
		}
		if info.IsDir() {
			// A stray empty .git directory isn't a repo; a real one always has HEAD.
			if _, err := os.Stat(filepath.Join(dir, ".git", "HEAD")); err == nil {
				return dir
			}
			continue
		}
		// A worktree's .git file reads "gitdir: <main>/.git/worktrees/<name>".
		if data, err := os.ReadFile(filepath.Join(dir, ".git")); err == nil {
			gitdir := strings.TrimSpace(strings.TrimPrefix(string(data), "gitdir:"))
			if main, _, ok := strings.Cut(gitdir, "/.git/worktrees/"); ok {
				return main
			}
		}
		return dir
	}
	return cwd
}

func round(f float64) float64 { return math.Round(f*100) / 100 }
