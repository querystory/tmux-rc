# The Claude skin

Status: implemented (`web/m/skin-claude.css`, Settings › Appearance › Skin).

Many people reach tmux-rc from Claude Code's Remote Control: they already drive agents from
a phone, in Claude's own app, and want to drive more of them, or other agents beside
Claude. For them the app's green-on-slate look is one more new thing on a screen that is
already doing the same job differently. The skin removes that one: the app in the look they
came from, so what is left to learn is only what tmux-rc adds.

## What it changes

- **The ground and the accent.** Warm paper (`#faf9f5`) or warm charcoal (`#262624`) in
  place of the cool greys, and a clay accent in place of green. Light and dark both: the
  skin is a class on `<html>` beside the theme's, not a third theme, so System, Light and
  Dark keep working under it.
- **Serif titles.** Pane titles, headlines, sheet and dashboard card headings are set in
  the system serif at a book weight. Body text, rows and the terminal are untouched.
- **Pills and soft cards.** Search, segmented controls, answer buttons and the composer are
  rounded; sheets get a 24px top.
- **New session is the dark pill** floating over the list, and Send is a round clay button:
  the two controls a Remote Control user's thumb already knows.

- **Scratch reads as Artifacts.** The gallery's cards are round, with the thumbnail a
  rounded tile inset in each, serif titles and a pill filter. The gallery and the Markdown
  pages it opens are sandboxed frames that cannot read the setting, so the app tells them,
  as it does the theme; opened in a tab of its own, Scratch has nobody to tell it and stays
  in the default skin.

## What it leaves alone, and why

- **Layout and words.** Rows, tabs, the sidebar and every label are the same in both skins,
  so docs, screenshots and muscle memory hold whichever is on. A skin that moved things
  would be a second app to maintain.
- **State colours.** Amber for Needs you, the chart's seven states and red are what the
  fleet is saying, not decoration, and mean the same in both skins.
- **Claude's own marks and fonts.** The serif is whatever the device has, and nothing of
  Anthropic's is bundled: the skin is inspired by their apps, not a copy of one. The app
  still says tmux-rc.

## How it is built

One stylesheet, loaded after `style.css`, every rule under `.claude`: the tokens restated
for both themes, then type and shape. `tests/test_mobile_ui.py` fails on a token the skin
sets that the app does not define. The choice is `tmuxrc-skin` in localStorage, read by the
boot script in `index.html` before first paint, as the theme is. The Fleet graph and the
charts repaint on a change of the root's class, so they pick up the palette unasked.

Rejected: a third value in Theme (a skin needs both light and dark); following Claude's
session-list layout as well (one line of status per row loses the summaries and one-tap
answers that are the reason to be here).
