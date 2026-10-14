# Design: the dashboard's layout

Status: **decided**; Open loops and the session cards are built (#382), the rest is in
review. Chosen from the dashboard layout and dashboard split mocks. This page holds the
final layout and why; when it changes, edit it in place so the diff shows what moved.

## The problem

The dashboard grew one panel at a time: chips, Activity, Plan usage, Running by, the
session atlas, the radar, a Needs you list, the whole Open loops queue, and only then the
heatmap. Everything was there, but the page answered no question first, the phone had to
scroll through several screens of it, and Needs you appeared twice (in the sidebar and on
the page).

## The layout (A + C)

- **Charts plus cards, and each card opens its own view.** Open loops shows its lane counts
  and the top item waiting on you; Running by hour shows the heatmap. Details › opens the
  full queue or the full heatmap as a page of its own. A dashboard is for noticing; the
  long lists it used to inline belong one tap away, where they can be read properly.
- **Running by hour fills its card.** The top row is as tall as its tallest card (usually
  Open loops), so a fixed-height week of rows left the heatmap card half empty. The rows now
  grow into whatever height the row leaves, without ever setting it, and four figures sit
  under them that the grid only implies: the peak hour, the busiest day, the average running
  at any hour, and the last 7 full days against the 7 before (the only week-over-week number
  on the dashboard). They use Open loops' own KPI tiles, one style for every figure on the
  page, with one-line labels so a tile never makes the row taller; each tile's tooltip
  holds the detail. Chosen over showing more weeks (denser rows whose count changes with
  the window), an hour-of-day profile (a second chart form in one card) and a full-width
  strip (a taller page), from the "use the space" mock.
- **One card per session** (from option C), with its 7-day trend, its pane icons and an
  "N need you" note. It replaces both the atlas and Running by session, which drew the same
  sessions twice in two shapes.
- **No Needs you list on the page**: the sidebar already has it, and the session cards say
  where the waiting panes are.
- **Plan usage is a short full-width strip** under the cards on wide screens (three
  columns and wider), instead of a card competing with them for a grid cell. Usage is
  glanced at, not studied, and the detail view still has the charts.
- **Activity is pinned to the bottom** of the page, so the fleet's pulse is always in the
  same place however long the cards above it run.

## Alternatives considered

- **As shipped in #369** (every panel inline): the problem above.
- **B, the same dashboard with Dashboard / Sessions / Open loops as top-level tabs**: not
  chosen; the cards already give Open loops and the heatmap their own views without a
  second layer of navigation.
- **Plan usage as a card in the grid**: replaced on wide screens by the strip, since a card
  took a whole grid cell for a handful of meters.
- **First-round options** (usage folded into the Activity chart as tabs, a KPI row over the
  page, usage off the dashboard entirely): superseded by the strip.
