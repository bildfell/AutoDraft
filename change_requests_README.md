# Change-request and reoptimization previews

Place the updated `change_requests.py`, `auto_drafter.py` and `teams_report.py` together in your
project. Run from the folder containing `data/` and `output/`. Dependencies:

```bash
python -m pip install pandas ortools reportlab matplotlib
```

## 1. Keep the accepted baseline frozen

If `output/draft_baseline.json` already exists, keep it. It remains compatible
with this update; do not recapture or rerun the accepted draft.

If you have not captured it, retain the accepted run's `player_evidence.csv`
and `assessment_summary.csv`, with the corresponding inputs and configuration:

```bash
python change_requests.py --capture-baseline
```

Capture checks the saved evidence against the current inputs, imports the
existing assignments and writes `output/draft_baseline.json` without optimizing.
A mismatch stops capture. Historical practice-day configuration is not in
those CSVs, so keep that configuration unchanged when capturing an older run.

For an accepted run that already has its own snapshot, use this alternative:

```bash
python change_requests.py --capture-baseline --from-run output/latest_draft.json
```

An existing baseline is never overwritten. `latest_draft.json` changes on normal
drafter runs; `draft_baseline.json` stays fixed. These snapshots are not
persistent generated-ID reservation registries.

## 2. Specify swaps or withdrawals

The CSV format is unchanged. Copy `change_requests_template.csv` to
`data/change_requests.csv` if needed. Replace these illustrative names:

```csv
Proposal,Player A,Player A DOB,Player B,Player B DOB,Reason
swap_01,Alex Example,,Jordan Example,,Coach-requested swap
swap_02,Casey Example,02/14/2016,Morgan Example,,Another option
withdraw_01,Taylor Example,,,,Possible withdrawal if accommodation is unavailable
```

- Full names match without case sensitivity. DOB is optional except for duplicate
  names; use MM/DD/YYYY.
- Different proposal names are independent alternatives starting from the baseline.
- A blank `Player B` **withdraws Player A from the league** in the preview. Leave
  `Player B DOB` blank too. Two names still specify a swap.
- Rows with the same proposal name form simultaneous changes, including mixed
  swaps and withdrawals. A player may appear
  only once in a combined proposal.
- Reasons are optional. Quote cells containing commas.
- Same-player, same-team and unresolved-name requests get diagnostic reports.

## 3. Generate the PDF comparison

```bash
python change_requests.py
```

Each valid proposal now compares three drafts:

1. **Baseline:** the frozen baseline.
2. **Exact swap / Exact change:** requested swaps are applied and withdrawn
   players are removed; everyone else stays on their baseline team.
3. **Reoptimized:** withdrawn players remain excluded and swapped players stay
   on their requested destination teams;
   everyone else may move under the original constraints and priorities.

For withdrawals, team-size bounds are recalculated from the remaining roster.
The immediate removal may temporarily violate those bounds; reoptimization
still runs to try to repair the sizes. A withdrawal also removes that player's
assessment evidence and friendship edges from the scenario and rebuilds the
surviving friend groups. Requests involving a withdrawn player are explicitly
labelled `Withdrawn`, separate from failed requests between remaining players.
Friendship counts reflect the smaller roster. Saved ratings and prepared
secondary-pool membership for the remaining players are retained.

The main artifact is `output/change_requests/01_<proposal>/comparison.pdf`.
`comparison.html` contains the same comparison as an optional browser view.
Detailed Balance plots compare all three drafts with teams in rows and draft
alternatives in columns, using the same scales, range bars and imputation
symbols as the baseline. Roster slots are sorted separately in each draft; a
position is not the same player across columns.
Keep detailed_balance.png beside the HTML when copying or sharing that view.

The report leads with Overall Level spread (highest team mean minus lowest),
team averages, and secondary coverage. It then shows friendships restored
following the change, still lost, newly broken during reoptimization, or newly
honoured. Changed friend groups are grouped by team even when the entire group
moves together. It identifies players without a groupmate, distinguishes
requested from additional moves, and includes all assignments and solver status.
Friend-request counts are directed: mutual requests count twice; mutual-pair
counts count the pair once. Isolation considers only players with a friend group.

Reoptimization reruns the existing staged objective using the snapshot's
prepared ratings, eligibility and pools. The +0.10 tolerance is relative to the
scenario's balance pass **with swap destinations locked and withdrawn players excluded**, which may produce a different
ceiling from the baseline. Movement minimization is the final tie-breaker: it
preserves achieved friendship counts, capped secondary coverage, unobserved
count spread and Overall Level spread. It can still require substantial moves.

The default time limit is 20 seconds **per pass**, with up to seven passes.
Allow more time when needed:

```bash
python change_requests.py --solver-seconds 30
```

OPTIMAL refers to the individual objective/pass. FEASIBLE means a candidate
was found within the time limit, without proof that it is best. If a later
pass cannot improve its incumbent, the earlier feasible draft is retained.
A failed reoptimization still produces the exact-change comparison with the
reoptimized outcome explicitly marked unavailable.

## Output files

| File | Contents |
| --- | --- |
| `comparison.pdf` | Main shareable comparison |
| `comparison.html` | Secondary browser view of the same content |
| `detailed_balance.png` | Three-way Detailed Balance graphs embedded in the PDF and linked by HTML |
| `moves.csv` | Requested swaps/withdrawals and reasons |
| `additional_moves.csv` | Other players moved from the baseline by reoptimization |
| `balance_comparison.csv` | Overall Level spread, moves, active players and withdrawals in all three drafts |
| `team_comparison.csv` | Per-team metrics, changes from baseline and imputation counts |
| `friend_changes.csv` | Changed directed requests, three statuses, and outcome classification |
| `friend_group_changes.csv` | Changed placements grouped by team across the drafts |
| `proposal_roster.csv` | Player assignments (or Withdrawn), exact-change and reoptimized validity flags |
| `reoptimized_draft.json` | Separate snapshot of the feasible reoptimized proposal |
| `solver_results.json` | Pass statuses, achieved metrics and movement status |
| `proposal_manifest.json` | Preview provenance and requested placements for acceptance |

Team eligibility, accepted placement locks, blackouts and sizes remain hard
constraints for remaining players. A swap violating placement or availability
rules gets a diagnostic report without reoptimization. Size violations in the
immediate withdrawal preview are shown, and reoptimization attempts to repair
them. Withdrawing a player removes that player's fixed/accepted assignment.
A scenario must leave at least one player per team. Coverage and friendship losses are reported trade-offs.
No preview accepts a proposal or modifies the accepted baseline, `latest_draft.json`,
`teams_report.pdf`, or other accepted outputs. Rerunning a proposal replaces
only that proposal folder's script-owned outputs and removes stale results.

Optional paths:

```bash
python change_requests.py --baseline output/draft_baseline.json --requests data/change_requests.csv --output output/change_requests
```

Exit codes: 0 = all previews completed (or no requests); 1 = invalid request,
hard constraint violation or unavailable reoptimization, with reports written;
2 = setup/capture error. Ratings consensus overrides remain future work.

## 4. Accept one proposal and continue from it

Preview alternatives first, review their PDF, then accept the chosen
**reoptimized** result. Acceptance does not rerun the optimizer. For example:

```bash
python change_requests.py --accept output/change_requests/01_swap_01 --baseline output/draft_baseline.json --requests data/change_requests.csv --new-baseline output/draft_baseline_v2.json --remaining-requests data/change_requests_v2.csv
```

This writes a new baseline containing all assignments in the reviewed result,
carries previous accepted locks forward, and locks the explicitly requested
players to their reviewed destination teams. Accepted withdrawals remove those
players and their placement locks from the new baseline; the change history
records their IDs. Extra accommodation moves remain
movable. It removes all rows belonging to the accepted proposal from a new CSV;
the original baseline and request file remain unchanged. New output filenames
must not already exist. Only valid, complete reoptimized proposals are accepted.

Regenerate outstanding previews using both newly written files:

```bash
python change_requests.py --baseline output/draft_baseline_v2.json --requests data/change_requests_v2.csv --output output/change_requests_v2
```

Those alternatives are independent relative to **v2**, with accepted placements
enforced. Requests naming an already withdrawn player get a diagnostic report. Pending
swap destinations are recomputed from the current accepted
teams. A request conflicting with an accepted lock gets a diagnostic report.
Accept a subsequent proposal into v3 and repeat. Use a different output folder
per baseline version to keep comparisons easy to distinguish.

Multiple rows under one Proposal are accepted together. If coaches want several
requests considered as one package, use a shared Proposal name from the start.
After an acceptance, old outstanding previews are outdated and must be rerun;
the command does not automatically regenerate or relabel old PDF files.

Acceptance checks that the current baseline, selected request rows and reviewed
snapshot still match the preview. `proposal_manifest.json` records that
provenance and the requested locks. Previews made before this update need to be
regenerated once to create that manifest. Snapshots retain accepted locks and a
change history, while preserving original coach/family constraints separately.

Team days in previews and acceptance come from the frozen snapshot. Editing
`data/teams_config.csv` affects new auto_drafter runs, not previously saved drafts.
