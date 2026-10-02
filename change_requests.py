"""Preview swaps, withdrawals and reoptimized drafts against a frozen baseline.

Run beside data/ and output/. See change_requests_README.md for examples.
Requires pandas, matplotlib, reportlab, OR-Tools, auto_drafter.py and teams_report.py.
"""

import argparse
from copy import deepcopy
import hashlib
from html import escape
import json
from pathlib import Path
import re
import shutil

import pandas as pd

import auto_drafter as ad


REQUEST_COLUMNS = [
    "Proposal", "Player A", "Player A DOB", "Player B", "Player B DOB", "Reason",
]


def load_baseline(path):
    """Restore the saved draft, independent of current input CSVs or settings."""
    with Path(path).open(encoding="utf-8") as source:
        payload = json.load(source)
    if payload.get("schema_version") != 1:
        raise ValueError("Unsupported baseline format; capture a new snapshot")
    practice_days = {int(team): day for team, day in payload["practice_days"].items()}
    # Existing drafter helpers use these module settings. Take them from the
    # snapshot so changing today's config cannot alter yesterday's baseline.
    ad.TEAM_PRACTICE_DAYS = practice_days
    ad.TEAM_COUNT = len(practice_days)
    ad.FRIEND_BALANCE_TOLERANCE = payload["settings"]["friend_balance_tolerance"]
    ad.SECOND_YEAR_TARGET_PER_TEAM = payload["settings"]["second_year_target"]
    ad.BALANCE_UNOBSERVED_PLAYERS = payload["settings"].get("balance_unobserved_players", True)
    drafter = ad.AutoDrafter()
    drafter.df_players = pd.DataFrame(payload["players"])
    drafter.df_players[ad.SORT_OUT_NUMBER] = drafter.df_players[ad.SORT_OUT_NUMBER].astype("string")
    if drafter.df_players[ad.SORT_OUT_NUMBER].duplicated().any():
        raise ValueError("Baseline contains duplicate player IDs")
    drafter.df_evaluation_evidence = pd.DataFrame(payload["evaluation_evidence"])
    drafter.friend_requests = {tuple(pair) for pair in payload["friend_requests"]}
    drafter.friend_groups = payload["friend_groups"]
    drafter.tall_target_per_team = payload["settings"]["tall_target"]
    drafter.report_details = payload.get("report_details", {})
    drafter.accepted_locks = {str(pid): int(team) for pid, team in payload.get("accepted_locks", {}).items()}
    drafter.change_history = payload.get("change_history", [])
    drafter.set_assignments(pd.Series(payload["assignments"], dtype="Int64"))
    saved_allowed = {pid: set(teams) for pid, teams in payload["allowed_teams"].items()}
    if saved_allowed != {pid: set(teams) for pid, teams in drafter.allowed_teams.items()}:
        raise ValueError("Baseline eligibility is inconsistent with its saved constraints")
    if list(drafter.team_size_bounds) != payload["settings"]["size_bounds"]:
        raise ValueError("Baseline team-size bounds are inconsistent")
    return drafter, payload


def check_report_matches(saved, current, keys, name):
    """Refuse a capture when the existing report differs from current inputs."""
    if set(saved.columns) != set(current.columns):
        raise ValueError(f"{name}: columns differ; use matching drafter outputs")
    saved = saved.sort_values(keys).reset_index(drop=True)[current.columns]
    current = current.sort_values(keys).reset_index(drop=True)
    # CSV empty cells, pandas.NA, and JSON null represent the same missing
    # value here. Normalize both sides before comparing evidence.
    saved = saved.astype(object).where(saved.notna(), None).map(
        lambda value: None if value == "" else value
    )
    current = current.astype(object).where(current.notna(), None).map(
        lambda value: None if value == "" else value
    )
    try:
        pd.testing.assert_frame_equal(
            saved, current, check_dtype=False, check_exact=False,
            rtol=1e-9, atol=1e-9,
        )
    except AssertionError as exc:
        raise ValueError(
            f"{name} differs from the current input data. Restore the inputs "
            "used for the baseline, or capture its saved latest_draft.json. "
            "No baseline was written."
        ) from exc


def capture_baseline(destination, from_run=None, reports_folder=None):
    destination = Path(destination)
    if destination.exists():
        raise ValueError(
            f"{destination} already exists and will not be overwritten. "
            "Use --baseline with a different filename for another baseline."
        )
    if from_run:
        load_baseline(from_run)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with Path(from_run).open("rb") as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target)
    else:
        folder = Path(reports_folder or ad.OUTPUT_FOLDER)
        evidence_path = folder / "player_evidence.csv"
        assessments_path = folder / "assessment_summary.csv"
        if not evidence_path.exists() or not assessments_path.exists():
            raise ValueError(
                "Baseline capture needs player_evidence.csv and "
                "assessment_summary.csv from the accepted run. For a new run, "
                "use --capture-baseline --from-run output/latest_draft.json."
            )
        drafter = ad.AutoDrafter()
        drafter.read_data()
        drafter.transform_data()
        drafter.resolve_friend_groups()
        drafter.read_report_config()
        evidence = pd.read_csv(evidence_path, encoding="utf-8-sig")
        assessments = pd.read_csv(assessments_path, encoding="utf-8-sig")
        labels = drafter.player_labels()
        by_label = {label: pid for pid, label in labels.items()}
        if len(by_label) != len(labels):
            raise ValueError("Baseline player labels are ambiguous")
        if evidence["Player"].duplicated().any() or set(evidence["Player"]) != set(by_label):
            raise ValueError("The saved roster does not match current registrations")
        assignments = pd.Series({
            by_label[row["Player"]]: row["Team"]
            for _, row in evidence.iterrows()
        }, dtype="Int64")
        drafter.set_assignments(assignments)
        check_report_matches(
            evidence, drafter.get_player_evidence_report(), ["Player"],
            "player_evidence.csv",
        )
        check_report_matches(
            assessments, drafter.get_assessment_report(), ["Player", "Metric"],
            "assessment_summary.csv",
        )
        friend_path = folder / ad.FRIEND_REPORT_FILE
        if friend_path.exists():
            check_report_matches(
                pd.read_csv(friend_path, encoding="utf-8-sig"),
                drafter.get_friend_report(), ["Player", "Requested Friend"],
                ad.FRIEND_REPORT_FILE,
            )
        drafter.export_draft_snapshot(destination)
    print(f"Frozen baseline saved to {destination}. No optimization was run.")


def resolve_player(drafter, name, dob=""):
    players = drafter.df_players
    full_names = (players[ad.FIRST_NAME] + " " + players[ad.LAST_NAME]).map(ad.normalize_text)
    matches = players.loc[full_names.eq(ad.normalize_text(name))]
    if dob:
        try:
            date = pd.to_datetime(dob, format=ad.DOB_FORMAT, errors="raise")
        except ValueError as exc:
            raise ValueError(f"DOB for {name!r} must use MM/DD/YYYY") from exc
        dates = pd.to_datetime(matches[ad.DOB], format=ad.DOB_FORMAT, errors="raise")
        matches = matches.loc[dates.eq(date)]
    if matches.empty:
        raise ValueError(f"No baseline player matches {name!r}" + (f" with DOB {dob}" if dob else ""))
    if len(matches) != 1:
        raise ValueError(f"More than one player matches {name!r}; specify DOB")
    return str(matches.iloc[0][ad.SORT_OUT_NUMBER])


def apply_requests(drafter, rows):
    assignments = drafter.assignments.copy()
    used = set()
    moves = []
    withdrawn = set()
    labels = drafter.player_labels()
    for _, row in rows.iterrows():
        left = resolve_player(drafter, row["Player A"], row["Player A DOB"])
        if not row["Player B"]:
            if left in used:
                raise ValueError("Each player may appear only once within a combined proposal")
            used.add(left)
            withdrawn.add(left)
            moves.append({"Player": labels[left], "Before": int(assignments[left]),
                          "After": "Withdrawn", "Reason": row["Reason"]})
            continue
        right = resolve_player(drafter, row["Player B"], row["Player B DOB"])
        if left == right:
            raise ValueError("A player cannot be swapped with themselves")
        if used.intersection({left, right}):
            raise ValueError("Each player may appear only once within a combined proposal")
        used.update({left, right})
        left_team, right_team = int(drafter.assignments[left]), int(drafter.assignments[right])
        if left_team == right_team:
            raise ValueError(f"{labels[left]} and {labels[right]} are already on the same team")
        assignments[left], assignments[right] = right_team, left_team
        for pid, old, new in [(left, left_team, right_team), (right, right_team, left_team)]:
            moves.append({"Player": labels[pid], "Before": old, "After": new,
                          "Reason": row["Reason"]})
    return assignments.drop(list(withdrawn)), pd.DataFrame(moves), withdrawn


def remove_players(drafter, withdrawn):
    """Rebuild scenario connectivity from surviving frozen requests."""
    if not withdrawn:
        return
    drafter.df_players = drafter.df_players.loc[
        ~drafter.df_players[ad.SORT_OUT_NUMBER].isin(withdrawn)
    ].copy()
    if len(drafter.df_players) < ad.TEAM_COUNT:
        raise ValueError("Withdrawal must leave at least one player per team")
    drafter.assignments = drafter.assignments.drop(list(withdrawn))
    drafter.accepted_locks = {pid: team for pid, team in drafter.accepted_locks.items()
                             if pid not in withdrawn}
    evidence = drafter.df_evaluation_evidence
    if evidence is not None and ad.SORT_OUT_NUMBER in evidence:
        drafter.df_evaluation_evidence = evidence.loc[
            ~evidence[ad.SORT_OUT_NUMBER].astype("string").isin(withdrawn)
        ].copy()
    drafter.friend_requests = {pair for pair in drafter.friend_requests
                              if not withdrawn.intersection(pair)}
    adjacency = {pid: set() for pid in drafter.assignments.index}
    for left, right in drafter.friend_requests:
        adjacency[left].add(right)
        adjacency[right].add(left)
    groups, visited = [], set()
    for start in sorted(adjacency):
        if start in visited:
            continue
        pending, group = [start], []
        visited.add(start)
        while pending:
            pid = pending.pop()
            group.append(pid)
            for friend in sorted(adjacency[pid]):
                if friend not in visited:
                    visited.add(friend)
                    pending.append(friend)
        groups.append(sorted(group))
    drafter.friend_groups = groups
    drafter.build_allowed_teams()


def constraint_violations(drafter, assignments, check_sizes=True):
    labels = drafter.player_labels()
    violations = []
    for _, player in drafter.df_players.iterrows():
        pid = player[ad.SORT_OUT_NUMBER]
        team = int(assignments[pid])
        if pid in drafter.accepted_locks and team != drafter.accepted_locks[pid]:
            violations.append(f"{labels[pid]} is locked to accepted Team {drafter.accepted_locks[pid]}; the proposal places them on Team {team}.")
        choices = ad.parse_team_choices(player[ad.FIXED_TEAM])
        if choices is not None and team not in choices:
            permitted = ", ".join(str(value) for value in sorted(choices))
            violations.append(f"{labels[pid]} allows only Team(s) {permitted}; the proposal places them on Team {team}.")
        blocked = drafter.parse_blackout_day(player[ad.BLACKOUT_DAYS])
        if blocked and drafter.practice_days[team] == blocked:
            violations.append(f"{labels[pid]} cannot practise on {blocked}; Team {team} practices on {blocked}.")
    if not check_sizes:
        return violations
    minimum, maximum = drafter.team_size_bounds
    counts = assignments.value_counts()
    for team in drafter.practice_days:
        count = int(counts.get(team, 0))
        if not minimum <= count <= maximum:
            violations.append(f"Team {team} has {count} players; allowed size is {minimum}-{maximum}.")
    return violations


def team_summary(drafter):
    rows = []
    observed = drafter.df_players.loc[
        drafter.df_players["Shooting Assessment Count"].gt(0),
        "Shooting Observed Mean",
    ].dropna()
    cutoff = observed.nlargest(min(8, len(observed))).min() if len(observed) else None
    for team, players in drafter.teams.items():
        rows.append({
            "Team": team, "Players": len(players),
            "Average Overall Level": float(players[ad.OVERALL].mean()),
            "Top Ball-Handlers": int(players["Top Ball-Handling Pool"].sum()),
            "Top Shooters": int((
                players["Shooting Assessment Count"].gt(0)
                & players["Shooting Observed Mean"].ge(cutoff)
            ).sum()) if cutoff is not None else 0,
            "Tall Players": int(players["Tall Evidence Pool"].sum()),
            "Second Years": int(players["Age Year"].eq(2).sum()),
            "No assessment data": int(players["No Evaluation Match"].sum()),
            "Overall Level estimated": int(players[f"{ad.OVERALL} Imputed"].sum()),
        })
    return pd.DataFrame(rows).set_index("Team")


def comparison_tables(before, after, payload):
    original, proposed = team_summary(before), team_summary(after)
    rows, missed = [], []
    targets = {
        "Top Ball-Handlers": payload["settings"]["handler_target"],
        "Tall Players": payload["settings"]["tall_target"],
        "Second Years": payload["settings"]["second_year_target"],
    }
    for team in original.index:
        for metric in original.columns:
            old, new = original.at[team, metric], proposed.at[team, metric]
            rows.append({"Team": team, "Measure": metric, "Before": old,
                         "After": new, "Change": new - old,
                         "Goal": targets.get(metric, "")})
            if metric in targets and old >= targets[metric] > new:
                missed.append(f"Team {team}: {metric} falls from {int(old)} to {int(new)} (goal {targets[metric]}).")
    old_stats, new_stats = before.friendship_statistics(), after.friendship_statistics()
    old_groups, new_groups = before.get_friend_group_report(), after.get_friend_group_report()
    system = [{
        "Measure": "Team-to-team spread in Overall Level",
        "Before": original["Average Overall Level"].max() - original["Average Overall Level"].min(),
        "After": proposed["Average Overall Level"].max() - proposed["Average Overall Level"].min(),
    }]
    for label, key in [
        ("Friend requests honoured", "honoured_requests"),
        ("Mutual pairs together", "mutual_pairs_kept"),
        ("Players with a direct friend", "direct_companions"),
        ("Players with a friend-group member", "group_companions"),
    ]:
        system.append({"Measure": label, "Before": old_stats[key], "After": new_stats[key]})
    system.append({"Measure": "Players without a friend-group member",
                   "Before": len(old_stats["isolated_players"]),
                   "After": len(new_stats["isolated_players"])})
    system.append({"Measure": "Split friend groups",
                   "Before": int(old_groups["Status"].str.contains("Split").sum()),
                   "After": int(new_groups["Status"].str.contains("Split").sum())})
    system = pd.DataFrame(system)
    system["Change"] = system["After"] - system["Before"]
    friend_changes = []
    labels = before.player_labels()
    for left, right in sorted(before.friend_requests):
        old = bool(before.assignments[left] == before.assignments[right])
        new = bool(after.assignments[left] == after.assignments[right])
        if old != new:
            friend_changes.append({"Player": labels[left], "Requested Friend": labels[right],
                                   "Before": "Honoured" if old else "Split",
                                   "After": "Honoured" if new else "Split"})
    group_changes = []
    for index in old_groups.index:
        if old_groups.at[index, "Assignments"] != new_groups.at[index, "Assignments"]:
            group_changes.append({"Before": old_groups.at[index, "Assignments"],
                                  "After": new_groups.at[index, "Assignments"],
                                  "Status Before": old_groups.at[index, "Status"],
                                  "Status After": new_groups.at[index, "Status"]})
    return (original, proposed, pd.DataFrame(rows), system,
            pd.DataFrame(friend_changes, columns=["Player", "Requested Friend", "Before", "After"]),
            pd.DataFrame(group_changes, columns=["Before", "After", "Status Before", "Status After"]), missed)


def table_html(frame):
    if frame.empty:
        return "<p>None.</p>"
    return frame.to_html(
        index=False, border=0, escape=True,
        float_format=lambda value: str(int(value)) if value.is_integer()
        else f"{value:.3f}",
    )


def write_pdf(path, name, sections):
    """Render the same comparison content as a paginated, shareable PDF."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image

    styles = getSampleStyleSheet()
    styles["BodyText"].fontSize = 9
    styles["BodyText"].leading = 12
    styles["Heading2"].fontSize = 14
    styles["Heading2"].leading = 17
    styles["Heading2"].spaceBefore = 8
    styles["Heading2"].spaceAfter = 6
    styles["Title"].fontSize = 20
    styles["Title"].leading = 24
    styles.add(ParagraphStyle(name="Cell", fontSize=8, leading=10, textColor=colors.HexColor("#17253b")))
    styles.add(ParagraphStyle(name="HeaderCell", parent=styles["Cell"], textColor=colors.white, fontName="Helvetica-Bold"))
    doc = SimpleDocTemplate(str(path), pagesize=landscape(letter),
                            leftMargin=36, rightMargin=36, topMargin=32, bottomMargin=34,
                            title=f"Draft change comparison: {name}")
    story = [Paragraph(escape(f"Draft change comparison: {name}"), styles["Title"])]
    for heading, content in sections:
        if heading == "PAGE_BREAK":
            story.append(PageBreak())
            continue
        if heading:
            story.append(Paragraph(escape(heading), styles["Heading2"]))
        if isinstance(content, Path):
            picture = Image(str(content))
            picture.drawHeight *= doc.width / picture.drawWidth
            picture.drawWidth = doc.width
            story.append(picture)
        elif isinstance(content, pd.DataFrame):
            if content.empty:
                story.append(Paragraph("None.", styles["BodyText"]))
                continue
            def cell(value, header=False):
                if isinstance(value, float):
                    value = f"{value:.3f}" if not value.is_integer() else str(int(value))
                return Paragraph(escape(str(value)).replace("\n", "<br/>"),
                                 styles["HeaderCell" if header else "Cell"])
            data = [[cell(c, True) for c in content.columns]] + [
                [cell(v) for v in row] for row in content.itertuples(index=False, name=None)
            ]
            widths = [doc.width / len(content.columns)] * len(content.columns)
            if len(content.columns) == 4 and content.columns[0] == "Measure":
                widths = [doc.width * .46] + [doc.width * .18] * 3
            table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#28435f")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eff4f8")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LINEBELOW", (0, 0), (-1, -1), .3, colors.HexColor("#dbe3eb")),
            ]))
            story.extend([table, Spacer(1, 4)])
        else:
            story.extend([Paragraph(escape(str(content)), styles["BodyText"]), Spacer(1, 6)])

    def footer(canvas, document):
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#516078"))
        canvas.drawString(36, 18, "Preview only - accepted baseline unchanged")
        canvas.drawRightString(756, 18, f"Page {document.page}")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def write_comparison(folder, name, sections):
    write_pdf(folder / "comparison.pdf", name, sections)
    page = f"<h1>Draft change comparison: {escape(name)}</h1>"
    for heading, content in sections:
        if heading == "PAGE_BREAK":
            continue
        if heading:
            page += f"<h2>{escape(heading)}</h2>"
        if isinstance(content, Path):
            page += f'<img src="{escape(content.name)}" alt="Detailed Balance comparison" style="width:100%;height:auto">'
        else:
            page += table_html(content) if isinstance(content, pd.DataFrame) else f"<p>{escape(str(content))}</p>"
    write_html(folder / "comparison.html", name, page)


def write_proposal(folder, name, baseline_path, before, payload, rows):
    folder.mkdir(parents=True, exist_ok=True)
    owned = ["comparison.pdf", "comparison.html", "moves.csv", "team_comparison.csv",
             "balance_comparison.csv", "friend_changes.csv", "friend_group_changes.csv",
             "proposal_roster.csv", "additional_moves.csv", "reoptimized_draft.json", "solver_results.json",
             "detailed_balance.png", "proposal_manifest.json"]
    for filename in owned:
        (folder / filename).unlink(missing_ok=True)
    try:
        assignments, moves, withdrawn = apply_requests(before, rows)
        exact = deepcopy(before)
        remove_players(exact, withdrawn)
    except ValueError as exc:
        write_comparison(folder, name, [("Request could not be evaluated", str(exc))])
        print(f"{name}: request error: {exc}")
        return False
    violations = constraint_violations(exact, assignments)
    placement_violations = constraint_violations(exact, assignments, check_sizes=False)
    if violations:
        exact.assignments = assignments
    else:
        exact.set_assignments(assignments)
    required = {pid: int(team) for pid, team in assignments.items()
                if team != before.assignments[pid]}
    optimized = None
    failure = ""
    if not placement_violations:
        candidate = deepcopy(exact)
        try:
            candidate.optimize_teams(required_teams=required,
                                     reference_assignments=before.assignments.loc[assignments.index])
            if any(int(candidate.assignments[pid]) != team for pid, team in required.items()):
                raise RuntimeError("Optimizer did not preserve requested placements")
            if constraint_violations(candidate, candidate.assignments):
                raise RuntimeError("Optimizer returned a draft violating original constraints")
            optimized = candidate
            optimized.export_draft_snapshot(folder / "reoptimized_draft.json")
            manifest = {"proposal": name, "baseline_hash": file_hash(baseline_path),
                        "snapshot_hash": file_hash(folder / "reoptimized_draft.json"),
                        "requested_locks": required,
                        "withdrawn_players": sorted(withdrawn),
                        "request_rows": rows[REQUEST_COLUMNS].to_dict("records")}
            (folder / "proposal_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            (folder / "solver_results.json").write_text(
                json.dumps(optimized.optimization_result, indent=2), encoding="utf-8")
        except (ValueError, RuntimeError) as exc:
            failure = f"Reoptimization unavailable: {exc}. Exact-swap results are still shown."
    exact_label = "Exact change" if withdrawn else "Exact swap"
    states = {"Baseline": before, exact_label: exact}
    if optimized is not None:
        states["Reoptimized"] = optimized
    summaries = {label: team_summary(draft) for label, draft in states.items()}
    unavailable = "Unavailable"
    status = "Exact change violates hard constraints" if violations else "Valid requested change"
    intro = (
        f"{status}. Frozen baseline captured {payload['captured_at'][:10]}. "
        f"The request withdraws {len(withdrawn)} players and moves {len(moves) - len(withdrawn)} players. "
        "Reoptimization excludes withdrawn players, fixes requested swap "
        "placements and reruns the existing balance, friendship and coverage priorities using frozen "
        "ratings and rules. Fewer changes from the baseline are a final tie-breaker. "
        "No proposal is accepted by this preview."
    )
    sections = [("", intro)]
    if placement_violations:
        sections.append(("", "Reoptimization was not run because the requested placements violate hard constraints."))
    if withdrawn:
        minimum, maximum = exact.team_size_bounds
        sections.append(("", f"Withdrawal: remaining roster: {len(exact.df_players)} players; "
                         f"team-size bounds: {minimum}-{maximum}. Requests involving withdrawn players "
                         "are shown as Withdrawn, not failed friendships. Surviving friend connections "
                         "are rebuilt without withdrawn players; counts use the smaller roster."))
    if violations and not placement_violations:
        sections.append(("", "The immediate change leaves uneven team sizes. Reoptimization can repair "
                         "these sizes under the smaller roster's bounds."))
    for message in violations + ([failure] if failure else []):
        sections.append(("Attention", message))
    if optimized is not None:
        result = optimized.optimization_result
        sections.append(("", f"Scenario balance-pass spread: {result['baseline_spread']:.3f}; "
                         f"scenario tolerance ceiling: {result['spread_limit']:.3f}. "
                         "The +0.10 tolerance is relative to this constrained scenario, so its ceiling "
                         "may differ from the baseline. Time-limited feasible results are previews, not proof of optimality."))
    balance_rows = []
    for metric in ["Team-to-team spread in Overall Level", "Players moved from baseline"]:
        row = {"Measure": metric}
        for label in ["Baseline", exact_label, "Reoptimized"]:
            if label not in states:
                row[label] = unavailable
            elif metric.startswith("Team-to-team"):
                means = summaries[label]["Average Overall Level"]
                row[label] = float(means.max() - means.min())
            else:
                row[label] = int(states[label].assignments.ne(
                    before.assignments.loc[states[label].assignments.index]).sum())
        balance_rows.append(row)
    balance_rows.extend([
        {"Measure": "Active players", **{label: len(states[label].assignments) if label in states else unavailable
                                        for label in ["Baseline", exact_label, "Reoptimized"]}},
        {"Measure": "Players withdrawn", **{label: len(before.assignments) - len(states[label].assignments)
                                           if label in states else unavailable
                                           for label in ["Baseline", exact_label, "Reoptimized"]}},
    ])
    sections.append(("Overall balance", pd.DataFrame(balance_rows)))
    averages = [{"Team / practice": f"{team} / {before.practice_days[team].title()}",
                 **{label: (float(summaries[label].at[team, "Average Overall Level"])
                            if label in states else unavailable)
                    for label in ["Baseline", exact_label, "Reoptimized"]}}
                for team in sorted(before.practice_days)]
    sections.append(("Team Average Overall Level", pd.DataFrame(averages)))
    settings = payload["settings"]
    sections.append(("", f"Player-mix counts read Baseline / {exact_label} / Reoptimized. Soft goals per team: "
                     f"{settings['handler_target']} top ball-handlers, {settings['tall_target']} tall players, "
                     f"{settings['second_year_target']} second years. Shooting is reported only."))
    coverage = []
    for team in sorted(before.practice_days):
        row = {"Team": team}
        for metric in ["Players", "Top Ball-Handlers", "Tall Players", "Second Years",
                       "Top Shooters", "No assessment data"]:
            row[metric] = " / ".join(str(int(summaries[label].at[team, metric]))
                                    if label in states else "-"
                                    for label in ["Baseline", exact_label, "Reoptimized"])
        coverage.append(row)
    sections.append(("Player mix", pd.DataFrame(coverage)))
    sections.append(("", "No assessment data means no matching evaluation row, excluding matched blank rows. "
                     "The CSV comparison additionally reports missing Overall Level scores estimated as 3."))
    sections.append(("PAGE_BREAK", ""))
    from teams_report import render_balance_comparison
    chart = render_balance_comparison({"Baseline": before, exact_label: exact,
                                       "Reoptimized": optimized})
    chart_path = folder / "detailed_balance.png"
    chart_path.write_bytes(chart.getvalue())
    sections.append(("Detailed Balance: baseline, exact change, reoptimized",
                     "The same rating scale and team colours apply across columns. "
                     "Each draft sorts assessed players by Overall Level, then places imputed players at the right. "
                     "Slots do not identify the same child across columns. Dotted 1-5 guides indicate missing ratings, not observed ranges."))
    sections.append(("", chart_path))
    sections.append(("PAGE_BREAK", ""))
    friend_stats = []
    keys = [("Friend requests honoured", "honoured_requests"),
            ("Mutual pairs together", "mutual_pairs_kept"),
            ("Players with a direct friend", "direct_companions"),
            ("Players with a friend-group member", "group_companions"),
            ("Players without a friend-group member", "isolated_players")]
    for measure, key in keys:
        row = {"Measure": measure}
        for label in ["Baseline", exact_label, "Reoptimized"]:
            value = states[label].friendship_statistics()[key] if label in states else unavailable
            row[label] = len(value) if isinstance(value, list) else value
        friend_stats.append(row)
    friend_stats.append({"Measure": "Split friend groups", **{
        label: int(states[label].get_friend_group_report()["Status"].str.contains("Split").sum())
        if label in states else unavailable
        for label in ["Baseline", exact_label, "Reoptimized"]
    }})
    sections.append(("Friendship outcomes", pd.DataFrame(friend_stats)))
    labels = before.player_labels()
    changes = []
    for left, right in sorted(before.friend_requests):
        kept = {label: (bool(draft.assignments[left] == draft.assignments[right])
                        if left in draft.assignments and right in draft.assignments else None)
                for label, draft in states.items()}
        if len(set(kept.values())) < 2:
            continue
        if left in withdrawn or right in withdrawn:
            outcome = "Request involves withdrawn player"
        elif optimized is None:
            outcome = "Lost in exact change" if kept["Baseline"] else "Gained in exact change"
        elif kept["Baseline"] and not kept[exact_label] and kept["Reoptimized"]:
            outcome = "Restored after exact change"
        elif kept["Baseline"] and not kept["Reoptimized"]:
            outcome = "Still lost" if not kept[exact_label] else "Newly broken by reoptimization"
        elif not kept["Baseline"] and kept["Reoptimized"]:
            outcome = "Newly honoured"
        else:
            outcome = "Temporary gain lost"
        changes.append({"Player": labels[left], "Requested Friend": labels[right],
                        **{label: ("Withdrawn" if kept[label] is None else "Honoured" if kept[label] else "Split")
                           if label in kept else unavailable
                           for label in ["Baseline", exact_label, "Reoptimized"]},
                        "Outcome": outcome})
    friends = pd.DataFrame(changes, columns=["Player", "Requested Friend", "Baseline", exact_label, "Reoptimized", "Outcome"])
    if not friends.empty:
        counts = friends["Outcome"].value_counts()
        sections.append(("", "; ".join(f"{kind}: {count}" for kind, count in counts.items())
                         + ". Counts are directed requests; a mutual pair contributes two requests."))
    sections.append(("Changed friend requests", friends))
    group_reports = {label: draft.get_friend_group_report() for label, draft in states.items()}
    group_changes = []
    for group in before.friend_groups:
        if len(group) < 2:
            continue
        placements = {}
        for label, draft in states.items():
            parts = []
            for team in sorted(before.practice_days):
                members = [labels[pid] for pid in group
                           if pid in draft.assignments and draft.assignments[pid] == team]
                if members:
                    parts.append(f"Team {team}: " + ", ".join(members))
            absent = [labels[pid] for pid in group if pid not in draft.assignments]
            if absent:
                parts.append("Withdrawn: " + ", ".join(absent))
            placements[label] = "; ".join(parts)
        if len(set(placements.values())) > 1:
            group_changes.append({label: placements.get(label, unavailable)
                                  for label in ["Baseline", exact_label, "Reoptimized"]})
    groups = pd.DataFrame(group_changes, columns=["Baseline", exact_label, "Reoptimized"])
    sections.append(("Friend-group placement changes (grouped by team)", groups))
    isolation_row = {}
    for label, draft in states.items():
        pids = draft.friendship_statistics()["isolated_players"]
        isolation_row[label] = "; ".join(labels[pid] for pid in pids) or "None"
    sections.append(("Players without a friend-group member", pd.DataFrame([isolation_row])))
    sections.append(("PAGE_BREAK", ""))
    sections.append(("Requested changes" + (" (preserved during reoptimization)" if optimized is not None else ""), moves))
    additional = []
    if optimized is not None:
        for pid, team in optimized.assignments.items():
            if pid not in required and team != before.assignments[pid]:
                additional.append({"Player": labels[pid], "Baseline Team": int(before.assignments[pid]),
                                   "Reoptimized Team": int(team)})
    additional = pd.DataFrame(additional, columns=["Player", "Baseline Team", "Reoptimized Team"])
    if optimized is not None:
        sections.append(("Additional moves from the baseline", additional))
    roster_rows = [{"Player": labels[pid], **{
        label: int(draft.assignments[pid]) if pid in draft.assignments else "Withdrawn"
        for label, draft in states.items()
    }} for pid in sorted(labels, key=lambda pid: (int(before.assignments[pid]), labels[pid]))]
    roster = pd.DataFrame(roster_rows)
    sections.append(("Team assignments", roster))
    if optimized is not None:
        statuses = {key: value for key, value in optimized.optimization_result.items()
                    if key.endswith("status")}
        sections.append(("Solver status", "; ".join(f"{key.replace('_', ' ')}: {value}" for key, value in statuses.items())
                         + "; secondary passes: " + "; ".join(
                             f"{name}: {details['status']}" for name, details in optimized.optimization_result['secondary_targets'].items())))
    team_comparison = []
    for team in summaries["Baseline"].index:
        for metric in summaries["Baseline"].columns:
            row = {"Team": team, "Measure": metric}
            for label in ["Baseline", exact_label, "Reoptimized"]:
                row[label] = summaries[label].at[team, metric] if label in summaries else unavailable
            row[f"{exact_label} change"] = row[exact_label] - row["Baseline"]
            row["Reoptimized change"] = (row["Reoptimized"] - row["Baseline"]
                                         if optimized is not None else unavailable)
            team_comparison.append(row)
    for filename, frame in [("moves.csv", moves), ("balance_comparison.csv", pd.DataFrame(balance_rows)),
                            ("team_comparison.csv", pd.DataFrame(team_comparison)), ("friend_changes.csv", friends),
                            ("friend_group_changes.csv", groups), ("additional_moves.csv", additional),
                            ("proposal_roster.csv", roster.assign(**{"Valid Exact Change": not violations,
                                                             "Valid Reoptimized Proposal": optimized is not None}))]:
        frame.to_csv(folder / filename, index=False, encoding="utf-8-sig")
    write_comparison(folder, name, sections)
    print(f"{name}: {status}. Report: {folder / 'comparison.pdf'}")
    return optimized is not None and not failure


def write_html(path, title, body):
    style = """
    body{font:15px/1.5 system-ui,sans-serif;color:#17253b;max-width:1100px;margin:36px auto;padding:0 20px}
    h1{font-size:28px}h2{font-size:19px;margin-top:28px}.status{font-weight:700}
    table{border-collapse:collapse;width:100%;font-size:13px;margin:12px 0}
    th{text-align:left;background:#28435f;color:white}td,th{padding:8px 10px;border-bottom:1px solid #dbe3eb}
    tr:nth-child(even){background:#f1f6fa}.issues{background:#fff3df;padding:16px 32px}.footer{color:#516078;font-size:12px}
    @media print{body{margin:0;max-width:none}thead{display:table-header-group}tr{break-inside:avoid}}
    """
    Path(path).write_text(f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{escape(title)}</title><style>{style}</style></head><body>{body}</body></html>", encoding="utf-8")


def read_requests(path):
    requests = pd.read_csv(path, dtype="string", keep_default_na=False, encoding="utf-8-sig")
    requests.columns = requests.columns.str.strip()
    for column in ["Proposal", "Player A", "Player B"]:
        if column not in requests:
            raise ValueError(f"Request CSV needs column {column!r}")
    for column in REQUEST_COLUMNS:
        if column not in requests:
            requests[column] = ""
        requests[column] = requests[column].str.strip()
    requests = requests.loc[requests[REQUEST_COLUMNS].ne("").any(axis=1)]
    if requests[["Proposal", "Player A"]].eq("").any().any():
        raise ValueError("Every request needs a proposal name and Player A")
    if (requests["Player B"].eq("") & requests["Player B DOB"].ne("")).any():
        raise ValueError("A withdrawal must leave both Player B and Player B DOB blank")
    return requests


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def accept_proposal(folder, baseline_path, requests_path, new_baseline, remaining_path):
    """Accept the reviewed reoptimized result; do not solve or overwrite inputs."""
    folder = Path(folder)
    manifest = json.loads((folder / "proposal_manifest.json").read_text(encoding="utf-8"))
    if manifest["baseline_hash"] != file_hash(baseline_path):
        raise ValueError("Preview is outdated for this baseline. Regenerate it before acceptance.")
    snapshot = folder / "reoptimized_draft.json"
    if manifest["snapshot_hash"] != file_hash(snapshot):
        raise ValueError("Reviewed snapshot changed. Regenerate the preview before acceptance.")
    before, _ = load_baseline(baseline_path)
    requests = read_requests(requests_path)
    selected = requests.loc[requests["Proposal"].eq(manifest["proposal"])]
    canonical = lambda records: sorted(json.dumps(row, sort_keys=True) for row in records)
    if selected.empty or canonical(selected[REQUEST_COLUMNS].to_dict("records")) != canonical(manifest["request_rows"]):
        raise ValueError("Accepted request rows have changed or are missing. Regenerate the preview.")
    assignments, _, withdrawn = apply_requests(before, selected)
    locks = {pid: int(team) for pid, team in assignments.items() if team != before.assignments[pid]}
    if locks != manifest["requested_locks"]:
        raise ValueError("Preview locks do not match the current requests")
    if sorted(withdrawn) != manifest.get("withdrawn_players", []):
        raise ValueError("Preview withdrawals do not match the current requests")
    accepted, snapshot_payload = load_baseline(snapshot)
    if set(accepted.assignments.index) != set(assignments.index):
        raise ValueError("Reviewed draft does not contain the expected remaining roster")
    expected_locks = {pid: team for pid, team in before.accepted_locks.items() if pid not in withdrawn}
    if accepted.accepted_locks != expected_locks:
        raise ValueError("Preview does not retain the current accepted locks")
    if any(int(accepted.assignments[pid]) != team for pid, team in locks.items()):
        raise ValueError("Reviewed draft does not satisfy requested placements")
    accepted.accepted_locks.update(locks)
    accepted.set_assignments(accepted.assignments.copy())
    accepted.optimization_result = {"spread_limit": snapshot_payload["settings"].get("spread_limit")}
    accepted.change_history.append({"proposal": manifest["proposal"],
                                    "previous_baseline_hash": manifest["baseline_hash"],
                                    "requested_locks": locks,
                                    "withdrawn_players": sorted(withdrawn)})
    new_baseline, remaining_path = Path(new_baseline), Path(remaining_path)
    if new_baseline.resolve() == remaining_path.resolve():
        raise ValueError("New baseline and remaining requests need different paths")
    for path in [new_baseline, remaining_path]:
        if path.exists():
            raise ValueError(f"{path} already exists; choose a new version filename")
        path.parent.mkdir(parents=True, exist_ok=True)
    remaining = requests.loc[~requests["Proposal"].eq(manifest["proposal"])]
    accepted.export_draft_snapshot(new_baseline)
    try:
        remaining.to_csv(remaining_path, index=False, encoding="utf-8-sig", mode="x")
    except Exception:
        new_baseline.unlink()
        raise
    print(f"Accepted {manifest['proposal']}: {new_baseline}")
    print(f"Remaining requests: {remaining_path}. Regenerate previews against this new baseline.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=Path("output/draft_baseline.json"))
    parser.add_argument("--requests", type=Path, default=Path("data/change_requests.csv"))
    parser.add_argument("--output", type=Path, default=Path("output/change_requests"))
    parser.add_argument("--capture-baseline", action="store_true")
    parser.add_argument("--from-run", type=Path, help="Snapshot of the accepted run, used only when capturing")
    parser.add_argument("--reports-folder", type=Path, default=Path("output"))
    parser.add_argument("--solver-seconds", type=float, default=ad.SOLVER_SECONDS_PER_PASS,
                        help="Positive time limit for each optimization pass (default: %(default)s)")
    parser.add_argument("--accept", type=Path, help="Proposal folder whose reviewed reoptimized result is accepted")
    parser.add_argument("--new-baseline", type=Path, help="New accepted baseline filename; never overwritten")
    parser.add_argument("--remaining-requests", type=Path, help="New CSV with accepted proposal rows removed")
    args = parser.parse_args()
    if args.solver_seconds <= 0:
        parser.error("--solver-seconds must be positive")
    ad.SOLVER_SECONDS_PER_PASS = args.solver_seconds
    try:
        if args.accept:
            if args.capture_baseline or args.from_run or not args.new_baseline or not args.remaining_requests:
                raise ValueError("--accept requires --new-baseline and --remaining-requests; do not combine it with capture")
            accept_proposal(args.accept, args.baseline, args.requests, args.new_baseline, args.remaining_requests)
            return 0
        if args.new_baseline or args.remaining_requests:
            raise ValueError("--new-baseline and --remaining-requests are used with --accept")
        if args.capture_baseline:
            capture_baseline(args.baseline, args.from_run, args.reports_folder)
            return 0
        if args.from_run:
            raise ValueError("--from-run is only used with --capture-baseline")
        drafter, payload = load_baseline(args.baseline)
        requests = read_requests(args.requests)
        if requests.empty:
            print("No change requests found. Add a row to the request CSV.")
            return 0
        before_hash = hashlib.sha256(args.baseline.read_bytes()).hexdigest()
        all_valid = True
        for index, (name, rows) in enumerate(requests.groupby("Proposal", sort=False), 1):
            slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(name)).strip("_")[:80] or "proposal"
            folder = args.output / f"{index:02d}_{slug}"
            valid = write_proposal(folder, str(name), args.baseline, drafter, payload, rows)
            all_valid = all_valid and valid
        if hashlib.sha256(args.baseline.read_bytes()).hexdigest() != before_hash:
            raise RuntimeError("Baseline changed during preview; rerun from its saved copy")
        return 0 if all_valid else 1
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
