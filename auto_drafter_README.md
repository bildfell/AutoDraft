# Auto Drafter

Auto Drafter prepares balanced recreational basketball teams from registration
constraints and independent coach evaluations. Google OR-Tools solves the draft
as a constrained optimization problem. The output includes team rosters, a PDF
showing balance and assessment evidence, and friendship reports.

The draft is a proposal for coach review. It does not send reports or accept
changes automatically.

## Quick start

With your Python environment active, run from the project folder:

```bash
pip install -r requirements.txt
python auto_drafter.py
```

Before running, populate `data/constraints.csv`, add coach evaluation CSVs to
`data/evaluations/`, and set the team practice days in `data/teams_config.csv`.
Header-only templates establish the format; they need actual player rows
before a draft can be generated. There must be at least one player per team.

## Project layout

| Path | Purpose |
| --- | --- |
| `auto_drafter.py` | Input preparation, hard constraints, staged optimization and exports |
| `teams_report.py` | Team Draft Report and Detailed Balance charts |
| `change_requests.py` | Optional swap/withdrawal and reoptimization previews |
| `requirements.txt` | Pinned direct dependencies tested for this starter package |
| `data/teams_config.csv` | Team IDs and practice days; team count derives from its rows |
| `data/constraints.csv` | Registration roster and player constraints |
| `data/evaluations/evaluations.csv` | Starter evaluation CSV; rename or add files for each evaluator |
| `data/report_config.csv` | Optional club/division subtitle, with exactly one data row |
| `data/change_requests.csv` | Optional named swap/withdrawal proposals |
| `output/` | Generated reports and draft snapshots |

Keep the Python scripts together. Evaluation CSVs belong inside
`data/evaluations/`, rather than directly inside `data/`. Any number of CSVs
can be used; filenames such as `CoachJim.csv` identify the assessment source.
Files with an unrecognized evaluation header are skipped and listed.

## Registration input: constraints.csv

The first row is the header. Headers are trimmed and normalized for casing.
The supplied template contains all supported registration columns; optional
columns may be left empty or omitted.

| Column | What to enter |
| --- | --- |
| `First Name`, `Last Name` | Required for every player |
| `Dob` | Required; month/day/year, e.g. `02/14/2016` |
| `Sort Out #` | The player's evaluation number, if assigned; otherwise blank or `Can't attend` |
| `Team` | Optional acceptable team number(s): `2`, `2 3`, `2 or 3`, or `2 and 3`; a single number fixes the player to that team |
| `No Practice Date` | A single weekday: Monday, Tuesday, Wednesday, Thursday or Friday; blank means unrestricted |
| `Buddy Request` | Optional free-form friend names; use registered full names when possible |
| `Height` | Optional dropdown range including centimetres, e.g. `5' 3" to 5' 7" (160 cm to 170 cm)` |
| `Player Assessment`, `Returning Player` | Retained registration fields; do not affect the current optimization |

Sort-out numbers must be unique whole numbers. Numeric representations such as
`12.0` normalize to `12`. Missing numbers receive readable numbers above all
reserved input IDs, using normalized name and DOB to order allocation. The
reservation lookup lives in memory only. Generated IDs may change across fresh
runs if the inputs change; snapshots preserve the identity used in their run.

Friend names are matched within free-form text, so a recognized name can still
match inside a numbered list or carpool comment. Ambiguous registered names
require review; `FRIEND_OVERRIDES` can replace a player's requests using IDs.
Connected friend groups describe request connectivity, not necessarily mutual
friendship. Requests remain directional, with mutual requests identified.

## Evaluation input

The minimal recognized header contains `#`, `First Name`, `Last Name` and at
least one rating column or `Notes`. The template includes:

```csv
#,First Name,Last Name,Notes,Shooting,Ball-Handling,Rebounding,Overall Level
```

Ratings are numeric from 1 through 5. Blank ratings are allowed, including
partially assessed players and entirely blank assessment rows. Missing metric
columns are treated as missing ratings. Decorative introductory rows before
the real header are supported, including quoted multiline titles.

**Evaluations join to registrations by sort-out number, not by name.** Names
are required in evaluation rows but do not repair a missing or incorrect ID.
Use the same number in the roster and every evaluator file. Each evaluator
file may contain a given nonblank number only once. Rows without a usable
number and evaluation numbers absent from registration are surfaced for review.

For each player and metric, the drafting rating is the arithmetic mean of the
available scores. If no score exists for that metric, its drafting rating is
3. No imputed values are mixed into an observed mean. Assessment evidence
retains count, observed mean, min/max and range, along with evaluator sources.
A single assessment has no reported disagreement range; it is not consensus.

Height comes from registration. A positive whole-word `Tall` description in
coach Notes also contributes tall evidence. Negative or uncertain mentions
are flagged for review. The tall pool combines the highest recorded height
band and positive Tall notes, counting a player once. Missing height is unknown.
The note parser is deliberately simple; check flagged descriptions manually.

`No assessment data` means no matching evaluation row. A matched row with
blank scores still has a match, even though its ratings may all be imputed.

## Configuration

Set practice days in `data/teams_config.csv` before a normal draft:

```csv
Team,Practice Day
1,Monday
2,Thursday
3,Tuesday
4,Wednesday
```

Team count comes from the rows; team IDs must be unique and consecutive from 1.
Weekday casing and surrounding whitespace are normalized. Team specifications in
constraints.csv must refer to these IDs. The current default days are:

| Team | Practice day |
| --- | --- |
| 1 | Monday |
| 2 | Thursday |
| 3 | Tuesday |
| 4 | Wednesday |

The primer PDF layout is designed for four teams; review its layout before
changing team count. Other optimizer settings remain near the top of
`auto_drafter.py`.

| Setting | Current meaning/default |
| --- | --- |
| `FRIEND_BALANCE_TOLERANCE` | Allow up to +0.10 rating points above the balance-pass spread |
| `TOP_BALL_HANDLER_COUNT` | Observed top pool of 8 players, including all cutoff ties |
| `TALL_TARGET_PER_TEAM` | `None`: target 2 if the flagged pool permits, otherwise 1; `0` disables the target |
| `SECOND_YEAR_TARGET_PER_TEAM` | Soft target of 2 older-cohort players per team |
| `COHORT_BIRTH_YEARS` | Infer two consecutive roster birth years; optionally supply the eligible pair explicitly |
| `BALANCE_UNOBSERVED_PLAYERS` | `True`: spread players with no evaluation match across teams |
| `SOLVER_SECONDS_PER_PASS` | 20 seconds per solve; total runtime spans several passes |
| `SOLVER_RANDOM_SEED` | Search seed, default 42 |
| `RUN_OPTIMIZATION` | `True`; `False` inspects initial placements, which may be incomplete |
| `GENERATE_TEAM_REPORT_PDF` | Generate the PDF after an optimized draft |
| `EXPORT_FRIEND_REPORT` | Export named requests and grouped placements in friend_requests.csv |
| `EXPORT_ASSESSMENT_REPORTS` | Export assessment evidence and notes |
| `OUTPUT_FOLDER` | Output destination, default `output/` |

For a personalized subtitle, copy `report_config_template.csv` to
`data/report_config.csv` and add exactly one row with `Club Name` and
`Division Details`. Omit the optional file until populated: a header-only
report_config.csv is not a valid configuration. This file survives code updates.

## Optimization methodology

Hard constraints enforce the existing `Team` specifications, practice blackouts
and team sizes differing by at most one player. Blank Team allows any configured
team; one number fixes a destination; multiple numbers allow any listed team.
For example, `2 3`, `2 or 3` and `2 and 3` all mean Team 2 OR Team 3 is acceptable.
`and` is a list separator, not a request to assign a player to multiple teams.
No extra registration column is needed. Separators use spaces; casing is ignored.
Duplicate numbers are counted once. Other text or unknown team IDs are rejected.

Eligibility is the intersection of this list, practice availability and accepted
placement locks. If that intersection is empty, the program raises a conflict
for resolution. These hard restrictions take precedence over friend requests.
Editing constraints.csv changes new auto_drafter runs; frozen change-request
baselines retain their saved Team specifications until a new baseline is captured.

The first solve minimizes **Overall Level spread**: highest team mean minus
lowest team mean. Means include the prepared ratings and missing-score defaults.
Internally scores are rounded to 0.001; reported means use prepared values.

Within the balance pass's +0.10 allowance, preferences prioritize mutual pairs,
players with a direct friend, and players with a groupmate. Coverage of up to
2 top observed ball-handlers per team comes before remaining honoured requests.
Further passes target tall evidence, second-year coverage and the distribution
of players with no evaluation match, preserving achieved earlier credits.
Extra ball-handlers beyond the target earn no additional coverage credit.
Shooting is reported for discussion and does not affect optimization.

All friendship/coverage goals are soft. Availability, coach assignments,
capacity and balance may require a split or an unmet target. A selected split
is not labelled unavoidable unless a specific hard restriction proves it.
`OPTIMAL` describes an individual solve; `FEASIBLE` means a time-limited candidate
without proof of optimality. Later passes retain the incumbent when they cannot
improve it. Equal team averages do not imply identical ability distributions.

## Outputs and interpretation

| Output | Contents |
| --- | --- |
| `teams_report.pdf` | Rosters/methodology, Detailed Balance charts, assessment appendix and friendship appendix |
| `friend_requests.csv` | Named directed requests, assignments, status, reason and accommodation |
| `assessment_summary.csv` | Player/metric evidence, observed range, imputation and evaluator scores |
| `player_evidence.csv` | Prepared player evidence, pools, cohorts, missingness and assignments |
| `evaluation_notes.csv` | Notes with source and registration-match status |
| `latest_draft.json` | Complete run snapshot for future baseline capture |

Normal runs replace their generated outputs, including `latest_draft.json`.
Keep a frozen baseline before comparing changes to an accepted primer.
The charts show multiple-assessment means with observed min/max bars, single
assessments as triangles and imputed Overall Level ratings as hollow diamonds
at the right with dotted 1-5 guides. Those guides express missing evidence;
they are not measured ranges or confidence intervals. Dashed lines show team
means. Roster slot numbers connect the main PDF's names to its chart points.

The PDF and snapshots contain identifiable player assessment information and
are intended for coach review. Leaving ratings out of roster tables does not
remove them from identifiable chart points or the appendix.

## Swap previews

See `change_requests_README.md` for baseline capture and the request format.
With a frozen baseline and `data/change_requests.csv` in place:

```bash
python change_requests.py
```

Every proposal compares the primer, exact swap and reoptimized draft, including
Detailed Balance graphs. Requested destinations stay fixed during reoptimization;
other players may move. The accepted baseline and normal drafter outputs remain
unchanged. Fewer additional moves are a final tie-breaker after achieved priorities.
Current input CSVs are not read during previews; the snapshot's evidence is used.
Use the acceptance command in change_requests_README.md to save a new baseline,
retain accepted placements as locks, remove the accepted request from a new CSV,
and then regenerate remaining previews. Consensus rating overrides remain future work.

## Sharing a clean project

Make a separate sharing copy. Include the three Python scripts, the two READMEs,
your existing frozen `requirements.txt`, and header-only input templates.
Include data/teams_config.csv with the default four-team practice days.
Keep just one starter file, `data/evaluations/evaluations.csv`. Recipients can
rename it and add evaluator files as needed. Keep `report_config_template.csv`
outside `data/` until they fill it in.

Exclude all populated registration/evaluation/request files, personalized config,
`output/`, baseline and scenario JSON snapshots, generated reports and assessment
exports, `.venv/`, caches, backups and other copies of player data. Do not strip
or overwrite the working data that produced your accepted draft.

The supplied clean starter bundle contains code, header-only player/request
CSV templates and the populated four-team configuration.
Add your own frozen `requirements.txt`: it was not supplied in this workspace,
so the bundle does not substitute a freeze from a different environment.

## Common input issues

- Missing names or malformed DOB: correct registration; dates use MM/DD/YYYY.
- Evaluation file skipped: check the `#`, name headers and rating/Notes header.
  Avoid manually retaining literal Markdown `**` markers around CSV headers.
- Player unexpectedly imputed: verify the sort-out number matches registration
  and the relevant metric is actually populated.
- Duplicate evaluation ID: retain one row per player in each evaluator file.
- Blackout/fixed-team conflict or infeasible draft: review hard constraints.
- No feasible solution before timeout: increase `SOLVER_SECONDS_PER_PASS` and
  check eligibility/capacity. More time does not repair contradictory rules.
- Cannot infer age cohorts: set the two consecutive `COHORT_BIRTH_YEARS`.
- Invalid report configuration: use one data row or omit the optional file.
