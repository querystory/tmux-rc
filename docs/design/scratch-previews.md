# Scratch Previews

Status: implemented (`/scratch/` in `openbus/server.py`, opt-in via `TMUXRC_SCRATCH_DIR`).
How to use it: [Previewing work](../deploy/previews.md).

## Why

Agents produce things a person has to look at: a mock, a built site, a report. The person
is often on the phone, where "open this file" means nothing. The habit that grew up was a
quick static server plus a second tunnel client per preview. Those clients authenticated
with the developer's interactive login, which the organization's reauthentication policy
expires about once a day, so previews died overnight while the main tunnel, running on a
service-account credential, stayed up. Nobody was watching an unattended preview to notice.

The daemon already has a front door that is authenticated, supervised and on the phone.
Serving previews through it removes a moving part instead of adding one.

## Decisions

**Static files only.** A directory of files covers every case that actually came up:
mocks, `dist/` folders, reports, images, PDFs. Starlette's static mount already refuses
traversal and serves no listings, so the feature is a few lines on a code path `/docs`
already uses, not new surface.

**Opt-in, with no default directory.** Whatever sits in the directory is published to
everyone the front door admits. A default path (say under `~/.local/share`) would mean a
file saved there for an unrelated reason gets published by an upgrade nobody read the notes
for. Making the operator name the directory makes publishing a deliberate act.

**Sandboxed by CSP rather than trusted.** Previews are arbitrary HTML, often written by an
agent, served from the daemon's own origin, where a script could call the API that types
into terminals, carrying the viewer's session. A `sandbox` directive without
`allow-same-origin` moves the page into an opaque origin, so that API becomes cross-origin
to it, as it is to any website. Cross-origin only protects responses, though: a form post
or a no-cors fetch still delivers the request, with the front door's cookie. So
`form-action` and `connect-src` are `'none'` too. The price is that pages needing a backend
or remote data do not work. That is the right trade for a review tool; such pages have a
documented escape hatch.

**Committed mocks live in the docs site.** A mock that justifies a PR belongs with the PR,
so `docs-site/static/mocks/` is the home for the ones worth keeping, and scratch stays
explicitly disposable. Two places by lifetime keeps the scratch dir free to be wiped.

## Alternatives rejected

**A second tunnel per preview.** The status quo. Each one is another credential, process
and hostname to keep alive, and the credential is the part that kept failing. It stays
documented as the escape hatch for previews that genuinely need their own hostname, with
the fix applied: reuse the tunnel service's self-refreshing credential.

**A reverse proxy to local dev servers.** It would preview live apps, not just builds, but
it would put any local app and its unauthenticated API on the daemon's origin and behind
its login, where the CSP sandbox cannot apply without breaking the app. Dev servers also
expect to own the site root and need WebSockets for hot reload, so a path-prefixed proxy
half-works. It turns tmux-rc into a general gateway, a much larger thing to secure than a
read-only file mount.

**A default scratch directory.** Rejected above: convenience at the cost of accidental
publication.
