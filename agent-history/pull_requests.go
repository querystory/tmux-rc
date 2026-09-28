package main

import (
	"context"
	"os/exec"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
)

var (
	pullURL     = regexp.MustCompile(`https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([0-9]+)`)
	pullMention = regexp.MustCompile(`(?i)\bpr\s*#?\s*([0-9]+)\b`)
)

// addPullRequests records canonical GitHub pull URLs. URLs can appear anywhere in a
// transcript (most often `gh pr create` output); shorthand such as "PR 4955" is only
// trusted in a human message and is resolved against that session's origin remote.
// One session may name several PRs, and one PR may naturally occur in several sessions.
func (s *Session) addPullRequests(text string, human bool) {
	for _, match := range pullURL.FindAllStringSubmatch(text, -1) {
		s.addPullRequest(match[1], match[2], match[3])
	}
	if !human {
		return
	}
	repo := ""
	for _, match := range pullMention.FindAllStringSubmatch(text, -1) {
		if repo == "" {
			repo = githubOrigin(s.Cwd)
		}
		if owner, name, ok := strings.Cut(repo, "/"); ok {
			s.addPullRequest(owner, name, match[1])
		}
	}
}

func (s *Session) addPullRequest(owner, repo, number string) {
	n, err := strconv.ParseUint(number, 10, 64)
	if err != nil || n == 0 {
		return
	}
	url := "https://github.com/" + owner + "/" + repo + "/pull/" + strconv.FormatUint(n, 10)
	if !slices.Contains(s.PRs, url) {
		s.PRs = append(s.PRs, url)
	}
}

// githubOrigin resolves only local git configuration; git does not contact GitHub for
// `remote get-url`. Keep the timeout anyway so a broken git installation cannot stall
// a Stop hook. Empty means an unrecognized/non-GitHub origin, so shorthand is ignored.
func githubOrigin(cwd string) string {
	if cwd == "" {
		return ""
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "git", "-C", cwd, "remote", "get-url", "origin").Output()
	if err != nil {
		return ""
	}
	remote := strings.TrimSpace(string(out))
	for _, prefix := range []string{
		"git@github.com:", "ssh://git@github.com/", "https://github.com/", "http://github.com/",
	} {
		if path, ok := strings.CutPrefix(remote, prefix); ok {
			path = strings.TrimSuffix(path, ".git")
			if strings.Count(path, "/") == 1 {
				return path
			}
		}
	}
	return ""
}
