# emailchrono-synapse

**Track 2 entry for the 2nd Brain Labs x Geek Room Vibe-a-thon: one smart tool from many small functions.**

We used Synapse CLI to compose two MCP tools from 11 small Python functions in [emailchrono](https://github.com/iris-ng/emailchrono-local). This local app turns emails into a chronology for legal work. An agent uses the two tools to fix problems in an email chronology. It splits cards that hold more than one email and fills in missing senders, recipients, subjects and dates. It puts emails read in the wrong time zone back in order and resolves duplicate pairs. Every change shows its evidence. Guesses stay marked "inferred". Nothing changes until a person approves the exact plan in the web app.

<!-- TODO: demo video link -->

## Results at a glance

- **One tool vs many:** with the 2 composed tools, the agent finished 9 of 9 jobs. With the same 11 functions as 11 separate tools, it finished 7 of 9, wrote no field fixes, and used 7 to 18 times as many tokens (3 runs per task, same model and prompt).
- **Errors and edge cases:** 7 of 7 live rehearsal cases pass: decline, a stale plan, an expired plan, applying twice, a failure halfway through apply, the AI model being down, and a missing case.
- **Safety:** no tool can approve. Every run, comparison and rehearsal had 0 unsafe apply attempts.

## The problem

A chronology depends on correct cards. emailchrono's parser handles clean mail well, but it fails on some emails:

- a reply marker in French or German that it does not recognise, so two emails end up on one card;
- a Gmail quote line that leaves `AM, John Tan` as the sender and no date;
- a quoted email read in the wrong time zone, so it sits in the wrong place.

Fixing these by hand is slow. Letting an AI fix them silently is not acceptable for legal work. The tool must propose a change, show its evidence, and wait for a person.

## Tool design

**Two tools, one job each, two arguments each:**

| Tool | Call it with | It does |
|---|---|---|
| `review_chronology` | `case_id` | finds problem cards, asks the AI for split points and field values, checks every value in code, saves a plan; returns a short summary and a `plan_id` |
| | `case_id, plan_id` | applies that plan if a person approved it; otherwise says it is waiting, declined, stale or expired |
| `resolve_case_duplicates` | the same two forms | the same for duplicate pairs |

Why we use this design:

- **Plan and apply in one tool.** The agent never builds a plan itself. It calls once to plan and once more with the plan id. The runner pauses that second call until the person decides.
- **The tool returns a summary, not data.** Data passed between steps (card lists, AI proposals, pair evidence) stays inside the tool. The comparison below shows why that matters.
- **No approve tool, in either mode.** The model has no tool to approve its own plan.

**What was already there, and what we added.** emailchrono already had Snip to split a card, a duplicate scorer, a date parser, thread rebuilding and field edits. For this entry, we wrapped them as 11 small functions. Each handles one review step: check the case is ready, list problem cards or pairs, propose (with AI for chronology), save the plan, apply the plan. 6 are in `backend/app/review/chronology.py` and 5 in `backend/app/review/dedupe.py`. These functions use the new parts: the problem finder, the checks on every AI value, and the plan and approval store.

**What Synapse added.** Synapse CLI 0.1.9 in Custom mode (`--local`, OpenAI provider) read one paragraph per tool (appendix). It scanned `backend/` (51 endpoints, 300+ functions), found our functions and generated each MCP server. It wrote the loop over problem cards, the "split only merged cards" branch and the plan-or-apply switch. This turns five steps into one tool. Code carries the data between steps instead of the model. The comparison below shows the effect.

## Safety: the confirmation step

**Only a person can approve:** through the Approve button in the web app or a backup terminal script that refuses piped input. The model cannot reach either. A test fails if any server file imports approval, web or HTTP code.

**What is approved is what is applied:** approval records a hash of the plan items, and apply checks it.

**Stale plans are refused:** a plan stores a fingerprint of every card and pair it touches, and of the case's time zone and trash state. If any of them changes, no one can approve or apply the plan.

**Plans expire** after 30 minutes. **Apply is all or nothing**, in one transaction. **Database triggers freeze finished plans.**

**Code checks every AI value before it enters a plan:**

- A split line must be a reply marker or header line.
- A date must be one date expression quoted from the email's own header lines, not a deadline in the text.
- A sender, recipient or subject must appear in full in the email's header lines or follow from how the email is attached (a reply or a forward). A value must never make an email's recipient its own sender.

Anything unproven goes to a "left for you" list.

**Duplicates need proof:** the tool proposes a duplicate only for an exact match: the same Message-ID, the same file, or the same text with the same sender and date. It proposes the same text from another sender, or days apart, as a separate email. Anything in between goes to the person.

**Guesses stay visible:** values the AI works out, rather than reads from a header, carry an "inferred" mark on the card. Hovering shows the evidence. Editing the field by hand clears it. The Excel export has an "Inferred fields" column.

We also ran both jobs with a real person approving in the browser.

## Errors and edge cases

We rehearsed these cases live with the composed agent, the real model and a scripted person. Each ran on a fresh copy of the test case (`bench/edge_cases.py`, run on commit `dff45be`):

| Case | What the agent did |
|---|---|
| The person declines | "…was declined in emailchrono, so nothing changed." Did not try again. |
| The person edits a card the plan touches (Approve is then disabled) | Noticed the plan went stale and built a new plan once; it was approved and applied. The person's edit was kept. |
| The plan expires | Built a new plan once; it was approved and applied. |
| Asked to apply the same plan again, then to clean up again | "…was already applied. Nothing else changed." Then: nothing to change; one pair needs your decision. |
| Apply fails halfway (injected) | "…failed and was rolled back, so nothing changed." |
| The AI model is down | "…found nothing to change automatically. 9 items need your review." No plan, nothing changed. |
| A case that does not exist | "Case 99 was not found…" |

The first rehearsal found one real problem, which we fixed. When a person edited a card in a waiting plan, the web app disabled Approve. But the agent waited until its timeout, then asked the person to approve the plan even though approval was no longer possible. The runner now detects a stale plan while it waits and tells the agent to build a new one.

We also run 228 backend tests offline with a scripted model. They cover a plan for another case, a plan applied twice, approve and decline racing each other or apply, and expiry racing apply. They cover a tampered plan (apply rechecks the items hash), a card changing while the plan is being built, and an invalid item causing the whole plan to be refused. They also cover values the AI quotes wrongly, header-like lines in the body, a split at an ordinary line, a date pieced together from two dates, a sender address the AI tries to replace, recipients taken from a greeting, and API errors and timeouts.

## One tool vs many small tools

**Setup.** We compare the same job with the 2 composed tools and with the same 11 functions as 11 separate tools (`backend/small_tools_server.py`, one tool per function, each with the function's own description). Both modes use the same model (`gpt-5.6-luna`), settings, system prompt and approval rule. For fairness, the many-tools agent also gets the same steps for chaining calls that Synapse was given (the appendix intents). Every run starts from a fresh copy of the test case. A scripted approver approves every plan. We score results against the answer key (`bench/run.py`, `bench/score.py`).

**Results** (mean of 3 runs per task and mode):

| Task | | Composed (2 tools) | Small (11 tools) |
|---|---|---|---|
| Chronology | Jobs done | 3 of 3 | 3 of 3 (splits only) |
| | Field values written / wrong | 28 / 0 | 0 / 0 |
| | Items left for the person | 1 | 12 |
| | Model calls / tool calls | 3 / 3 | 8 / 22 |
| | Tokens | 15,842 | 115,446 |
| | Time | 55 s | 88 s |
| Duplicates | Jobs done, wrong decisions | 3 of 3, 0 | 3 of 3, 0 |
| | Model calls / tool calls | 3 / 3 | 7 / 19 |
| | Tokens | 2,398 | 44,048 |
| | Time | 6.5 s | 37 s |
| Both in one request | Jobs done | 3 of 3 | 1 of 3 |
| | Tokens | 18,883 | 261,297 |
| | Time | 58 s | 105 s |

Tokens include the AI calls made inside the tools. Unsafe apply attempts: 0 in every run of both modes. After the stricter value checks (see "Independent review"), 3 more composed chronology runs gave the same result: 28 values written, 0 wrong.

**What happened with the separate tools.** The 11 functions work: a test chains them in code exactly as the composed tool does and gets the same plan. Problems start when the model chains the calls. It has to carry each tool's output into the next call.

- In every chronology run, the model passed the split lines on but dropped the AI's field answers (0 of 11 proposals kept them) when it called the save step. The save step rebuilds every item from the database and the stored AI answers, so it saved only what it could prove: the splits. The merged cards were split, and the new cards kept the parser's blank or broken fields.
- In the duplicate runs, the model first cut fields out of the pair objects (7 failed calls), then sent them again.

The lesson is about who carries data between steps. When code carries it (the composed tool), nothing is lost and the model sees a short summary. When the model carries it, large structured data passes through the model twice and comes out changed, at many times the cost. A many-tool API designed for agents could avoid this, for example by keeping proposals on the server and passing short ids. Writing that code is the composition work Synapse generated here from one paragraph per tool.

## Independent review

Before publishing, we had a separate AI reviewer (Codex) attack the code with crafted inputs.

- **In the review code:** body lines that look like headers, a split at an ordinary line, and a date pieced together from two dates could pass the value checks (a person still had to approve them). We fixed all of them, writing tests first. The checks now tie each value to one place in the email.
- **In the inherited emailchrono code:** the issues it found were fixed upstream in emailchrono-local PRs #1 to #4. Those fixes are included in this copy.
- It found no way for a tool to approve its own plan.

## Limits

- **One test case.** All numbers come from one planted case: 18 fake `.eml` files, 21 messages, built to hit known parser gaps. The same author wrote the files, the answer key and the code, so accuracy figures are optimistic. On this case, correct card fields go from 80% to 98%, with 0 wrong values in all 9 composed chronology runs since we tightened the inference rules. The comparison between modes is not affected: both modes ran on identical copies.
- **Few runs.** 3 runs per task and mode; the rehearsal ran each case once. The pattern was the same in every run.
- **Time zones in quote headers.** For a quoted email with no zone in its date, the tool uses the time zone of the sender's other emails. Many mail clients write quote-header times in the replier's time zone instead, so threads across time zones may get the wrong hour. We have not fixed this. The value is marked "inferred" with its evidence.
- **One duplicate pair is always left for the person:** a received copy with a new Message-ID and a gateway footer matches only on weak signals. That is by design.
- **Data leaves the machine for the AI calls.** emailchrono is local-first, but the review sends card text to the OpenAI API. Do not use it on mail that must not leave the machine.

## Synapse-generated code

Two MCP servers are generated by Synapse CLI 0.1.9 in Custom mode (`--local`, OpenAI):

- `backend/review_chronology_server.py`, composed from the functions in `backend/app/review/chronology.py`.
- `backend/resolve_case_duplicates_server.py`, composed from the functions in `backend/app/review/dedupe.py`.

We edited one line by hand in each server, with the same fix both times. Synapse dropped the default on `plan_id`, so we could not call the plan step. We added `= None` (marked with a `# hand edit` comment). We changed nothing else in either file. Synapse's own check does not run the code, so we test every generated file by running it as a real MCP server (`backend/tests/test_generated_servers.py`).

## Setup (macOS)

1. Python venv (3.12) and backend packages:
   ```bash
   uv venv --python 3.12 .venv
   uv pip install --python .venv/bin/python -r backend/requirements.txt pytest
   ```
2. Frontend build:
   ```bash
   (cd frontend && npm ci && npm run build)
   ```
3. Synapse CLI 0.1.9, installed locally (not globally):
   ```bash
   (cd tools/synapse && npm ci)
   scripts/synapse.sh --version
   ```
4. OpenAI key: put `OPENAI_API_KEY=...` in `secrets.local` (gitignored). `scripts/synapse.sh` reads it and always sets `SYNAPSE_TELEMETRY=0`. The agent uses `gpt-5.6-luna` by default (`EMAILCHRONO_AGENT_MODEL` overrides it).
5. Demo data and app:
   ```bash
   .venv/bin/python demo_data/build.py --force   # fresh data/demo with the planted case
   scripts/run_app.sh                            # http://127.0.0.1:8765
   ```

## Reproduce

```bash
.venv/bin/python demo_data/build.py --force              # fresh demo data
scripts/run_app.sh                                       # web app on http://127.0.0.1:8765
.venv/bin/python agent/agent.py "Review the chronology of case 1."
# approve the plan in the web app; the agent then applies it

cd backend && EMAILCHRONO_REVIEW_MODEL_STUB=tests.chronology_stub:respond \
  ../.venv/bin/python -m pytest -q tests/                # offline tests
cd .. && .venv/bin/python bench/run.py --runs 3          # comparison (live, costs tokens)
.venv/bin/python bench/edge_cases.py                     # edge-case rehearsal (live)
```

## Demo data

All demo data is fake. `demo_data/planted.py` writes the planted case (`demo_data/planted/*.eml`) and its answer key (`demo_data/ground_truth.json`). Each file targets one known gap in the parser or duplicate scorer. The `purpose` field in the answer key says which. The build also adds an optional sample case from a local folder of Gmail `.eml` files. That folder is not part of this repo. The build skips the sample case when the folder is missing.

## Source of the app

We copied `backend/` and `frontend/` from [iris-ng/emailchrono-local](https://github.com/iris-ng/emailchrono-local) at commit `c326963` (Apache 2.0, see [LICENSE](LICENSE)). We added the upstream bug fixes merged up to `0a40233` ([PRs #1 to #5](https://github.com/iris-ng/emailchrono-local/pulls?q=is%3Apr+is%3Amerged)). We added or changed these parts for this entry:

- **Review code:** `backend/app/review/` holds the plans and the approval gate (`plans.py`, `approval.py`), the duplicate functions (`dedupe.py`), and the chronology problem finder and functions (`problems.py`, `chronology.py`). `backend/migrations/027_review_plans.sql` and `028_review_plans_guards.sql` add the plans table and its guard triggers. `backend/app/api/review_plans.py` adds the list, show, approve and decline routes for the web app.
- **Changes to emailchrono's own code:** `backend/app/services/snip.py` and `backend/app/repos/emails.py` now have split and field-edit cores that take a database connection, so a plan applies in one transaction, plus inferred-field flags. `backend/app/services/transfer.py` adds an "Inferred fields" column to the `.ecz` Excel sheet.
- **Frontend:** the review banner and plan panel, inferred marks with an evidence tooltip, and an "Inferred fields" column in the Excel export.
- **New files:** the two generated servers, `backend/small_tools_server.py`, `agent/agent.py`, `scripts/approve_plan.py`, `demo_data/`, `bench/`, and 228 backend tests.

## Appendix: the Synapse intents

`review_chronology`:

> Create one MCP tool named review_chronology with the exact signature (case_id: int, plan_id: Optional[str] = None) -> dict, importing Optional from typing, that reviews the chronology of an emailchrono case using only the functions in module app.review.chronology. Without plan_id it builds a plan and changes no data: check the case with check_case_ready_for_chronology_review (if ok is False, return that result), list cards with list_chronology_problem_cards, and for each card call propose_splits(card['email_id']) only when 'merged' is in card['problems'] (otherwise use None), then call propose_fields(card['email_id'], split) and collect every result in a list; then store them with save_chronology_plan(case_id, proposals, warnings from the readiness check) and return its result (plan_id and summary). With plan_id it calls apply_chronology_plan(case_id, plan_id) and returns the result unchanged, which may say the plan is still waiting for approval in emailchrono. The tool must never approve or decline a plan, and must not use app.review.approval, app.api or any HTTP call.

`resolve_case_duplicates`:

> Create one MCP tool named resolve_case_duplicates with the exact signature (case_id: int, plan_id: Optional[str] = None) -> dict, importing Optional from typing, that cleans up duplicate emails in an emailchrono case, using only the functions in module app.review.dedupe. Without plan_id it builds a plan and changes no data: check the case is ready with check_case_ready_for_duplicate_review (if ok is False, return that result), list the pending pairs with list_pending_duplicate_pairs, propose a decision for each pair with propose_duplicate_decision, then store them with save_duplicate_plan passing the readiness warnings, and return its result (plan_id and summary). With plan_id it calls apply_duplicate_plan(case_id, plan_id) and returns the result unchanged, which may say the plan is still waiting for approval in emailchrono. The tool must never approve or decline a plan, and must not use app.review.approval, app.api or any HTTP call.

## License

Apache 2.0, see [LICENSE](LICENSE). The emailchrono code is from [iris-ng/emailchrono-local](https://github.com/iris-ng/emailchrono-local) under the same license.
