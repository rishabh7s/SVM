**Start here:** Open `RUN.md` — it walks you through every step, in order,
starting from a fresh download of the code.

---

## What this product does

### Two modes

| Mode | Does it use memory? |
|---|---|
| **Dictation** | No. It just turns your speech into text. Nothing is saved, nothing is looked up. |
| **Hey Kivi** | Yes, completely. Looking things up, giving sources, fixing mistakes, deleting info, and saving new info all happen here. |

This isn't just a rule written in a document — it's built into the app.
The Dictation screen doesn't even connect to the internet. The idea is
simple: when you're dictating, the job is to write down exactly what you
said, quickly and correctly. Adding memory to that would only slow it down
and risk getting your words wrong. Memory matters when you **ask Kivi a
question** — that's what the "Hey Kivi" mode is for.

### The problems it was designed to solve

A memory system could learn all sorts of things. These four problems are
the ones that justified building it:

1. **"What's true right now?"** — for example: *Who owns Project
   Driftwood? What's the budget?* The answer might be spread across things
   you said weeks apart, and it may have changed more than once. The system
   needs to always give you the latest answer, not an outdated one.
2. **"What should I do first?"** — your open to-dos, put in order, with an
   honest picture of their status. If you said one thing depends on
   another, that comes first — even before deadlines. After that, it's
   ordered by what's overdue, then by due date, then by what you've already
   started. Anything you're blocked on is shown separately as something
   you're *waiting on*, described in your own words — never mixed in with
   things you should be doing today. The system explains, in plain terms,
   why each item is in the order it's in — an order you can't check isn't
   worth much. And if the system keeps reminding you about work you've
   already finished, you'll stop trusting it fast.
3. **"How do I like this done?"** — your standing preferences, found by
   understanding what you mean, not just matching the exact words you used
   before.
4. **"Forget that."** — and it's really gone from every answer the system
   gives, while still being possible to check what happened, if needed.

Underneath all four of these, the most important skill is knowing when
**not** to answer. A memory system that makes up a plausible-sounding
answer when it doesn't actually know is worse than having no memory at
all — because you can't tell the difference from the outside.

### What it deliberately does not do

It doesn't summarize your week, nudge you about things unprompted, guess
your mood, map out who works with whom, or rank what it thinks matters to
you. All of these were possible to build, but none of them made the cut.
The system only remembers things you **actually said** — facts, events,
promises, and preferences — and it won't guess at anything beyond that.

---

## How it's built

```
                    ┌──────────────────────┐
  raw recordings ──►│  BRINGING IN NEW      │
  and dictations    │  INFORMATION          │
                    │                      │
                    │  quick safety check   │──► sensitive info set aside, never sent anywhere
                    │  (no AI involved)     │
                    │       ↓              │
                    │  AI reads it and      │──► small talk/guesses thrown out
                    │  pulls out the facts  │
                    │       ↓              │
                    │  saved properly       │
                    │   (matched to the     │
                    │   right person/thing, │
                    │   old info updated,   │
                    │   every choice logged)│
                    └──────────┬───────────┘
                               ▼
                    ┌──────────────────────┐
                    │  STORAGE              │
                    │  facts · events ·    │
                    │  to-dos ·             │
                    │  preferences          │
                    └──────────┬───────────┘
                               ▼
  "Hey Kivi..." ───► ┌──────────────────────┐
                    │  LOOKING THINGS UP    │
                    │  condensing results   │
                    │       ↓              │
                    │  AI decides what to   │
                    │  check, step by step  │
                    │       ↓              │
                    │  answer with sources  │
                    │  or "I don't know"    │
                    └──────────────────────┘
```

**Built with:** Python, a simple built-in database with fast text search,
a web server (FastAPI), and a simple app interface (Streamlit). It uses
Google's Gemini AI to read text and pull out structured information. There's
no separate search-engine-style database and no complicated add-on layer —
more on why below.

### Bringing in new information

There are three checks, done in this order, and the order matters:

1. **Quick safety check** — before any AI even looks at it, the raw text is
   scanned for things like one-time passcodes, passwords, or access keys.
   If any are found, that piece is set aside on your device and **never
   sent anywhere**. Checking the AI's output instead of the raw text
   wouldn't be as safe.
2. **Pulling out the facts** — one AI step reads the text and turns it into
   a structured result. It's told to ignore small talk and, importantly, to
   ignore "what if" statements: something like "suppose the budget dropped
   to $400k" describes a hypothetical, not a fact — and treating it as real
   would quietly mess up your actual history.
3. **Saving it properly** — matching things to the right person or project,
   using consistent naming, and updating old info when something changes.

Every single thing that comes in gets logged — whether it was saved or
thrown out, and why, including how long it took. So "why wasn't this
saved?" is something you can just look up, not something you have to
investigate.

### How memory is organized

There are four kinds of memory, because they behave differently — not
because four felt like a nice number:

| Kind | What it's for | How it changes |
|---|---|---|
| **Facts** | Things that are true right now | A new value replaces the old one, but the old one is kept on record |
| **Events** | Things that happened at a specific time | Never changes — it happened, that's final |
| **Preferences** | How you like things done | Replaced if specific, added to if general |
| **Commitments (to-dos)** | Promises and tasks | The task itself stays the same; its status (done, blocked, etc.) is tracked over time |

**Nothing is ever changed in place, and nothing is ever truly erased.** A
new value gets added as a new entry, and the old one is marked as no longer
current. Deleting something just marks it as deleted. This is what makes
it possible to answer "what did Kivi think last month, and why did it
change?" — and it's why you can always check what was corrected or removed.

The database itself enforces some ground rules, so a bug in the code can't
quietly break them: there can only be one "current" fact for a given topic,
one current status for a given to-do, and one current preference for a
given category. If something ever tries to break that rule, it fails loudly
right away, instead of silently giving you a confusing or duplicated answer
later.

You can always tell whether something was corrected or deleted, just by
looking at its record — corrections point to what replaced them; deletions
don't.

### Looking things up

Think of the AI as a detective: it starts at one clue, looks around, follows
connections, and checks the history — one step at a time. It has twelve
specific tools available and decides on its own how far to dig.

| Tool | What it's for |
|---|---|
| Search | The starting point — searches across everything. Shows only current info by default |
| Get everything about one thing | Pulls a full, current picture of one person/project/topic in one step |
| Get exact record | Shows exactly what one saved entry says, including its status — for double-checking |
| Get history | Shows how a fact changed over time, or a to-do's status history |
| Get open to-dos | Your outstanding work, in order, with blocked items shown separately |
| Get connections | Shows how two things are related |
| Get events in a time window | Finds things that happened in a given period |
| Calculate | Does any math needed — the AI is never asked to do arithmetic in its head |
| Quick save | Fast way to save one simple fact |
| Full save | Runs a whole sentence through the complete saving process (used for the "remember this" feature) |
| Update | Corrects something — always by replacing, never by writing over the old version |
| Delete | Removes something so it stops being used in answers, while keeping a record for auditing |

The alternative approach — one big tool that just hands back a pre-packaged
bundle of context — would be faster, but it was rejected: it means the AI
can only ever see what the bundle's designer thought to include, and can't
double-check who said something or decide for itself whether an old value
is still relevant. This approach takes more steps, but it lets the AI
actually investigate.

**Three things are enforced by the code itself, not just by instructing the
AI to behave well:**

- **Staying current.** Searches only return active, non-deleted info by
  default. Old info is still searchable text underneath, but it's filtered
  out so it can't accidentally be presented as current. There are separate
  tools for when you deliberately want to see the full, unfiltered record.
- **Not guessing when unsure.** If a search term only loosely matches
  something unrelated, that shouldn't be treated as a real answer — because
  one loose match is enough to produce a confident-sounding wrong answer.
  Weak matches are filtered out, and results are ranked by how well they
  actually match, not just by relevance score alone.
- **Always showing sources.** The system is built so that it's literally
  impossible to give an answer without a source attached. If there's
  nothing to cite, the only valid response is "I don't know" — this isn't
  just an instruction the AI could ignore, it's built into the code.

### Two ways to save something, on purpose

The **quick save** tool takes one simple fact — cheap and fast, good for
something like "David is now our team lead."

The **full save** tool takes a whole sentence. For example: "The Helix
launch moved to the 14th, Priya's covering it while Sam's out, and I owe
the client a summary by Friday" — that's actually two facts and a
commitment bundled together. Trying to force that into the simple one-fact
format would lose most of the information. So this tool runs the sentence
through the same full process a dictation goes through, safety checks
included. One single tool couldn't handle both of these cases well, so
there are two.

Both tools follow the same rule: hypotheticals and questions are never
saved as facts.

### Being honest about what it actually did

The system is only allowed to say it made a change if it actually
succeeded in making that change. Any tool that changes something has to
complete the change and get a confirmed result back before the AI is
allowed to say it worked — and if something fails, it has to say so
plainly. This is checked directly against the database after each test run,
because "I've deleted that" followed by the info still being there quietly
in the background is about the worst thing a memory system could do — you'd
stop worrying about it, while it's still sitting there, still being used in
future answers.

The voice-based and app-based ways of deleting or correcting something both
use the exact same underlying code, so they can't ever behave differently
from each other.

---

## Results

### The test data

509 realistic, made-up transcripts for one fictional user, each with raw
speech-to-text output, a cleaned-up version, a timestamp, and which app was
open at the time. The story woven through them deliberately includes
handovers, budget changes, updated preferences, finished and blocked
to-dos, hypotheticals, small talk, and people accidentally saying passwords
out loud.

### Processing all the test data

Counts below are read straight out of the shipped database. It holds **516**
items: the 509 corpus records plus the 7 from the small hand-written seed
narrative (`db/seed.sql`) that the tests and a clean demo run against.

| | |
|---|---|
| Items processed | **516** |
| Saved as memory | **463** |
| Thrown out by the AI (small talk, hypotheticals, empty) | **48** |
| Set aside by the safety check before reaching the AI | **5** |
| Failed | **0** |
| Facts / events / to-dos / preferences saved | 298 / 162 / 122 / 39 |
| People and projects recognised | 51 |
| Facts still current vs. replaced by something newer | 198 current, 99 replaced |
| Connections found between items | 9 (5 "this fixed that", 4 "this must come first") |
| Database size | **780 KB** |
| Time per item | typically 3.5 seconds, 17 seconds at the 95th percentile |

That number of 99 replaced facts is the interesting one — it means the
underlying story genuinely changed 99 times, and each one of those is a
chance for the system to accidentally give an outdated answer instead of
the current one.

The database already comes with all of this test data loaded in, so running
the offline check reproduces these results on a fresh copy of the code in
seconds, instead of needing to reprocess around 500 items from scratch. Full
details behind every number above — what was decided, why, and how long it
took — are logged and can be looked up.

### Testing the "Hey Kivi" question-answering

17 test cases covering: recalling current facts, correctly saying "I don't
know," handling multi-part questions, making changes, tracking to-do
status, checking history, preferences, combining several pieces of info,
catching false assumptions in a question, deciding what to do first,
reporting what's being waited on, and reusing a past fix.

Numbers below are from the run saved in `evals/results/20260912_193119/`,
using `gemini-3.1-flash-lite` as the answering model:

| | |
|---|---|
| Question-answering test cases passed | **16 / 16 evaluated** (see note) |
| Search-quality checks passed | **18 / 18** |
| Database-state checks after the run | **1 / 1** |
| AI calls used | 45 |
| Words processed | 279,474 read / 7,618 written |
| Time per question | typically 5.7 seconds, up to 27 seconds in the slowest case |
| Time to look things up (excluding the AI thinking) | about 2 thousandths of a second |

**The note, because it matters:** one of the 17 cases
(`false_premise_is_corrected`) was **not evaluated** in that run — Gemini's
free tier allows 15 requests per minute and a 17-case run exceeds it. The
harness deliberately reports a rate-limited case as "not evaluated" rather
than as a failure, because a provider quota says nothing about whether the
system works — but it also means it is not claiming a pass it didn't
observe. Re-running that case alone passes
(`evals/results/20260912_193421/`, 1/1).

So every one of the 17 cases passes, but on a free-tier key they do not all
pass inside a single batch run. How many get skipped varies with quota: a
later run (`20260912_193816`) got through 14 and skipped 3. **On a paid key
the full set should complete in one run** — and if you are comparing runs,
`evals/results/` keeps all of them, so the variation is visible rather than
hidden (`latest.json` is simply whichever ran most recently, offline runs
included — read the named directories above for the cited figures). The
deterministic
`--offline` evaluation is unaffected by any of this: it makes no model
calls and passes 17/17 cases and 18/18 checks every time.

Other automated tests: **198 passed, 3 skipped** (the 3 skips need a live
API key).

That last row is worth noticing: looking things up in the database takes
almost no time at all — the wait you feel is the AI thinking, not the
memory system. That's the right place for the slowdown to be, and it's
the trade-off made by letting the AI take extra investigative steps instead
of using one big shortcut.

Cost is reported as a word/token count rather than a dollar figure, since
prices change — you can plug in current pricing yourself if you want a
dollar estimate.

### How to reproduce these results

```bash
python run_evaluation.py --offline    # no AI key needed, free, tests memory only
python run_evaluation.py               # full test, using the real AI
```

The offline version isn't a "lite" version — it tests the memory system
using the exact same tools the AI uses, so it can be fully tested without
needing any AI access or cost, and gives the same result every time. Every
run saves detailed results and a summary into a results folder, and it
works on a **copy** of the database, since some tests intentionally delete
things.

---

## Why it's built this way

**A simple built-in database with fast text search, instead of a more
complex "AI similarity search" database.** Most questions this product
answers are about *specific, named things* — a project, a person, a
detail — not vague similarity. Straightforward text search handles that
well, and a normal structured database is better suited to what actually
matters here: not "what sounds similar," but "what's currently true, what
replaced what, what's still open." The fancier approach would add some
flexibility but make it much harder to track corrections and sources — a
trade not worth making for this product. The whole database is one small
file, under a megabyte, that anyone can open and look at directly.

**Facts, events, to-dos, and preferences kept as separate categories.**
They behave differently enough that treating them all as one generic
"memory" type would mean using one set of rules for four different
behaviors — and to-dos in particular need their status tracked separately
from the to-do itself.

**Nothing is ever truly deleted, just marked as removed.** "Forget that"
needs to feel real to the user, while still being possible to explain and
check later if needed. Truly erasing data would give you the first but not
the second.

**A hard rule, not just an instruction, that every answer must have a
source.** Anything this product promises should be guaranteed by the code
itself — not left to the AI remembering to follow instructions.

**No fixed list of allowed categories for facts/events/relationships.** A
fixed list designed around one way of working would be the wrong fit for
someone else. The cost of this flexibility is that the same idea can get
saved under slightly different names — handled with a small cleanup list
for the few cases where that duplication would actually cause a wrong
answer, rather than locking the categories down completely.

---

## Known limitations

Known on purpose, roughly in order of how much they matter:

- **Connections between things are only found within a single recording.**
  If you mention a problem and its fix in the same sentence, it links them.
  But if you mention the fix weeks after mentioning the problem, it won't
  connect them — that would need a more advanced kind of matching that
  isn't built yet. As a result, the test data only has 9 connections found,
  while several earlier problems are left unresolved because their fix was
  mentioned separately. This is currently the biggest gap.
- **The same to-do can end up stored more than once under different
  wording.** A to-do is matched to an existing one only when the wording
  matches exactly, so restating it creates a second entry. In the test data
  "Talon onboarding flow" exists as 8 separate to-dos ("review onboarding
  flow", "look at the onboarding flow", "Talon onboarding flow
  development"), and "vendor transition" as 6. The visible consequence:
  asking what you're waiting on can list the same underlying task more than
  once, and a status update lands on whichever copy was named. Fixing this
  properly needs the same fuzzy matching used for people and projects,
  applied to to-dos.
- **Only the 50 most recently active people/projects are shown to the AI
  when it reads a new recording.** The test data has 51, so the least
  recently used one is invisible at that moment and a fresh mention of it
  can create a duplicate entry instead of attaching to the existing one.
  This gets worse as the number of projects grows, and should become a
  relevance-filtered lookup rather than a fixed cap.
- **Numbers aren't findable by text search.** A fact's numeric value (69
  facts in the test data carry one) isn't included in the search index —
  only its label and text are. So "which project has the 4.5 million
  budget?" can't find the fact by the number itself; asking about the
  project by name works fine.
- **"This depends on that" is never guessed from free text.** If you say
  you're blocked on something, that's shown to you word-for-word, but the
  system won't try to automatically match it to another to-do — testing
  showed that kind of guessing produces confidently wrong matches (for
  example, matching "waiting on vendor access" from one project to an
  unrelated project that also mentions "access"). A wrong guess is worse
  than no guess, so only connections you've explicitly stated are used.
- **Naming cleanup only applies going forward.** Entries saved before the
  cleanup rules existed keep their original wording. New entries are
  matched against the cleaned-up version, so a new entry will correctly
  update an old one — but the old database itself isn't rewritten. That's
  treated as a deliberate, separate step, not something that happens
  automatically.
- **Matching people/projects to what you mean is based on wording, not
  full understanding.** It matches exact or very similar names, plus a
  broader text search. It won't know that "the Helix thing" means the
  "Helix migration project" unless it's seen that connection before.
  Every match is logged with how it was made, so mismatches can be traced.
- **Figuring out relative dates (like "next Tuesday") relies on the AI's
  judgment** at the time you said it. Both what you actually said and what
  it was understood to mean are saved, so a mistake is visible — but it
  isn't double-checked automatically.
- **Breaking a multi-part question into pieces relies on careful
  instructions to the AI, not a hard guarantee.** It's tested and working,
  but it's the part most likely to break if the underlying AI model
  changes.
- **Ongoing conversation context isn't saved permanently.** Restarting the
  app clears the current conversation, but your actual saved memory is
  unaffected.
- **The safety check for sensitive info uses pattern-matching, not a
  smart classifier.** It catches things like one-time codes, passwords,
  and card-like numbers, plus generally "password-looking" text. A
  credential in an unusual format might slip through.
- **Built for one user at a time.** There's no login system or per-user
  separation. This is meant as a local demo, not something ready to deploy
  for multiple people.
- **The step-by-step tool approach is slower.** Questions that need several
  steps mean several separate AI calls — typically around 6 seconds
  end-to-end, up to 27 in the worst measured case. A single all-in-one
  lookup would be faster but less capable; that trade-off was made
  deliberately and could be revisited.
- **Heavy reading and writing at the same time can collide.** The database
  is opened with default settings, so importing a corpus from the app while
  also asking questions can, in the worst case, produce a "database is
  locked" error. Re-running the request works. A single shared connection
  helper with write-ahead logging would remove this; it isn't in place.

---

## Use of AI in building this

**Part one — the overall idea and design:** the positioning statement and
vision document (`Positioning_Vision.md`) were written without AI help, as
required.

**Part two — building it:** built with AI help (Claude), used more like a
pair-programming partner than something that just generates code on its
own. The overall structure, how memory works, the split between Dictation
and "Hey Kivi," and the decision to leave out features that didn't earn
their place — those were all decided by the person building it. AI was
used to help implement, review, and debug against those decisions.

**The models the running system uses** are Google's Gemini family, called
through `instructor` so they return validated structured data instead of
free-form text. Ingestion/extraction and the answering agent are configured
separately (`KIVI_EXTRACTION_MODEL`, `KIVI_RETRIEVAL_MODEL`,
`KIVI_LIGHT_MODEL`); the evaluation numbers above were produced with
`gemini-3.1-flash-lite` answering.

**The test data** (`kivi_corpus.json`) was generated with a language model,
which the assignment permits explicitly. It is synthetic data for one
fictional user — not anyone's real history.

---

## Where things are in the code

```
kivi/
  ingestion/     the "bringing in new information" pipeline
  retrieval/     the "looking things up" logic and tools
  api/           the web server and shared logic for making changes
  models/        the data formats used
  formatting.py  one shared place that formats how things are displayed
  retry.py       shared logic for retrying if something fails temporarily
db/
  schema.sql     the database structure, heavily commented
  seed.sql       a small hand-written example dataset for testing/demos
  init_db.py     sets up/resets the database  ·  verify.py  checks it's correct
evals/
  eval_set.json  the 17 test cases, each explaining why it matters
  results/       where test results get saved, one folder per run
tests/           201 automated tests, no internet needed for 198 of them
streamlit_app.py the app itself
run_evaluation.py
import_corpus.py · reset_db.py
```

---

## License

Apache 2.0 — see `LICENSE`.
