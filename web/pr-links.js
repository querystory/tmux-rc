// PR associations outlive the visible terminal frame. Always offer their GitHub
// destinations alongside transient parser links, without duplicating the same PR.
export function paneLinks(pane) {
  const tracked = (Array.isArray(pane?.prs) ? pane.prs : []).flatMap((pr) => {
    if (!pr || typeof pr.repo !== "string" ||
        !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(pr.repo) ||
        pr.repo.split("/").some((part) => part === "." || part === "..") ||
        !Number.isSafeInteger(pr.number) || pr.number <= 0) return [];
    return [{ href: `https://github.com/${pr.repo}/pull/${pr.number}`, text: `${pr.repo}#${pr.number}` }];
  });
  const seen = new Set();
  return [...tracked, ...(Array.isArray(pane?.links) ? pane.links : [])].filter((link) => {
    try {
      const url = new URL(link.href);
      if (!/^https?:$/.test(url.protocol)) return false;
      const key = url.href.replace(/\/$/, "");
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    } catch { return false; }
  });
}
