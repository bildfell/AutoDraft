"""Two-page team draft report with an evidence appendix.

Requires matplotlib and reportlab. All figures come from an AutoDrafter run.
"""

from datetime import datetime
from io import BytesIO
from pathlib import Path
import re
from xml.sax.saxutils import escape

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Paragraph

from auto_drafter import (
    FIRST_NAME, LAST_NAME, OVERALL, SORT_OUT_NUMBER,
    FRIEND_BALANCE_TOLERANCE, TOP_BALL_HANDLER_COUNT,
)


PAGE_W, PAGE_H = landscape(letter)
INK = colors.HexColor("#17253b")
MUTED = colors.HexColor("#516078")
LINE = colors.HexColor("#dbe3eb")
BLUE = colors.HexColor("#17639c")
FONT = "Helvetica"
BOLD = "Helvetica-Bold"
for font_path in (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/Library/Fonts/Arial Unicode.ttf"),
):
    if font_path.exists():
        pdfmetrics.registerFont(TTFont("PrimerSans", str(font_path)))
        FONT = "PrimerSans"
        break


def _text(c, text, x, y, size=9, color=INK, font=None):
    c.setFillColor(color)
    c.setFont(font or FONT, size)
    c.drawString(x, y, str(text))


def _fit(c, text, x, y, width, size=9):
    text = str(text)
    while pdfmetrics.stringWidth(text, FONT, size) > width and size > 6.1:
        size -= 0.3
    _text(c, text, x, y, size)


def _para(c, text, x, top, width, size=8.5, leading=11, color=INK,
          italic_phrase=None):
    style = ParagraphStyle(
        "body", fontName=FONT, fontSize=size, leading=leading,
        textColor=color, spaceAfter=0,
    )
    content = escape(str(text))
    if italic_phrase:
        phrase = escape(italic_phrase)
        content = content.replace(
            phrase, f'<font name="Helvetica-Oblique">{phrase}</font>'
        )
    p = Paragraph(content, style)
    _, height = p.wrap(width, PAGE_H)
    p.drawOn(c, x, top - height)
    return top - height


def _frame(c, title, page, illustrative, subtitle=""):
    c.setFillColor(INK)
    c.rect(0, PAGE_H - 13, PAGE_W, 13, fill=1, stroke=0)
    _text(c, title, 34, PAGE_H - 43, 17, INK, BOLD)
    if subtitle:
        _fit(c, subtitle, 34, PAGE_H - 60, 450, 9)
        _text(c, datetime.now().strftime("%Y-%m-%d"),
              684, PAGE_H - 43, 8, MUTED)
    else:
        _text(c, "TEAM DRAFT  |  " + datetime.now().strftime("%Y-%m-%d"),
              34, PAGE_H - 60, 8, MUTED)
    if illustrative:
        _text(c, "ILLUSTRATIVE DATA - NOT REAL TEAMS", 498,
              PAGE_H - 60, 8, colors.HexColor("#b03b38"), BOLD)
    c.setStrokeColor(LINE)
    c.line(34, 30, PAGE_W - 34, 30)
    _text(c, f"Team Draft Report  |  {page}", 34, 17, 7, MUTED)


def _rosters(drafter):
    labels = drafter.player_labels()
    result = {}
    for team, frame in drafter.teams.items():
        rows = (
            frame.assign(_report_imputed=frame[f"{OVERALL} Imputed"].fillna(False))
            .sort_values(
                ["_report_imputed", OVERALL, LAST_NAME, FIRST_NAME],
                ascending=[True, False, True, True], kind="stable",
            )
        )
        result[team] = [
            (slot, row, labels[row[SORT_OUT_NUMBER]])
            for slot, (_, row) in enumerate(rows.iterrows(), 1)
        ]
    return result


def _overview(c, drafter, rosters, illustrative, subtitle):
    _frame(c, "Team Draft Report", "1 / 2 + appendix", illustrative, subtitle)
    result = drafter.optimization_result or {}
    spread = result.get("actual_spread")
    _text(c, f"{len(drafter.df_players)} players  |  "
          f"Team-to-team spread in Overall Level: {spread:.3f}" if spread is not None
          else f"{len(drafter.df_players)} players", 34, 529, 10, BLUE, BOLD)
    methodology = [
        "The goal of the draft is to arrive at a set "
        "of well-balanced teams that all coaches are happy with. To achieve "
        "this, we approach team selection as a constrained optimization "
        "problem, which we solve with Google's open-source OR-Tools.",
        "We enforce fixed/family coach assignments, practice day blackouts, "
        "and team sizes (at most one player difference between teams). "
        "A first pass minimizes the difference between team-averaged "
        "Overall Level ratings.",
        "We then run successive passes, allowing at most "
        f"{FRIEND_BALANCE_TOLERANCE:.2f} additional rating points of "
        "team-to-team spread in Overall Level, to optimize friend requests, "
        "ball-handler balance, height, age, and the distribution of players "
        "with no matching evaluation record.",
        "Missing ratings in any assessment category, including Overall "
        "Level, are assigned a value of 3.",
    ]
    _text(c, "Team Draft Methodology:", 34, 514, 8.5, INK, BOLD)
    methodology_bottom = _para(
        c, " ".join(methodology), 34, 501, 716, 8, 10,
        italic_phrase="constrained optimization problem",
    )

    roster_shift = max(0, 469 - (methodology_bottom - 10))
    x_positions = (34, 219, 404, 589)
    for team, x in zip(sorted(rosters), x_positions):
        players = rosters[team]
        c.setFillColor(colors.HexColor("#eaf2f8"))
        c.roundRect(x, 245 - roster_shift, 169, 224, 6, fill=1, stroke=0)
        c.setFillColor(INK)
        c.setFont(BOLD, 11)
        c.drawString(x + 9, 450 - roster_shift, f"Team {team}  |  {drafter.practice_days[team]}")
        average = sum(float(r[OVERALL]) for _, r, _ in players) / len(players)
        _text(c, f"{len(players)} players", x + 9, 435 - roster_shift, 8, MUTED)
        _fit(c, f"Team Average Overall Level: {average:.2f}",
             x + 9, 422 - roster_shift, 151, 7.5)
        for slot, _, label in players:
            _fit(c, f"{slot:>2}. {label}", x + 9, 402 - roster_shift - 15.0 * (slot - 1),
                 151, 8.7)

    report = drafter.get_friend_group_report()
    split = report.loc[report["Status"].str.contains("Split", na=False)]
    stats = drafter.friendship_statistics()
    _text(c, "Friendship placements", 34, 220 - roster_shift, 11, INK, BOLD)
    _text(c,
          f"{len(report) - len(split)} groups together; {len(split)} split  |  "
          f"mutual pairs together {stats['mutual_pairs_kept']}/"
          f"{stats['mutual_pairs_total']}  |  direct companions "
          f"{stats['direct_companions']}/{stats['connected_players']}",
          34, 205 - roster_shift, 8, MUTED)
    y = 191 - roster_shift
    if split.empty:
        _text(c, "All requested friend groups remain together.", 34, y, 8.5)
    for _, row in split.iterrows():
        if y < 100:
            _text(c, "Additional split placements: appendix.", 34, 56, 8, MUTED)
            break
        y = _para(c, f"Split: {row['Assignments']}", 34, y, 716, 8, 10.5) - 2
        if row["Reason"] != "Selected draft split; not proven unavoidable":
            y = _para(c, f"Reason: {row['Reason']}", 43, y, 707,
                      7.7, 10, MUTED) - 2
        if y < 75:
            _text(c, "Additional group placements and individual requests: appendix.",
                  34, 56, 8, MUTED)
            break
    if not split.empty and y >= 75:
        _text(c, "Full group placements and individual requests: appendix.",
              34, max(47, y - 8), 8, MUTED)
    c.showPage()


def _draw_balance_panel(ax, team, players, drafter, color, annotate_mean=True):
    if not players:
        ax.text(.5, .5, "No players", ha="center", va="center",
                transform=ax.transAxes, fontsize=8, color="#516078")
        return
    for slot, row, _ in players:
        n = int(row[f"{OVERALL} Assessment Count"])
        value = float(row[OVERALL])
        if n >= 2:
            ax.errorbar(slot, value,
                        yerr=[[value - float(row[f"{OVERALL} Min"])],
                              [float(row[f"{OVERALL} Max"]) - value]],
                        fmt="o", color=color, markersize=4,
                        capsize=2.5, linewidth=1)
        elif n == 1:
            ax.plot(slot, value, "^", color=color, markersize=5)
        else:
            ax.vlines(slot, 1, 5, colors=color, linestyles=":",
                      linewidth=1.2, alpha=.7, zorder=1)
            ax.plot(slot, value, "D", markerfacecolor="white",
                    markeredgecolor=color, markersize=5, zorder=3)
    mean = sum(float(row[OVERALL]) for _, row, _ in players) / len(players)
    ax.axhline(mean, linestyle="--", linewidth=1, color=color)
    if annotate_mean:
        ax.text(.98, .93, f"Avg. Overall Level {mean:.2f}", transform=ax.transAxes,
                ha="right", va="top", fontsize=8, color=color)
    ax.set_title(f"Team {team}  |  {drafter.practice_days[team]}",
                 loc="left", fontsize=9, fontweight="bold")


def _chart(rosters, drafter):
    fig, axes = plt.subplots(2, 2, figsize=(10.6, 4.4), sharex=True, sharey=True)
    fig.subplots_adjust(left=.07, right=.99, bottom=.15, top=.94,
                        hspace=.42, wspace=.17)
    palette = ("#17639c", "#906629", "#667c3c", "#9a508a")
    for ax, (team, players), color in zip(axes.flat, rosters.items(), palette):
        _draw_balance_panel(ax, team, players, drafter, color)
        ax.set_xlim(.5, max(11, len(players)) + .5)
        ax.set_ylim(.8, 5.2)
        ax.set_xticks(range(1, max(11, len(players)) + 1))
        ax.grid(axis="y", alpha=.18)
        ax.tick_params(labelsize=7)
    for ax in axes[1]:
        ax.set_xlabel("Roster slot (page 1)", fontsize=8)
    for ax in axes[:, 0]:
        ax.set_ylabel("Overall Level rating", fontsize=8)
    fig.legend(handles=[
        Line2D([], [], marker="o", color="#333", linestyle="None",
               label="2+ assessments: observed min-max bar"),
        Line2D([], [], marker="^", color="#333", linestyle="None",
               label="1 assessment"),
        Line2D([], [], marker="D", markerfacecolor="white", color="#333",
               linestyle=":", label="0 assessments: imputed 3; 1-5 scale guide"),
    ], loc="lower center", ncol=3, fontsize=7, frameon=False,
       bbox_to_anchor=(.5, -.015))
    data = BytesIO()
    fig.savefig(data, format="png", dpi=190, facecolor="white")
    plt.close(fig)
    data.seek(0)
    return data


def render_balance_comparison(drafts):
    """Return a chart PNG: teams in rows, draft alternatives in columns."""
    available = [draft for draft in drafts.values() if draft is not None]
    teams = sorted(available[0].practice_days)
    rosters = {label: _rosters(draft) for label, draft in drafts.items()
               if draft is not None}
    slots = max(11, max(len(players) for roster in rosters.values()
                        for players in roster.values()))
    fig, axes = plt.subplots(len(teams), len(drafts), figsize=(10.6, 6.8),
                             sharex=True, sharey=True, squeeze=False)
    fig.subplots_adjust(left=.065, right=.99, bottom=.12, top=.94,
                        hspace=.52, wspace=.18)
    palette = ("#17639c", "#906629", "#667c3c", "#9a508a")
    for row_index, team in enumerate(teams):
        color = palette[row_index % len(palette)]
        for column_index, (label, draft) in enumerate(drafts.items()):
            ax = axes[row_index, column_index]
            if draft is None:
                ax.text(.5, .5, "Reoptimization unavailable", ha="center",
                        va="center", transform=ax.transAxes, fontsize=8,
                        color="#516078")
                ax.set_title(f"Team {team}", loc="left", fontsize=9,
                             fontweight="bold")
            else:
                players = rosters[label][team]
                _draw_balance_panel(ax, team, players, draft, color, annotate_mean=False)
                average = (f"Avg. {sum(float(player[OVERALL]) for _, player, _ in players) / len(players):.2f}"
                           if players else "No players")
                ax.set_title(f"Team {team} | {draft.practice_days[team]} | {average}",
                             loc="left", fontsize=8.5, fontweight="bold")
            ax.set_xlim(.5, slots + .5)
            ax.set_ylim(.8, 5.2)
            ax.set_xticks(range(1, slots + 1))
            ax.set_yticks(range(1, 6))
            ax.grid(axis="y", alpha=.18)
            ax.tick_params(labelsize=7)
            if column_index == 0:
                ax.set_ylabel("Overall Level", fontsize=8)
            if row_index == len(teams) - 1:
                ax.set_xlabel("Sorted roster slot", fontsize=8)
    for column_index, label in enumerate(drafts):
        position = axes[0, column_index].get_position()
        fig.text((position.x0 + position.x1) / 2, .985, label,
                 ha="center", va="top", fontsize=11, fontweight="bold",
                 color="#17253b")
    fig.legend(handles=[
        Line2D([], [], marker="o", color="#333", linestyle="None",
               label="2+ assessments: observed min-max bar"),
        Line2D([], [], marker="^", color="#333", linestyle="None",
               label="1 assessment"),
        Line2D([], [], marker="D", markerfacecolor="white", color="#333",
               linestyle=":", label="0 assessments: imputed 3; 1-5 scale guide"),
        Line2D([], [], color="#333", linestyle="--", label="Team Average Overall Level"),
    ], loc="lower center", ncol=2, fontsize=7, frameon=False,
       bbox_to_anchor=(.5, .005))
    data = BytesIO()
    fig.savefig(data, format="png", dpi=190, facecolor="white")
    plt.close(fig)
    data.seek(0)
    return data


def _coverage(c, drafter):
    frame = drafter.get_player_evidence_report()
    assessed = drafter.df_players.set_index(SORT_OUT_NUMBER)
    shooting = assessed.loc[
        assessed["Shooting Assessment Count"].gt(0), "Shooting Observed Mean"
    ].dropna().sort_values(ascending=False)
    cutoff = shooting.iloc[min(7, len(shooting) - 1)] if len(shooting) else None
    shooter_ids = set(shooting.loc[shooting.ge(cutoff)].index) if cutoff is not None else set()
    _text(c, "Player mix by team",
          34, 176, 10, INK, BOLD)
    measured_height = frame.loc[frame["Height Band"].notna()]
    top_height = (
        measured_height[["Height Min Cm", "Height Max Cm"]]
        .drop_duplicates()
        .sort_values(["Height Min Cm", "Height Max Cm"])
    )
    tallest_band = tuple(top_height.iloc[-1]) if not top_height.empty else None
    heads = ["Team", "Top Shooters", "Top Ball-Handlers", "Tall Players*",
             "Second Years", "No assessment data", "Overall Level estimated"]
    xs = [38, 112, 217, 328, 440, 526, 637]
    for x, head in zip(xs, heads):
        _text(c, head, x, 153, 7.4, MUTED)
    c.setStrokeColor(LINE)
    c.line(34, 147, 755, 147)
    second_year_target = (drafter.optimization_result or {}).get(
        "secondary_targets", {}
    ).get("second_year", {}).get("target", 2)
    for team in sorted(drafter.teams):
        subset = frame.loc[frame["Team"].eq(team)]
        players = drafter.teams[team]
        tall_evidence = subset["Tall Evidence Pool"].fillna(False)
        vals = [f"Team {team}",
                str(sum(pid in shooter_ids for pid in players[SORT_OUT_NUMBER])),
                f"{int(subset['Top Ball-Handling Pool'].fillna(False).sum())} / 2",
                f"{int(tall_evidence.sum())} / {drafter.tall_target_per_team}",
                f"{int(subset['Age Cohort'].eq('Year 2').sum())} / {second_year_target}",
                str(int(subset["No Evaluation Match"].fillna(False).sum())),
                str(int(players[f"{OVERALL} Imputed"].fillna(False).sum()))]
        for x, val in zip(xs, vals):
            _text(c, val, x, 137 - (team - 1) * 17, 8)
    height_label = (
        measured_height.loc[
            measured_height["Height Min Cm"].eq(tallest_band[0])
            & measured_height["Height Max Cm"].eq(tallest_band[1]),
            "Height Band",
        ].iloc[0] if tallest_band is not None else "none recorded"
    )
    tall_label = "tall player" if drafter.tall_target_per_team == 1 else "tall players"
    _text(c, "Counts / goal: aim for 2 top ball-handlers, "
          f"{drafter.tall_target_per_team} {tall_label} and "
          f"{second_year_target} second-years per team. These are preferences, not guarantees.",
          34, 70, 7.0, MUTED)
    _text(c, f"* Tall Players combines the highest recorded height band "
          f"({height_label}) and positive coach 'Tall' notes; each player counted once.",
          34, 60, 7.0, MUTED)
    _text(c, f"Top pools: 8 shooters; {TOP_BALL_HANDLER_COUNT} ball-handlers, "
          "based on observed scores; cutoff ties included. Shooting is for discussion only.",
          34, 50, 7.0, MUTED)
    _text(c, "No assessment data = no matching evaluation record. "
          "Overall Level estimated = no Overall Level score; replaced by 3, including blank records.",
          34, 40, 7.0, MUTED)


def _balance(c, drafter, rosters, illustrative, subtitle):
    _frame(c, "Detailed Balance", "2 / 2 + appendix", illustrative, subtitle)
    result = drafter.optimization_result or {}
    spread = result.get("actual_spread")
    statuses = [
        stage["status"] for stage in result.get("secondary_targets", {}).values()
        if not stage["status"].startswith("Skipped")
    ]
    status = (statuses[-1] if statuses else None) or result.get("coverage_status") or result.get("friendship_status") or result.get("balance_status") or "Draft"
    if status.startswith("Skipped"):
        status = result.get("friendship_status") or result.get("balance_status") or "Draft"
    _text(c, f"Team-to-team spread in Overall Level: {spread:.3f}  |  Solver: {status}"
          if spread is not None else f"Solver: {status}", 34, 529, 10, BLUE, BOLD)
    _text(c, "Assessed players rank by Overall Level; unrated players appear at right. Slots match page 1.",
          34, 514, 8, MUTED)
    image = _chart(rosters, drafter)
    c.drawImage(ImageReader(image), 31, 191, width=729, height=310)
    _coverage(c, drafter)
    c.showPage()


def _appendix(c, drafter, illustrative, subtitle):
    appendix = 1
    page = 1
    titles = {
        1: "Appendix 1 | Ratings and assessment evidence",
        2: "Appendix 2 | Friend requests",
    }
    _frame(c, titles[appendix], f"A{appendix}.{page}", illustrative, subtitle)
    y = 524

    cell_style = ParagraphStyle(
        "appendix-cell", fontName=FONT, fontSize=7.3, leading=9.5,
        textColor=INK,
    )

    def new_page():
        nonlocal page, y
        c.showPage()
        page += 1
        _frame(c, titles[appendix], f"A{appendix}.{page}", illustrative, subtitle)
        y = 524

    def next_appendix():
        nonlocal appendix, page, y
        c.showPage()
        appendix = 2
        page = 1
        _frame(c, titles[appendix], f"A{appendix}.{page}", illustrative, subtitle)
        y = 524

    def prepared_row(values, widths):
        cells = [Paragraph(escape(str(value)), cell_style) for value in values]
        heights = [cell.wrap(width - 12, PAGE_H)[1]
                   for cell, width in zip(cells, widths)]
        return cells, max(21, max(heights) + 10)

    def table(title, headings, rows, widths, empty_text, group_labels=None):
        nonlocal y
        if not rows:
            rows = [(empty_text,) + ("",) * (len(headings) - 1)]
        prepared = [prepared_row(row, widths) for row in rows]
        group_counts = {
            label: group_labels.count(label) for label in set(group_labels or [])
        }
        group_tints = {
            "Overall Level": "#dceaf6",
            "Shooting": "#f8e8d7",
            "Ball-Handling": "#e4f1e2",
            "Rebounding": "#eee5f5",
        }

        def header(continuation=False):
            nonlocal y
            _text(c, title + (" (continued)" if continuation else ""),
                  38, y, 10, INK, BOLD)
            y -= 17
            c.setFillColor(colors.HexColor("#28435f"))
            c.rect(38, y - 23, sum(widths), 23, fill=1, stroke=0)
            x = 38
            for label, width in zip(headings, widths):
                _text(c, label, x + 6, y - 15, 7.2, colors.white, BOLD)
                x += width
            y -= 23

        if y - (17 + 23 + prepared[0][1] + (19 if group_labels else 0)) < 47:
            new_page()
        header()
        previous_group = None
        for index, (cells, height) in enumerate(prepared):
            group = group_labels[index] if group_labels else None
            needs_band = group is not None and group != previous_group
            band_height = 19 if needs_band else 0
            if y - height - band_height < 45:
                new_page()
                header(continuation=True)
                needs_band = group is not None
            if needs_band:
                c.setFillColor(colors.HexColor(group_tints.get(group, "#e8eef5")))
                c.rect(38, y - 19, sum(widths), 19, fill=1, stroke=0)
                _text(c, f"{group}  |  {group_counts[group]} flagged entries",
                      44, y - 13, 7.8, INK, BOLD)
                y -= 19
            c.setFillColor(colors.HexColor(
                "#f1f6fa" if index % 2 == 0 else "#ffffff"
            ))
            c.rect(38, y - height, sum(widths), height, fill=1, stroke=0)
            c.setStrokeColor(LINE)
            c.line(38, y - height, 38 + sum(widths), y - height)
            x = 38
            for cell, width in zip(cells, widths):
                cell.drawOn(c, x + 6, y - 5 - cell.height)
                x += width
            y -= height
            previous_group = group
        y -= 18

    evidence = drafter.get_assessment_report()
    notable = evidence.loc[
        evidence["Assessment Count"].ge(2) & evidence["Range"].ge(2)
    ].copy()
    metric_order = {
        name: position for position, name in enumerate(
            ("Overall Level", "Shooting", "Ball-Handling", "Rebounding")
        )
    }
    notable["_metric_order"] = notable["Metric"].map(metric_order).fillna(99)
    notable = notable.sort_values(
        ["_metric_order", "Range", "Player"],
        ascending=[True, False, True], kind="stable",
    )
    table("Observed differences for review (range >= 2, at least 2 assessments)",
          ("Player", "Team", "Metric", "Mean", "n", "Min-max", "Range", "Coach scores by source"),
          [(r["Player"], r["Team"], r["Metric"],
            f"{float(r['Observed Mean']):.2f}", int(r["Assessment Count"]),
            f"{float(r['Min']):g}-{float(r['Max']):g}",
            f"{float(r['Range']):g}",
            re.sub(r"\.csv(?=:)", "", r["Evaluations"], flags=re.IGNORECASE))
          for _, r in notable.iterrows()],
          (116, 37, 89, 43, 24, 55, 42, 308),
          "No metric meets this review threshold",
          group_labels=notable["Metric"].tolist())

    assessment_rows = []
    for metric in ("Overall Level", "Shooting", "Ball-Handling", "Rebounding"):
        counts = drafter.df_players[f"{metric} Assessment Count"]
        assessment_rows.append((metric, int(counts.eq(0).sum()),
                                int(counts.eq(1).sum()), int(counts.ge(2).sum())))
    # Keep the two short coverage tables together when the discussion table
    # has used most of the page.
    if y < 326:
        new_page()
    table("Assessment coverage", ("Metric", "Missing", "One assessment", "Two or more"),
          assessment_rows, (270, 110, 165, 169), "No evaluations")
    people = drafter.get_player_evidence_report()
    table("Evaluation matching", ("Measure", "Players", "Interpretation"), [
        ("No matching evaluation row", int(people["No Evaluation Match"].sum()),
         "Soft preference to spread these players across teams."),
        ("Matched row, no metric ratings", int((
            ~drafter.df_players["No Evaluation Match"]
            & drafter.df_players[[f"{metric} Assessment Count" for metric in
                ("Overall Level", "Shooting", "Ball-Handling", "Rebounding")]].eq(0).all(axis=1)
        ).sum()), "Missing ratings are imputed; excluded from the no-match pool."),
    ], (226, 80, 408), "No evaluation matching information")

    table("Height evidence", ("Measure", "Players", "Interpretation"), [
        ("Registered height band absent", int(people["Height Band"].isna().sum()),
         "Unknown height; not evidence of being short."),
        ("Positive coach Tall note", int(people["Coach Tall Flag"].fillna(False).sum()),
         "Qualitative description; may overlap a registered band."),
        ("Height wording needs review", int(people["Height Needs Review"].fillna(False).sum()),
         "Unparsed registration or uncertain coach wording."),
    ], (226, 80, 408), "No height evidence")
    if y < 64:
        new_page()
    _para(c, "A single rating has no observed range. Matching entered scores "
          "do not establish certainty. Full player-metric statistics and "
          "source rows are in the companion assessment CSVs.",
          38, y, 710, 7.1, 9, MUTED)

    next_appendix()
    groups = drafter.get_friend_group_report()
    table("Friend-group placements", ("Status", "Assignments by team", "Reason"),
          [(r["Status"], r["Assignments"], r["Reason"])
           for _, r in groups.iterrows()],
          (72, 400, 242), "No friend groups requested")

    friends = drafter.get_friend_report()
    table("Individual friend requests",
          ("Player / team", "Requested friend / team", "Status", "Reason"),
          [(f"{r['Player']} (T{r['Player Team']})",
            f"{r['Requested Friend']} (T{r['Friend Team']})",
            r["Status"], r["Reason"])
           for _, r in friends.iterrows()],
          (139, 148, 95, 332), "No matched requests")
    c.showPage()


def render_report(drafter, path, illustrative=False, report_details=None):
    """Write the currently assigned draft to one shareable PDF."""
    if drafter.assignments is None or drafter.assignments.isna().any():
        raise ValueError("Generate a complete assignment before rendering the primer")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=(PAGE_W, PAGE_H), pageCompression=1)
    c.setTitle("Team Draft Report")
    details = report_details or {}
    subtitle = "  |  ".join(
        str(details[field]).strip() for field in ("Club Name", "Division Details")
        if details.get(field)
    )
    rosters = _rosters(drafter)
    _overview(c, drafter, rosters, illustrative, subtitle)
    _balance(c, drafter, rosters, illustrative, subtitle)
    _appendix(c, drafter, illustrative, subtitle)
    c.save()
    return path
