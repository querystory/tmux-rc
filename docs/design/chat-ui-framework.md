# Should Chat adopt assistant-ui?

Evaluation, with a recommendation: **do not adopt now; cure the papercut class with one
shared vanilla transcript module, and revisit on the triggers below.** Related:
[live-mode.md](live-mode.md) (the chat features and why they are shaped as they are),
[control-session-routing.md](control-session-routing.md).

## The problem

Chat has produced a steady trickle of small UI fixes: the divider under the paperclip
(#276), the input growing when an image is pasted and the missing close button (#281), the
attach button picking one file, End Chat leaving the sheet open, the empty-reply
placeholder, the 16px iOS zoom rule, overflow at 390px. The question is whether a
chat framework would have stopped them, or whether they come from somewhere a framework
does not reach.

Sorting them (details in the table) gives two causes. About two of seven come from our
**composer**, a contenteditable with inline image chips shared with the pane composer.
The rest come from **our own layout and state decisions** (what the header buttons do,
when the sheet closes, what an empty reply means) and from **CSS on a phone**. A headless
library cannot make those decisions for us.

## What assistant-ui is (from the source, not memory)

MIT-licensed, React only (peer range 18 or 19), pre-1.0 (react package 0.15.x). The
runtime core is framework-agnostic, but the Vue and Svelte bindings are unpublished
version-0.0.0 previews, so there is no vanilla path today. It offers headless primitives
(thread, viewport, composer, message, attachment, action bar, branch picker, scroll to
bottom), several runtimes, and two ways to style: the primitives unstyled, or Tailwind and
shadcn registry components copied into the app. Tool calls render as components, and the
tool-part props include a result setter and a server-approval gate, which is the natural
home for our consent cards. Attachments go through an adapter with a "multiple" default.
Markdown and syntax highlighting are separate optional packages.

Health: very active (at least a hundred package releases in September 2026 alone, 12k stars,
405 open issues). That is a strength for fixes and a cost for a repo that has to track it:
the API is visibly mid-migration (a "use generative" toolkit directive beside older
helpers, many `unstable_` names). Open issues relevant to us: the Enter that confirms an
IME conversion sends the message in Safari (#8199, #8319), scroll-to-bottom stays hidden
after a short scroll (#8211), and a run that starts elsewhere pulls focus into the
composer (#8223), which on a phone raises the keyboard unasked. Nothing in the library
handles the iOS keyboard or standalone viewport: its own composer relies on a 16px font
and `dvh` like ours. Accessibility is claimed in the README but the primitives gave no
log role or composer label by default in the spike; we would add them.

## What was measured (spike, not product code)

A throwaway island was built outside the tree: thread, viewport, composer with
multi-image attachment, and one tool UI for the consent card, fed from a stand-in for the
`/api/live-mode` protocol through the external-store runtime, skinned with the phone's
design tokens. Versions: @assistant-ui/react 0.15.22 (core 0.3.21), React and React DOM 19.3.0, esbuild 0.28.2 bundling to one ESM file with minify on and NODE_ENV production defined; sizes are of that file, with gzip at level 9 and brotli at quality 11 (the spike lived in a scratch directory and is not committed). Screenshots are in the PR.

| | JS min | gzip | brotli | CSS min |
|---|---|---|---|---|
| Spike island (React, React DOM, assistant-ui) | 554 KB | 170 KB | 144 KB | 2.2 KB |
| Our chat JS today (live-chat, composer, phone live) | 19 KB | 8 KB | 7 KB | 1.7 KB |
| deep-chat 2.5.1 (prebuilt bundle, not spiked) | 387 KB | 107 KB | 89 KB | in JS |
| ECharts, already vendored | 1.1 MB | 368 KB | n/a | n/a |

React DOM is 205 KB of the island; assistant-ui and its core, store, tap and stream
packages are about 310 KB. The island is roughly 29 times our chat's code. That is
tolerable beside ECharts, but the production dependency tree is about 100 packages
(mostly the Radix meta package), against four today (ECharts, wordcloud and their two
transitive packages).

It works: in headless Chromium at 390x844 (dark and light) and 1440x900 it streamed text,
accepted two images from one picker call, kept the input at 44px with both attached (the
composer is a textarea, so the pasted-image jump cannot occur), showed the consent card
with its thumbnail, sent Approve over the socket from the result setter, and showed the
decision. No horizontal overflow, 16px input, no console errors. **Not measured:** real
iOS Safari, the keyboard and standalone viewport, the bottom-sheet dialog's focus
handling, minimize, the desktop card, voice, and a screen reader. The spike also showed
what "headless" costs: the stock attachment thumb renders the file extension, not the
image, so image chips needed our own component, and the tool part nested inside a message
bubble until restyled.

## How our features map

Streaming text, the transcript and the consent card map cleanly: the daemon's transcript
frames become message growth, a propose frame becomes a tool call awaiting a result, and
the decided frame fills it in. The "echo, never optimistic" rule survives because the
runtime only shows what we put in the store. Images need our own adapter to keep the
1568px JPEG transcode. The approve frame carries only the proposal id and the decision, and the
pane-identity check happens entirely in the daemon, so it is unaffected as long as a
replacement renderer keeps that contract, but the "image, caption and Enter as one locked draft" client logic
would have to be redone on the library's composer.

Not provided, and still ours: the pane-bound consent semantics and expiry on disconnect,
the minimize bubble with unread and pending badges, scroll restore on reopen, the native
dialog sheet and standalone viewport rules, voice mode and its audio recovery, the
websocket protocol and audit, the model switcher, and our status-color rules. The island
would be mounted inside our sheet, so every one of these still wraps it.

## The papercuts, classified

| Cut | Prevented natively? |
|---|---|
| Divider under the paperclip (#276) | No: our controls layout |
| Input height jump on image paste (#281) | Yes: textarea plus a separate attachment row |
| Missing close button, End Chat leaving the sheet open | No: our sheet and session state |
| Multi-select attach took one file | Yes: multiple is the default |
| Empty-reply placeholder | No: daemon semantics |
| 16px iOS zoom | No: still a CSS rule we set |
| Overflow at 390px | No: our skin |

Two of seven, and both were small fixes on the existing composer (the multi-select one touches the picker in live-chat.js and the shared composer's file handling).

## Options

1. **Keep and fix papercuts.** Cheapest. The real defect is that the transcript, consent
   card and settle logic exist twice: the phone's live module and the desktop card in
   app.js. Every fix lands twice or drifts. One shared transcript module next to
   live-chat.js, which already shares the composer and bubble, removes roughly 100
   lines and the drift, which is the class of bug we actually have.
2. **assistant-ui island.** Removes about 250 lines of transcript and composer glue but
   adds a 150-line island, a build script and glue, for a net saving near 100 lines,
   against a 554 KB committed generated file and a 100-package tree. Gains we would
   actually use are small today: markdown is not rendered at all (replies are plain
   pre-wrapped text), and branching, editing, regenerate and thread lists are not
   features. Gains we would not use come bundled.
3. **deep-chat web component.** Framework-free and smaller (387 KB), but it owns its DOM
   in a shadow root, so the consent card, thumbnails and tokens fit through its
   extension points, not our CSS, and it has one maintainer and a lower release cadence
   (2.5.1, August 2026). It trades React for a different lock-in. Rejected without a
   spike for that reason.
4. **Adopt patterns only.** Copy the useful idea (a textarea with auto-resize and a
   separate attachment strip instead of contenteditable chips) without the library. This
   is the one piece that addressed real cuts. But the chips are deliberately shared with
   the pane composer, which needs images inline at the caret, so this is a change to
   consider for chat only, and only if the composer produces another cut.

## Build and supply-chain cost of option 2

The repo already vendors npm output: a root package.json, a 46-line generator, committed
files, a byte-for-byte CI check, an npm audit gate and a grouped Dependabot entry. So a
build step is not new, but the kind is: ECharts is copied verbatim from its tarball,
whereas the island would be our JSX compiled by a pinned esbuild, a minified 554 KB file
whose diff is unreviewable and changes on every dependency bump. A CI-only build is
rejected: the daemon and wheels must serve the UI with no Node at runtime. The JS test
runner (node test) has no DOM, so rendering React would need a DOM implementation and a
testing utility added as dev dependencies, beyond the targeted DOM stubs the browser logic
has today. Dependabot would open a
weekly PR against a package that releases daily, and the audit gate would cover a tree
about twenty-five times larger, each advisory needing a decision as the orval exceptions did. License
compliance is easy (MIT throughout) but the Radix and zod licenses would need copying
into the vendor directory as ECharts' do.

## Recommendation

Do not adopt now. Do option 1 and revisit 4 on evidence:

1. Extract one shared transcript module (rows, follow-scroll, consent card, settle,
   thumbnails) used by both clients. Expected net: about 100 lines removed.
2. Put the controls row (attach, input, send, close, minimize) in shared markup and
   CSS, so layout fixes land once.
3. If the composer produces another height or caret cut, move chat alone to a textarea
   with an attachment strip.

**What would change this:** chat gaining markdown or code rendering, edit or regenerate,
branching, or a thread list (the library's real value); an assistant-ui 1.0 with a stable
API; a published non-React binding that removes React DOM (205 KB and the tree behind
it); or the shared transcript growing past what we can maintain. If that happens, adopt
in phases: island for the transcript only inside the existing sheet, then the composer,
with the bubble, consent semantics, voice and protocol staying ours throughout, and a
committed built bundle under the existing vendor check.

## Risks of the recommendation

Staying hand-rolled means we keep owning IME, scroll-follow and focus behavior (the same
bugs assistant-ui has open today, so the risk is shared, not removed). Feature growth in
chat could make a later migration bigger; the shared module keeps the seam clean, since
the island would replace exactly that module.
