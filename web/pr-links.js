// PR associations and the session's scratch `pages` outlive the visible terminal frame.
// Always offer them alongside transient parser links, without duplicating the same URL.
export function paneLinks(pane, pages = []) {
  const tracked = (Array.isArray(pane?.prs) ? pane.prs : []).flatMap((pr) => {
    if (!pr || typeof pr.repo !== "string" ||
        pr.repo.length > 256 ||
        !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(pr.repo) ||
        pr.repo.split("/").some((part) => part === "." || part === "..") ||
        !Number.isSafeInteger(pr.number) || pr.number <= 0) return [];
    const ref = `${pr.repo}#${pr.number}`;
    const title = typeof pr.title === "string" && pr.title.trim();
    return [{ href: `https://github.com/${pr.repo}/pull/${pr.number}`,
      text: title || ref, ...(title ? { detail: ref } : {}) }];
  });
  const seen = new Set();
  const accept = (link) => {
    try {
      const url = new URL(link.href);
      if (!/^https?:$/.test(url.protocol)) return false;
      const key = url.href.replace(/\/$/, "");
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    } catch { return false; }
  };
  const kept = [tracked, pages].flatMap((list) => list.slice(0, 64).filter(accept)); // a cap each
  const transient = (Array.isArray(pane?.links) ? pane.links : [])
    .filter(accept).slice(0, 3).map(({ href, text }) => ({ href, text }));
  return [...kept, ...transient];
}

// The link a question asks the user to open (a sign-in or device-login URL): the screen
// link the parser names as the question's own (question.link), else the first link the
// screen offers, since an asking pane's links are what it asks you to open.
export function askLink(pane) {
  const href = pane?.question?.link, links = paneLinks({ links: pane?.links });
  return (href ? links.find((link) => link.href === href) : links[0]) || null;
}
