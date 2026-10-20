# The phone pane header

Status: implemented (`web/m/app.js`, `web/m/style.css`, `web/m/index.html`). Phone layout only;
the wide pane header does not change.

The phone's pane view opened on a header that did two jobs on two rows: Back and the actions,
then the title and the session line under them. Between the actions sat a boxed
**Overview | Terminal** text pill, the only worded, boxed control in a row of bare icons, and it
cost a third of the row. Both rows stayed on screen however far you scrolled. The reference was
Gmail's message view, and the options came out of a mock loop with the user (the shared mock
harness, `pane-header`).

## What was picked

**A slim pinned bar, with the title in the content under it.** As in Gmail, only the bar stays:
Back, the view switch, Chat or Voice, and the ⋯ menu. The pane's title (now 20px, two lines at
most) and its session line head the overview and scroll up under the bar and off the screen with
everything else, so a long overview gets that space back. Once the title is out of sight, the bar
shows the pane's name in one line next to Back, so you still know where you are. That also
covers the terminal: its own scroll starts at the latest output, at the bottom, so a title placed
at its top would never be seen. The bar names the pane there from the start.

**The view switch is an icon pair with a sliding thumb.** The user liked two of the four options
equally. One was a single terminal toggle, which reads as one more plain icon in the row. The
other was a pair of icons, overview and terminal, which shows both modes at once. They also asked
for the switch to be more obvious. The pair wins on that: you see which mode you are in and which
one you are not. It borrows the toggle's look, bare icons rather than words. The active one sits
on a solid accent thumb with its icon knocked out, a much stronger mark than the old pale tint. On
a switch the thumb slides across to the other icon and the incoming view fades in, so the change
shows in the control and in the content at the same moment. With reduced motion, both are
instant. Overview's icon is a document (lines of prose), not the list glyph the mock tried, which
read as "list" more than "summary".

## What was rejected

- **The text pill (as it was).** It is unambiguous, but wordy and boxed next to bare icons, and
  it held the title to one ellipsized line beside it.
- **A single toggle on its own.** It was the mock's recommendation, and the closest to Gmail. But
  in Overview it reads as an action ("open the terminal") more than as a mode. A pressed or
  unpressed state on one icon is exactly the subtle cue the user asked to make louder.
- **Swipe between views, with page dots.** It gives the cleanest bar, but nothing shows that the
  gesture exists. In the terminal, wide lines scroll sideways and take the swipe. The dots cost a
  row and don't say what each page is.
- **The switch in the composer.** It sits under the thumb, but the composer is already full, and
  the mode control would then be out of sight at the bottom while you check where you are at the
  top.
- **Hiding the bar on scroll as well.** Gmail keeps its action bar, and so does this view: Back
  and the switch are what you reach for mid-scroll.

## Why the title moves rather than collapses

The title block is moved into the overview's scroller on a phone, and back into the header on a
wide screen. The alternative was to shrink a header that stays put as the overview scrolls (a
collapsing toolbar). That needs scroll-linked sizing, which is janky on iOS and resizes the
scroller under your finger. Moving the block lets the browser's own scrolling carry it away, at no cost.
Whether the title is in view is watched with an observer rather than scroll events. The observer
also reports the title gone when the overview is hidden, so the terminal case comes free.
