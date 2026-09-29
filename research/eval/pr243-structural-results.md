# PR 243: structural rewrite evaluation

2026-09-28. Model: `gemini-3.1-flash-lite`, production Vertex settings, temperature 0.
Main baseline prompt: `9517b80`, exact `parser_prompt.txt` text. Five sequential
runs of each sample below. An earlier concurrent attempt hit a closed-client error
and is excluded, not scored as model failures.

| Sample | Main passes | Published long prompt | Initial structural | Revised (8aed39a) |
| --- | --- | --- | --- | --- |
| 27: footer title versus quoted pane listing | 0/5 | 5/5 | 0/5 | 5/5 |
| 28: titleless footer starting with a path | 0/5 | 5/5 | 0/5 | 5/5 |
| 29: compact titleless footer and quoted pane name | 0/5 | 5/5 | 0/5 | 5/5 |
| 30: UUID is not a conversation title | 0/5 | 5/5 | 5/5 | 5/5 |
| 31: conversation name containing a model name | 5/5 | 5/5 | 5/5 | 5/5 |
| 32: answered update menu | 0/5 | 5/5 | 0/5 | 5/5 |

The third column uses published #243 (`fa76bd4`) prompt text, main's classifier
post-processing (without the new UUID/visibility checks), and the original unmarked
captures. Those appendix examples were copied from these samples, so 5/5 is
contaminated evaluation evidence, not demonstrated generalization. They stay out
of the rewrite.

Candidate changes: deterministic UUID/hex-ID rejection; separate tmux history and
visible captures joined by a marker; five shared instruction lines for the boundary,
completed-turn timestamps, and identity provenance; main's session hints and answer
style rules restored. Tool sections are composed from separate files. The appended
identity/state examples are removed. No new example screens were added to the prompt.

Sample 32 now explicitly models the synthetic history/viewport boundary before the
welcome card. This compares the combined structural fix, not just prompt wording.
All other captures in this comparison are unchanged. The boundary alone did **not**
stop the model from reporting the old menu. UUID filtering is reliable in code;
the remaining failures should not be described as regressions from shortening,
because they also fail on main. The revised code fixes these cases as shown above.

First full candidate run: **28/35**. Failures: 12 (unexpected copyable), 21 (path
promoted to session), 27–29 (identity), 32 (stale question), 33 (missing PR association).
The candidate-file A/B run (`--prompt` with the exact composed text) also passed
**28/35**, with the same failures. Python tests: **613 passed**; Ruff clean.
New completed-turn/draft sample 35 passes. Main's repeated baseline above does not
establish whether failures outside 27–32 are pre-existing; no such claim is made.

These are historical intermediate results, not the revised implementation's verdict.
Keep this evidence separate from the independently deployed PR-title feature (#248).

## Current structural implementation (2026-09-29)

#249 is Copilot-clean at c41354e and remains unmerged. #243 is stacked on that
branch; tool fragments are unchanged. The main-sync preserves all landed work.

The classifier validates session names against bottom status rows or a visible
Thread-renamed line, rejecting UUID/hex IDs and absolute/home paths. With a viewport
marker, question text must occur below it (whitespace/case normalized). A rejected
field permits one bounded model re-read on the relevant UI slice, followed by the
same validation. This recovers an actual footer title rather than merely deleting
the wrong name, and reclassifies activity/headline after rejecting a stale menu.

Genuine Claude permission-box testing exposed a conflict with the old instruction
to paraphrase question prompts. That paragraph now asks for verbatim visible text
and supporting context in tables. Answer-style rules are unchanged. Total shared
prompt change: five new instruction lines and a five-line paragraph replaced by
two lines; no new literal example screens. A byte comparison test allows only these
edits relative to #249's golden composed prompt.

Three structural runs with the visible checks passed all six cases 27–32 five
times each (30/30 per run), including the final 8aed39a code after the null-session
retry optimization. Full tests: 696 passed; Ruff clean, including a real isolated
tmux history/zero-history boundary test.
Both genuine Codex and Claude permission boxes pass individually with viewport
markers. The earlier full run (before the question-instruction correction) passed
32/36: failures were 04, 16, 22, and 33. Sample 16 is the known main regression
introduced as an eval in #198, not fixed by this PR. Other failures remain under
evaluation and are not being silently waived.

After the verbatim-question correction, the full run was 32/36 and candidate-file
A/B was 33/36. Five-run comparisons established copyable regressions on samples 06
and 26 (main 5/5 each, candidate 0/5 each); 27_pr_identifier was main 5/5 versus
candidate 4/5. The code now deduplicates copyables already represented by table rows,
and preserves menu/cursor copyable payloads as supporting tables rather than paste
actions. No extra prompt prose was added for these failures. The next full run was
34/36: only sample 16 and the intermittent 27_pr_identifier copyable failed. Five
subsequent direct classifier calls for 27_pr_identifier emitted no copyable. This
history is retained rather than reporting only the favorable reruns.

The last copyable was a shortened fragment of a longer narrative paragraph. Copyable
validation now requires a whole displayed block or inline code, allowing box borders,
terminal wrapping, and shell line continuations. Positive commit-message/command
fixtures still pass. Final candidate-file A/B: **35/36**, with only sample 16's known
main missing-table regression failing. No remaining failure is hidden or re-blessed.
Final unit suite: **698 passed**, Ruff clean. The prompt remains unchanged by these
copyable fixes; target matrix above is 30/30 on the same identity/question rules.

### Boundary review follow-up

Copilot identified a collision with panes printing `[visible screen]`. The capture
marker now frames that label with C0 record/unit separators emitted by tmux's
`display-message`, which are not terminal cells. The isolated tmux test writes the
entire framed token through a pane and verifies there is still exactly one actual
boundary and that the printed label survives. Sample 06 now includes a printed
label after a genuine permission question; that question remains actionable.

After this fix: **699 unit tests**, Ruff clean; samples 27–32 **30/30** again.
Full candidate-file A/B: **34/36**. Sample 16 still lacks its required table; sample
17 also omitted the cursor picker's search flag. A repeated main/candidate comparison
for 17 passed **5/5 on main versus 3/5 on the candidate**, so this was treated as a
regression. The classifier now restores that flag only for a detected cursor picker
whose visible footer explicitly advertises the binding. No prompt prose was added.
The repeated comparison now passes **5/5 for both**; sample 17 includes the actual
capture boundary, and unit tests reject history-only binding evidence. **700 unit
tests pass**, Ruff clean. That run was superseded by the main refresh below.

### Current-main / tmux 3.4 follow-up

Claude reproduced the CI failure on tmux 3.4: `display-message` vis-escapes C0 bytes.
The command now emits an unpredictable ASCII nonce, replaced with the framed marker
after capture. This retains collision protection without depending on tmux's C0
serialization. Local real-tmux tests pass on 3.7c; GitHub CI run 36551902280 also
passes all 731 tests with no skips after the correction. Tests pin that the transport
token is 32 hexadecimal characters.

Reapplied #243's net changes on #249 plus main 27d81e1, preserving #238's foreground
tool forcing, OpenCode task validation and interrupt-row detection before visible
field grounding. The prompt byte-comparison baseline now includes current main's
landed prompt edits. **731 unit tests pass**, Ruff clean; repeated targets **30/30**,
full corpus **37/38**, with only sample 16 failing.

Copilot's next two findings are also addressed: explicit rename events in scrollback
remain identity evidence (questions stay viewport-only), and copyables matching an
individual table row are deduplicated along with whole tables. Sample 36 pins a
scrolled-off rename; unit tests cover initial validation and the bounded retry.
Latest full corpus: **38/39**, only the same sample 16 failure. **732 unit tests**
and Ruff pass. Final samples 27–32 repetition: **30/30**, five passes per case.

Only #243 and #249 belong to this workstream. Do not merge either; do not deploy
integration or push Claude-owned branches. The structural rewrite is pushed, stacked
on #249. Next: finish the current-head Copilot loop; no merge or deployment authorized.
