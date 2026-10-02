"""Prepare and optimize balanced recreational basketball teams.

Requires pandas and ortools. Run beside data/: python auto_drafter.py
Team practice days are loaded from data/teams_config.csv.
Generated-ID reservations live only in this AutoDrafter instance.
Fixed coach assignments and provisional friend-group seeds are placed here.
Friends remain soft preferences; the optimizer may move or split seeds.
Set RUN_OPTIMIZATION=False to inspect initial placements only.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from itertools import combinations
from math import lcm
from pathlib import Path
import csv
import json
import re
import unicodedata

import pandas as pd


DATA_FOLDER = Path("data")
CONSTRAINTS_DATA_FILE = "constraints.csv"
EVALUATIONS_FOLDER = DATA_FOLDER / "evaluations"
IMPUTE_VALUE = 3

TEAMS_CONFIG_FILE = DATA_FOLDER / "teams_config.csv"
TEAM_PRACTICE_DAYS = {}
TEAM_COUNT = 0
SEED_EMPTY_TEAMS = True
RUN_OPTIMIZATION = True
SOLVER_SECONDS_PER_PASS = 20.0
SOLVER_RANDOM_SEED = 42
RATING_SCALE = 1000
EXPORT_FRIEND_REPORT = True
EXPORT_ASSESSMENT_REPORTS = True
GENERATE_TEAM_REPORT_PDF = True
TEAM_REPORT_FILE = "teams_report.pdf"
REPORT_CONFIG_FILE = DATA_FOLDER / "report_config.csv"
TOP_BALL_HANDLER_COUNT = 8
TALL_TARGET_PER_TEAM = None  # Auto: 2 if the pool permits, otherwise 1.
SECOND_YEAR_TARGET_PER_TEAM = 2
BALANCE_UNOBSERVED_PLAYERS = True
PRINT_DETAILED_FRIEND_REQUESTS = False
OUTPUT_FOLDER = Path("output")
FRIEND_REPORT_FILE = "friend_requests.csv"
# Extra max-minus-min team-average spread allowed when preserving friends.
# 0.10 means rating points, not 10 percent. Zero prioritizes balance strictly.
FRIEND_BALANCE_TOLERANCE = 0.10

DOB_FORMAT = "%m/%d/%Y"
# Optional (older birth year, younger birth year). Infer only when the roster
# contains exactly two consecutive birth years; otherwise report for review.
COHORT_BIRTH_YEARS = None

FIRST_NAME = "First Name"
LAST_NAME = "Last Name"
DOB = "Dob"
SORT_OUT_NUMBER = "#"
FIXED_TEAM = "Team"
ASSIGNED_TEAM = "Assigned Team"
BUDDY_REQUEST = "Buddy Request"
BLACKOUT_DAYS = "No Practice Date"
HEIGHT = "Height"
NOTES = "Notes"
OVERALL = "Overall Level"
METRICS = ["Shooting", "Ball-Handling", "Rebounding", OVERALL]
ROSTER_COLUMNS = [
    "Sort Out #", FIRST_NAME, LAST_NAME, DOB, "Player Assessment", HEIGHT,
    BLACKOUT_DAYS, FIXED_TEAM, BUDDY_REQUEST, "Returning Player",
]
EVALUATION_COLUMNS = [
    SORT_OUT_NUMBER, FIRST_NAME, LAST_NAME, NOTES,
] + METRICS

# Replace ALL requests made by a player with explicit target IDs.
# IDs are strings: {"17": ["23", "44"]}. [] waives the request.
FRIEND_OVERRIDES = {}

WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday"}
NO_REQUEST_VALUES = {"none", "no", "n/a", "na", "-"}


def normalize_text(value):
    if pd.isna(value):
        return ""
    return " ".join(
        unicodedata.normalize("NFKC", str(value)).casefold().split()
    )


def normalize_sort_out_number(value):
    """Preserve assigned numbers and previously generated IDs as strings."""
    if pd.isna(value):
        return pd.NA
    text = str(value).strip()
    if re.fullmatch(r"generated:[0-9a-f]{24}", text):
        return text
    try:
        number = Decimal(text)
        if (
            not number.is_finite()
            or number < 0
            or number != number.to_integral_value()
        ):
            raise ValueError
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid sort-out number: {value!r}") from exc
    return str(int(number))


def parse_team_choices(value):
    """Interpret Team as acceptable destinations; a singleton is fixed."""
    text = normalize_text(value)
    if not text:
        return None
    tokens = [token for token in text.split() if token not in {"or", "and"}]
    if not tokens:
        raise ValueError(f"Invalid Team specification: {value!r}")
    choices = set()
    for token in tokens:
        number = normalize_sort_out_number(token)
        if pd.isna(number) or not number.isdecimal() or not 1 <= int(number) <= TEAM_COUNT:
            raise ValueError(f"Invalid Team specification: {value!r}; use configured team numbers")
        choices.add(int(number))
    return frozenset(choices)


def load_teams_config():
    """Load team IDs and practice days for an input-data run."""
    global TEAM_PRACTICE_DAYS, TEAM_COUNT
    config = pd.read_csv(TEAMS_CONFIG_FILE, dtype="string", keep_default_na=False)
    config.columns = config.columns.str.strip().str.title()
    if not {"Team", "Practice Day"}.issubset(config.columns):
        raise ValueError(f"{TEAMS_CONFIG_FILE}: expected Team and Practice Day columns")
    days = {}
    for _, row in config.iterrows():
        number = normalize_sort_out_number(row["Team"])
        day = normalize_text(row["Practice Day"])
        if pd.isna(number) or not number.isdecimal() or int(number) < 1 or int(number) in days:
            raise ValueError(f"{TEAMS_CONFIG_FILE}: team IDs must be unique positive integers")
        if day not in WEEKDAYS:
            raise ValueError(f"{TEAMS_CONFIG_FILE}: invalid practice weekday {row['Practice Day']!r}")
        days[int(number)] = day.title()
    if not days or set(days) != set(range(1, len(days) + 1)):
        raise ValueError(f"{TEAMS_CONFIG_FILE}: use consecutive team IDs starting at 1")
    TEAM_PRACTICE_DAYS = days
    TEAM_COUNT = len(days)
    return days


class AutoDrafter:
    def __init__(self):
        self.df_constraints = None
        self.evaluation_dfs = []
        self.df_aggregated_evaluations = None
        self.df_unmatched_evaluations = None
        self.df_evaluations_missing_id = None
        self.df_evaluation_evidence = None
        self.data_issues = []
        self.cohort_birth_years = None
        self.ball_handling_cutoff = None
        self.tallest_height_band = None
        self.tall_target_per_team = 1
        self.df_players = None
        self.assignments = None
        self.friend_requests = set()
        self.friend_groups = []
        self.friend_issues = []
        self.unmatched_friend_text = []
        self.allowed_teams = {}
        self.practice_days = {}
        self.generated_ids = {}
        self.optimization_result = None
        self.report_details = {}
        self.accepted_locks = {}
        self.change_history = []

    def read_report_config(self):
        """Load optional, reusable PDF subtitle fields from a one-row CSV."""
        self.report_details = {}
        if not REPORT_CONFIG_FILE.exists():
            return self.report_details
        with REPORT_CONFIG_FILE.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            required = {"Club Name", "Division Details"}
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                raise ValueError(
                    f"{REPORT_CONFIG_FILE}: expected columns Club Name and "
                    "Division Details"
                )
            rows = list(reader)
        if len(rows) != 1 or None in rows[0]:
            raise ValueError(
                f"{REPORT_CONFIG_FILE}: expected exactly one row with no extra fields"
            )
        self.report_details = {
            field: (rows[0][field] or "").strip() for field in required
        }
        return self.report_details

    @staticmethod
    def find_evaluation_header(source):
        """Find an assessment header after any introductory CSV rows."""
        required = {SORT_OUT_NUMBER, FIRST_NAME, LAST_NAME}
        with source.open(newline="", encoding="utf-8-sig") as file:
            reader = csv.reader(file)
            while True:
                line_number = reader.line_num
                row = next(reader, None)
                if row is None:
                    return None
                columns = {value.strip().title() for value in row}
                if required.issubset(columns) and columns.intersection(
                    METRICS + [NOTES]
                ):
                    return line_number

    @staticmethod
    def clean_data(df, columns, source):
        df = df.copy()
        df.columns = df.columns.str.strip().str.title()
        if df.columns.duplicated().any():
            raise ValueError(f"{source}: duplicate normalized headers")
        if not {FIRST_NAME, LAST_NAME}.issubset(df.columns):
            raise ValueError(f"{source}: first/last name columns are required")
        # Missing optional columns are treated exactly like empty columns.
        for column in columns:
            if column not in df.columns:
                df[column] = pd.NA
            df[column] = (
                df[column].astype("string").str.strip().replace("", pd.NA)
            )
        df = df[columns].copy()
        df = df.loc[~df.isna().all(axis=1)].copy()
        for column in [FIRST_NAME, LAST_NAME]:
            df[column] = df[column].str.replace(r"\s+", " ", regex=True)
        bad_names = df[[FIRST_NAME, LAST_NAME]].isna().any(axis=1)
        if bad_names.any():
            raise ValueError(
                f"{source}: rows missing names {df.index[bad_names].tolist()}"
            )
        return df

    @staticmethod
    def numeric_column(df, column, lower, upper, integer=False):
        raw = df[column]
        numbers = pd.to_numeric(raw, errors="coerce")
        invalid = raw.notna() & numbers.isna()
        invalid |= (numbers.lt(lower) | numbers.gt(upper)).fillna(False)
        if integer:
            invalid |= (numbers.notna() & numbers.mod(1).ne(0))
        if invalid.any():
            raise ValueError(f"Invalid {column}: {raw[invalid].to_dict()}")
        df[column] = numbers.astype("Int64" if integer else "Float64")

    @staticmethod
    def check_unique_ids(df, source):
        ids = df[SORT_OUT_NUMBER].dropna()
        duplicates = ids[ids.duplicated()].tolist()
        if duplicates:
            raise ValueError(f"{source}: duplicate IDs {duplicates}")

    def generate_missing_ids(self, roster):
        """Allocate readable IDs using an in-memory name/DOB lookup."""
        lookup = self.generated_ids.copy()
        owners = {number: identity for identity, number in lookup.items()}
        identities = {}
        for index, player in roster.iterrows():
            birth_date = normalize_text(player[DOB])
            if DOB_FORMAT:
                birth_date = datetime.strptime(
                    str(player[DOB]), DOB_FORMAT
                ).date().isoformat()
            identities[index] = (
                normalize_text(player[FIRST_NAME]),
                normalize_text(player[LAST_NAME]),
                birth_date,
            )

        # Reserve evaluation-only IDs too, so an invented ID cannot match an
        # unrelated assessment during the later merge.
        evaluation_ids = {
            int(value)
            for df in self.evaluation_dfs
            for value in df[SORT_OUT_NUMBER].dropna()
            if value.isdecimal()
        }
        roster_ids = set()
        for index, value in roster[SORT_OUT_NUMBER].dropna().items():
            number = int(value)
            owner = owners.get(number)
            if owner is not None and owner != identities[index]:
                raise ValueError(
                    f"Sort-out number {number} is already reserved for "
                    "another player in the generated-ID lookup."
                )
            roster_ids.add(number)
            if owner is not None:
                roster.at[index, "Generated Id"] = True

        reserved = roster_ids | evaluation_ids | set(owners)
        next_number = max(reserved, default=0) + 1
        missing_indices = roster.index[roster[SORT_OUT_NUMBER].isna()]
        # Sorting makes first-time allocation independent of CSV row order.
        for index in sorted(missing_indices, key=identities.get):
            identity = identities[index]
            if list(identities.values()).count(identity) > 1:
                raise ValueError(
                    f"Duplicate identity {identity}; supply distinct "
                    "sort-out numbers or correct the registration data."
                )
            if identity in lookup:
                number = lookup[identity]
                if number in roster_ids or number in evaluation_ids:
                    raise ValueError(
                        f"Previously generated ID {number} appears in input "
                        "data. Resolve ownership and enter the correct "
                        "sort-out number in registration before continuing."
                    )
            else:
                number = next_number
                next_number += 1
                lookup[identity] = number
            roster.at[index, SORT_OUT_NUMBER] = str(number)
        self.check_unique_ids(roster, "Registration/generated IDs")

        # Commit only after validation. Keep absent players' reservations
        # for this instance, but never read or write an ID lookup file.
        self.generated_ids = lookup

    def read_data(self):
        load_teams_config()
        self.assignments = None
        self.optimization_result = None
        self.evaluation_dfs = []
        self.allowed_teams = {}
        source = Path(DATA_FOLDER) / CONSTRAINTS_DATA_FILE
        roster = self.clean_data(
            pd.read_csv(source, dtype="string", keep_default_na=False),
            ROSTER_COLUMNS, source,
        ).rename(columns={"Sort Out #": SORT_OUT_NUMBER})
        if roster[DOB].isna().any():
            raise ValueError("Registration DOB is required for every player")
        birth_dates = pd.to_datetime(
            roster[DOB], format=DOB_FORMAT, errors="coerce"
        )
        if birth_dates.isna().any():
            invalid = roster.loc[birth_dates.isna(), DOB].tolist()
            raise ValueError(f"DOB must use month/day/year: {invalid}")
        roster["Birth Year"] = birth_dates.dt.year.astype("Int64")
        # Registration uses this note for players without an assigned number.
        did_not_attend = roster[SORT_OUT_NUMBER].map(normalize_text).eq(
            "can't attend"
        )
        roster.loc[did_not_attend, SORT_OUT_NUMBER] = pd.NA
        roster[SORT_OUT_NUMBER] = roster[SORT_OUT_NUMBER].map(
            normalize_sort_out_number
        ).astype("string")
        # Upgrade any hash IDs copied from the earlier script version.
        legacy_ids = roster[SORT_OUT_NUMBER].str.fullmatch(
            r"generated:[0-9a-f]{24}", na=False
        )
        roster.loc[legacy_ids, SORT_OUT_NUMBER] = pd.NA
        roster["Generated Id"] = roster[SORT_OUT_NUMBER].isna()
        # Validate the existing export column without changing its schema.
        roster[FIXED_TEAM].map(parse_team_choices)
        self.check_unique_ids(roster, "Registration")

        if not EVALUATIONS_FOLDER.is_dir():
            raise FileNotFoundError(
                f"Create {EVALUATIONS_FOLDER} and place evaluation CSVs there"
            )
        sources = sorted(
            (path for path in EVALUATIONS_FOLDER.iterdir()
             if path.is_file() and path.suffix.lower() == ".csv"),
            key=lambda path: (path.name.casefold(), path.name),
        )
        ignored = []
        for source in sources:
            header = self.find_evaluation_header(source)
            if header is None:
                ignored.append(source.name)
                continue
            # Pandas skiprows counts parsed CSV records differently when the
            # introductory title contains quoted newlines. Position the file
            # at the physical header line discovered by csv.reader instead.
            with source.open(newline="", encoding="utf-8-sig") as file:
                for _ in range(header):
                    next(file)
                df = pd.read_csv(
                    file, header=0, dtype="string", keep_default_na=False,
                )
            df = self.clean_data(df, EVALUATION_COLUMNS, source)
            df[SORT_OUT_NUMBER] = df[SORT_OUT_NUMBER].map(
                normalize_sort_out_number
            ).astype("string")
            self.check_unique_ids(df, source.name)
            for metric in METRICS:
                self.numeric_column(df, metric, 1, 5)
            df["Evaluation Source"] = source.name
            self.evaluation_dfs.append(df)

        if ignored:
            print("Skipped CSVs without an evaluation header: "
                  + ", ".join(ignored))
        if not self.evaluation_dfs:
            raise ValueError(
                f"No evaluation CSVs with #, First Name, Last Name and "
                f"a rating or Notes column found in {EVALUATIONS_FOLDER}"
            )

        self.generate_missing_ids(roster)
        self.df_constraints = roster

    @staticmethod
    def classify_height_note(value):
        """Detect explicit tall descriptions; flag uncertain mentions."""
        if pd.isna(value):
            return False, False
        positive, review = False, False
        for clause in re.split(r"[.;,\n]+", str(value)):
            clause = normalize_text(clause)
            if not re.search(r"\btall\b", clause):
                continue
            uncertain = "?" in clause or re.search(
                r"\b(not|no|never|hardly|barely|maybe|perhaps|possibly|"
                r"if|whether|might|may|unsure|isn't|isn’t|wasn't|wasn’t|"
                r"aren't|aren’t)\b",
                clause,
            )
            if uncertain:
                review = True
            else:
                positive = True
        return positive, review

    def aggregate_evaluations(self):
        if self.evaluation_dfs:
            combined = pd.concat(self.evaluation_dfs, ignore_index=True)
        else:
            combined = pd.DataFrame({
                SORT_OUT_NUMBER: pd.Series(dtype="string"),
                **{metric: pd.Series(dtype="Float64") for metric in METRICS},
            })
        for column in [FIRST_NAME, LAST_NAME, NOTES, "Evaluation Source"]:
            if column not in combined:
                combined[column] = pd.Series(pd.NA, index=combined.index,
                                             dtype="string")
        flags = [self.classify_height_note(value) for value in combined[NOTES]]
        combined["Tall Note"] = [flag[0] for flag in flags]
        combined["Tall Mention Review"] = [flag[1] for flag in flags]
        self.df_evaluation_evidence = combined.copy()
        missing = combined[SORT_OUT_NUMBER].isna()
        self.df_evaluations_missing_id = combined.loc[missing].copy()
        grouped = combined.loc[~missing].groupby(SORT_OUT_NUMBER)
        means = grouped[METRICS].mean()
        counts = grouped[METRICS].count()
        minimum = grouped[METRICS].min()
        maximum = grouped[METRICS].max()
        # A single observation has no assessable disagreement. Do not report
        # its arithmetic range of zero as agreement between evaluators.
        ranges = (maximum - minimum).where(counts >= 2)
        tall_counts = grouped[["Tall Note", "Tall Mention Review"]].sum()
        tall_counts.columns = ["Tall Note Count", "Tall Review Count"]
        self.df_aggregated_evaluations = pd.concat([
            means,
            means.add_suffix(" Observed Mean"),
            counts.add_suffix(" Assessment Count"),
            minimum.add_suffix(" Min"),
            maximum.add_suffix(" Max"),
            ranges.add_suffix(" Range"),
            tall_counts,
        ], axis=1).reset_index()

    def transform_data(self):
        self.assignments = None
        self.optimization_result = None
        self.aggregate_evaluations()
        aggregated = self.df_aggregated_evaluations
        roster = self.df_constraints
        self.df_unmatched_evaluations = aggregated.loc[
            ~aggregated[SORT_OUT_NUMBER].isin(roster[SORT_OUT_NUMBER])
        ].copy()
        self.df_players = roster.merge(
            aggregated, on=SORT_OUT_NUMBER, how="left",
            validate="one_to_one", indicator="_evaluation_match",
        )
        # A matched, blank evaluation row still counts as an evaluation row.
        # Keep this distinct from per-metric missingness and imputed ratings.
        self.df_players["No Evaluation Match"] = (
            self.df_players.pop("_evaluation_match").eq("left_only")
        )
        for metric in METRICS:
            self.df_players[f"{metric} Imputed"] = (
                self.df_players[metric].isna()
            )
            self.df_players[metric] = self.df_players[metric].fillna(
                IMPUTE_VALUE
            )
            column = f"{metric} Assessment Count"
            self.df_players[column] = (
                self.df_players[column].fillna(0).astype("Int64")
            )
        self.prepare_secondary_metrics()

    @staticmethod
    def parse_height_range(value):
        """Read the explicit centimetre interval, never invent a height."""
        if pd.isna(value) or not str(value).strip():
            return pd.NA, pd.NA, pd.NA
        match = re.search(
            r"(\d+(?:\.\d+)?)\s*cm\s*(?:to|[-–])\s*"
            r"(\d+(?:\.\d+)?)\s*cm",
            normalize_text(value),
        )
        if match is None:
            return pd.NA, pd.NA, pd.NA
        lower, upper = map(float, match.groups())
        if lower <= 0 or upper < lower:
            return pd.NA, pd.NA, pd.NA
        return lower, upper, f"{lower:g}–{upper:g} cm"

    def prepare_secondary_metrics(self):
        """Prepare observed pools and cohorts for reporting and soft targets."""
        self.data_issues = []
        players = self.df_players
        years = sorted(int(year) for year in players["Birth Year"].unique())
        configured = COHORT_BIRTH_YEARS is not None
        eligible = sorted(COHORT_BIRTH_YEARS) if configured else years
        valid_pair = (
            len(eligible) == 2
            and all(type(year) is int for year in eligible)
            and eligible[1] == eligible[0] + 1
        )
        if configured and not valid_pair:
            raise ValueError("COHORT_BIRTH_YEARS needs two consecutive years")
        self.cohort_birth_years = tuple(eligible) if valid_pair else None
        if valid_pair:
            mapping = {eligible[0]: 2, eligible[1]: 1}
            players["Age Year"] = players["Birth Year"].map(mapping).astype(
                "Int64"
            )
            unexpected = sorted(set(years) - set(eligible))
            if unexpected:
                self.data_issues.append(
                    f"Birth years outside configured cohorts: {unexpected}"
                )
        else:
            players["Age Year"] = pd.Series(
                pd.NA, index=players.index, dtype="Int64"
            )
            self.data_issues.append(
                f"Cannot infer two age cohorts from birth years {years}; "
                "set COHORT_BIRTH_YEARS to the eligible years."
            )
        players["Age Cohort"] = players["Age Year"].map(
            {1: "Year 1", 2: "Year 2"}
        ).astype("string")
        heights = [self.parse_height_range(value) for value in players[HEIGHT]]
        for position, column in enumerate(
            ["Height Min Cm", "Height Max Cm", "Height Band"]
        ):
            dtype = "string" if position == 2 else "Float64"
            players[column] = pd.Series(
                [height[position] for height in heights],
                index=players.index, dtype=dtype,
            )
        for column in ["Tall Note Count", "Tall Review Count"]:
            players[column] = players[column].fillna(0).astype("Int64")
        players["Coach Tall Flag"] = players["Tall Note Count"] > 0
        players["Height Evidence"] = "Unknown"
        measured = players["Height Band"].notna()
        players.loc[measured, "Height Evidence"] = "Registered range"
        note_only = ~measured & players["Coach Tall Flag"]
        players.loc[note_only, "Height Evidence"] = "Coach description only"
        unparsed = players[HEIGHT].notna() & ~measured
        if unparsed.any():
            self.data_issues.append(
                "Unparsed height values (left unknown): "
                + repr(players.loc[unparsed, HEIGHT].unique().tolist())
            )
        players["Height Needs Review"] = (
            unparsed | (players["Tall Review Count"] > 0)
        )
        bands = (
            players.loc[measured, ["Height Min Cm", "Height Max Cm"]]
            .drop_duplicates().sort_values(["Height Min Cm", "Height Max Cm"])
        )
        self.tallest_height_band = (
            tuple(bands.iloc[-1]) if not bands.empty else None
        )
        tallest_registered = pd.Series(False, index=players.index)
        if self.tallest_height_band is not None:
            lower, upper = self.tallest_height_band
            tallest_registered = (
                players["Height Min Cm"].eq(lower)
                & players["Height Max Cm"].eq(upper)
            ).fillna(False)
        players["Tall Evidence Pool"] = (
            tallest_registered | players["Coach Tall Flag"]
        )
        for name, target in [
            ("TALL_TARGET_PER_TEAM", TALL_TARGET_PER_TEAM),
            ("SECOND_YEAR_TARGET_PER_TEAM", SECOND_YEAR_TARGET_PER_TEAM),
        ]:
            if target is None and name == "TALL_TARGET_PER_TEAM":
                continue
            if type(target) is not int or target < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        self.tall_target_per_team = (
            2 if int(players["Tall Evidence Pool"].sum()) >= 2 * TEAM_COUNT
            else 1
        ) if TALL_TARGET_PER_TEAM is None else TALL_TARGET_PER_TEAM
        if (
            type(TOP_BALL_HANDLER_COUNT) is not int
            or TOP_BALL_HANDLER_COUNT < 1
        ):
            raise ValueError(
                "TOP_BALL_HANDLER_COUNT must be a positive integer"
            )
        metric = "Ball-Handling"
        observed = players.loc[
            players[f"{metric} Assessment Count"] > 0,
            f"{metric} Observed Mean",
        ]
        self.ball_handling_cutoff = (
            float(observed.nlargest(
                min(TOP_BALL_HANDLER_COUNT, len(observed))
            ).min())
            if len(observed) else None
        )
        players["Top Ball-Handling Pool"] = False
        if self.ball_handling_cutoff is not None:
            players["Top Ball-Handling Pool"] = (
                (players[f"{metric} Assessment Count"] > 0)
                & players[f"{metric} Observed Mean"].ge(
                    self.ball_handling_cutoff
                ).fillna(False)
            )

    def find_friend_targets(self, request, names, pattern, player_id):
        """Find complete registered names anywhere in free-form requests."""
        text = normalize_text(request)
        if not text or text in NO_REQUEST_VALUES:
            return []
        targets = []
        remainder = list(text)
        for match in pattern.finditer(text):
            name = match.group()
            matches = names[name]
            if len(matches) == 1:
                targets.append(matches[0])
            else:
                self.friend_issues.append(
                    f"{player_id}: ambiguous friend name {name!r}; "
                    f"matches={matches}"
                )
            remainder[match.start():match.end()] = " " * len(name)
        # Retain unrecognized text for review without losing valid matches.
        # It may contain another name, a typo, or just a carpool comment.
        remainder = "".join(remainder)
        remainder = re.sub(r"(?<!\w)\d+[.)]\s*", " ", remainder)
        remainder = re.sub(r"\band\b", " ", remainder)
        remainder = re.sub(r"[,;|&/().]+", " ", remainder)
        remainder = " ".join(remainder.split())
        if remainder:
            self.unmatched_friend_text.append(
                f"{player_id}: unmatched request text {remainder!r}; "
                "recognized friends were retained"
            )
        return targets

    def resolve_friend_groups(self):
        self.friend_requests = set()
        self.friend_groups = []
        self.friend_issues = []
        self.unmatched_friend_text = []
        players = self.df_players.set_index(SORT_OUT_NUMBER)
        adjacency = {player_id: set() for player_id in players.index}
        names = {}
        for player_id, player in players.iterrows():
            name = normalize_text(f"{player[FIRST_NAME]} {player[LAST_NAME]}")
            names.setdefault(name, []).append(player_id)
        # Longest alternatives first prevent a shorter registered name from
        # consuming part of a longer registered name. Boundary checks avoid
        # matching Ann Lee inside Ann Leeman or Jill McDonald-Smith.
        alternatives = "|".join(
            re.escape(name)
            for name in sorted(names, key=lambda name: (-len(name), name))
        )
        pattern = re.compile(
            r"(?<![\w'’\-])(?:" + alternatives + r")(?![\w'’\-])"
        )
        for player_id in FRIEND_OVERRIDES:
            if player_id not in adjacency:
                raise ValueError(f"Unknown override source ID {player_id!r}")
        for player_id, player in players.iterrows():
            if player_id in FRIEND_OVERRIDES:
                targets = FRIEND_OVERRIDES[player_id]
                if not isinstance(targets, (list, tuple)):
                    raise ValueError("Friend overrides must contain ID lists")
                if any(target not in adjacency for target in targets):
                    raise ValueError(
                        f"Unknown override target for {player_id}"
                    )
            else:
                targets = self.find_friend_targets(
                    player[BUDDY_REQUEST], names, pattern, player_id
                )
            for target in targets:
                if target == player_id:
                    continue
                self.friend_requests.add((player_id, target))
                adjacency[player_id].add(target)
                adjacency[target].add(player_id)
        # Requests retain their direction: mutual requests count as two
        # requested relationships. Groups describe connectivity only.
        visited = set()
        for start in sorted(adjacency):
            if start in visited:
                continue
            group, pending = [], [start]
            visited.add(start)
            while pending:
                player_id = pending.pop()
                group.append(player_id)
                for friend in sorted(adjacency[player_id]):
                    if friend not in visited:
                        visited.add(friend)
                        pending.append(friend)
            self.friend_groups.append(sorted(group))

    @staticmethod
    def parse_blackout_day(value):
        """Return the single dropdown weekday, or None for no restriction."""
        key = normalize_text(value)
        if not key:
            return None
        if key not in WEEKDAYS:
            raise ValueError(f"Expected a single blackout weekday: {value!r}")
        return key.title()

    def build_allowed_teams(self):
        self.allowed_teams = {}
        self.practice_days = {}
        team_numbers = set(range(1, TEAM_COUNT + 1))
        if set(TEAM_PRACTICE_DAYS) != team_numbers:
            raise ValueError("Configure a practice day for every team number")
        for team, value in TEAM_PRACTICE_DAYS.items():
            day = normalize_text(value)
            if day not in WEEKDAYS:
                raise ValueError(
                    f"Set Team {team} in {TEAMS_CONFIG_FILE} to a weekday"
                )
            self.practice_days[team] = day.title()
        allowed_teams = {}
        for _, player in self.df_players.iterrows():
            player_id = player[SORT_OUT_NUMBER]
            blocked = self.parse_blackout_day(player[BLACKOUT_DAYS])
            allowed = {
                team for team, day in self.practice_days.items()
                if day != blocked
            }
            choices = parse_team_choices(player[FIXED_TEAM])
            if choices is not None:
                allowed &= choices
            if not allowed:
                raise ValueError(
                    f"Player {player_id}: Team {player[FIXED_TEAM]!r} and blackout "
                    f"{blocked!r} leave no eligible team; resolve this hard conflict."
                )
            if player_id in self.accepted_locks:
                accepted = self.accepted_locks[player_id]
                if accepted not in allowed:
                    raise ValueError(f"Accepted placement conflicts with original constraints: {player_id}, Team {accepted}")
                allowed = {accepted}
            allowed_teams[player_id] = frozenset(allowed)
        self.allowed_teams = allowed_teams

    def prepare_assignments(self):
        """Place fixed players, then optionally seed otherwise empty teams."""
        self.assignments = None
        self.optimization_result = None
        self.resolve_friend_groups()
        self.build_allowed_teams()
        self.assignments = pd.Series({
            pid: next(iter(teams)) if len(teams) == 1 else pd.NA
            for pid, teams in self.allowed_teams.items()
        }, dtype="Int64", name=ASSIGNED_TEAM)
        if SEED_EMPTY_TEAMS:
            self.seed_empty_teams()

    @property
    def team_size_bounds(self):
        """Return floor/ceiling team sizes for the actual player count."""
        minimum, remainder = divmod(len(self.df_players), TEAM_COUNT)
        return minimum, minimum + bool(remainder)

    def seed_empty_teams(self):
        """Greedy starting placements only; never create new fixed locks."""
        if self.assignments is None:
            raise ValueError("Call prepare_assignments() first")
        _, maximum = self.team_size_bounds
        empty_teams = {
            team for team in self.practice_days
            if not self.assignments.eq(team).any()
        }
        candidates = []
        for group in self.friend_groups:
            if not 2 <= len(group) <= maximum:
                continue
            if not self.assignments.loc[group].isna().all():
                continue
            eligible = set.intersection(
                *(set(self.allowed_teams[player_id]) for player_id in group)
            ) & empty_teams
            if eligible:
                candidates.append((group, eligible))
        # Reconsider the most constrained empty team after each placement.
        # Prefer groups with fewer team options, then larger groups.
        while empty_teams:
            options = {
                team: [item for item in candidates if team in item[1]]
                for team in empty_teams
            }
            options = {
                team: groups for team, groups in options.items() if groups
            }
            if not options:
                break
            team = min(options, key=lambda item: (len(options[item]), item))
            group, _ = min(
                options[team],
                key=lambda item: (
                    len(item[1] & empty_teams), -len(item[0]), tuple(item[0]),
                ),
            )
            self.assignments.loc[group] = team
            empty_teams.remove(team)
            candidates = [item for item in candidates if item[0] != group]
        # The original Team column remains authoritative for hard locks.
        # The optimizer is free to change these seeded assignments.

    def set_assignments(self, assignments, require_complete=True):
        """Validate a candidate draft before committing it."""
        self.build_allowed_teams()
        expected = set(self.df_players[SORT_OUT_NUMBER])
        if (
            assignments.index.has_duplicates
            or set(assignments.index) != expected
        ):
            raise ValueError(
                "Assignments must contain every player exactly once"
            )
        if require_complete and assignments.isna().any():
            raise ValueError("Some players remain unassigned")
        for player_id, team in assignments.items():
            if pd.isna(team):
                if len(self.allowed_teams[player_id]) == 1:
                    raise ValueError(
                        f"Fixed player {player_id} is unassigned"
                    )
                continue
            if team not in self.allowed_teams[player_id]:
                raise ValueError(f"Player {player_id} cannot join team {team}")
        if require_complete:
            minimum, maximum = self.team_size_bounds
            counts = assignments.value_counts().reindex(
                self.practice_days, fill_value=0
            )
            if not counts.between(minimum, maximum).all():
                raise ValueError(
                    f"Team sizes must be between {minimum} and {maximum}"
                )
        self.assignments = assignments.astype("Int64").rename(ASSIGNED_TEAM)
        self.optimization_result = None

    def optimize_teams(self, required_teams=None, reference_assignments=None):
        """Balance Overall Level, friendships, and staged secondary targets."""
        try:
            from ortools.sat.python import cp_model
        except ImportError as exc:
            raise RuntimeError(
                "Optimization requires OR-Tools: python -m pip install ortools"
            ) from exc
        if self.assignments is None:
            self.prepare_assignments()
        self.optimization_result = None
        self.build_allowed_teams()
        # Scenario locks supplement original coach/availability constraints.
        for pid, team in (required_teams or {}).items():
            if pid not in self.allowed_teams or team not in self.allowed_teams[pid]:
                raise ValueError(f"Requested placement is not allowed: {pid}, Team {team}")
            self.allowed_teams[pid] = {team}
        minimum, maximum = self.team_size_bounds
        if minimum < 1:
            raise ValueError(
                "Optimization requires at least one player per team"
            )
        if FRIEND_BALANCE_TOLERANCE < 0 or SOLVER_SECONDS_PER_PASS <= 0:
            raise ValueError(
                "Use a nonnegative tolerance and positive time limit"
            )

        players = self.df_players.set_index(SORT_OUT_NUMBER)
        player_ids = sorted(players.index)
        teams = sorted(self.practice_days)
        labels = self.player_labels()
        for team in teams:
            forced = [
                pid for pid in player_ids if self.allowed_teams[pid] == {team}
            ]
            eligible = [
                pid for pid in player_ids if team in self.allowed_teams[pid]
            ]
            if len(forced) > maximum:
                raise ValueError(
                    f"Team {team} has {len(forced)} forced players but a "
                    f"limit of {maximum}: "
                    + ", ".join(labels[pid] for pid in forced)
                )
            if len(eligible) < minimum:
                raise ValueError(
                    f"Team {team} needs {minimum} players but only "
                    f"{len(eligible)} satisfy availability/fixed-team rules"
                )

        model = cp_model.CpModel()
        chosen = {}
        team_variables = {}
        for pid in player_ids:
            team_variables[pid] = model.new_int_var(
                min(teams), max(teams), f"team_{pid}"
            )
            for team in teams:
                chosen[pid, team] = model.new_bool_var(f"choose_{pid}_{team}")
                if team not in self.allowed_teams[pid]:
                    model.add(chosen[pid, team] == 0)
            model.add(sum(chosen[pid, team] for team in teams) == 1)
            model.add(team_variables[pid] == sum(
                team * chosen[pid, team] for team in teams
            ))
            initial = self.assignments[pid]
            if pd.notna(initial) and initial in self.allowed_teams[pid]:
                # A seed is a search hint, never a hard assignment.
                model.add_hint(team_variables[pid], int(initial))

        ratings = {
            pid: round(float(players.at[pid, OVERALL]) * RATING_SCALE)
            for pid in player_ids
        }
        # Multiplying means by LCM(size bounds) handles 10- and 11-player
        # teams exactly after input ratings are rounded to RATING_SCALE.
        multiplier = lcm(minimum, maximum)
        mean_units = RATING_SCALE * multiplier
        averages = []
        for team in teams:
            size = model.new_int_var(minimum, maximum, f"size_{team}")
            model.add(size == sum(chosen[pid, team] for pid in player_ids))
            total = sum(
                ratings[pid] * chosen[pid, team] for pid in player_ids
            )
            average = model.new_int_var(
                mean_units, 5 * mean_units, f"average_{team}"
            )
            if minimum == maximum:
                model.add(average == total * (multiplier // minimum))
            else:
                larger = model.new_bool_var(f"larger_team_{team}")
                model.add(size == maximum).only_enforce_if(larger)
                model.add(size == minimum).only_enforce_if(larger.Not())
                model.add(
                    average == total * (multiplier // maximum)
                ).only_enforce_if(larger)
                model.add(
                    average == total * (multiplier // minimum)
                ).only_enforce_if(larger.Not())
            averages.append(average)
        highest = model.new_int_var(mean_units, 5 * mean_units, "highest")
        lowest = model.new_int_var(mean_units, 5 * mean_units, "lowest")
        spread = model.new_int_var(0, 4 * mean_units, "spread")
        model.add_max_equality(highest, averages)
        model.add_min_equality(lowest, averages)
        model.add(spread == highest - lowest)
        model.minimize(spread)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = SOLVER_SECONDS_PER_PASS
        solver.parameters.random_seed = SOLVER_RANDOM_SEED
        solver.parameters.num_search_workers = 1
        print("Optimizing team-average balance...", flush=True)
        status = solver.solve(model)
        if status == cp_model.INFEASIBLE:
            raise ValueError(
                "No draft satisfies team sizes, fixed teams and blackout "
                "days together. Friend requests are soft and cannot cause "
                "this infeasibility. Review those hard constraints."
            )
        if status not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
            raise RuntimeError(
                f"Balance pass: {solver.status_name(status)}. No complete "
                "draft found; increase SOLVER_SECONDS_PER_PASS if timed out."
            )
        balance_status = solver.status_name(status)
        baseline_spread = solver.value(spread)
        best_assignment = {
            pid: solver.value(team_variables[pid]) for pid in player_ids
        }
        limit = min(
            4 * mean_units,
            baseline_spread + round(FRIEND_BALANCE_TOLERANCE * mean_units),
        )
        top_handlers = [
            pid for pid in player_ids
            if bool(players.at[pid, "Top Ball-Handling Pool"])
        ]
        tall_players = [
            pid for pid in player_ids if bool(players.at[pid, "Tall Evidence Pool"])
        ]
        second_year_players = [
            pid for pid in player_ids
            if pd.notna(players.at[pid, "Age Year"])
            and players.at[pid, "Age Year"] == 2
        ]
        unobserved_players = [
            pid for pid in player_ids
            if bool(players.at[pid, "No Evaluation Match"])
        ]
        def pool_counts(pool, assignment):
            return {
                team: sum(assignment[pid] == team for pid in pool)
                for team in teams
            }

        def coverage_counts(assignment):
            return pool_counts(top_handlers, assignment)

        friendship_status = "Skipped (no requests)"
        coverage_status = "Skipped (no observed top ball handlers)"
        secondary_results = {}
        if (self.friend_requests or top_handlers or tall_players
                or second_year_players or unobserved_players):
            model.add(spread <= limit)
            direct, groupmates = self.friend_relationships()
            connected = [pid for pid in player_ids if direct[pid]]
            together = {}
            for group in self.friend_groups:
                for source, target in combinations(sorted(group), 2):
                    pair = (source, target)
                    same = model.new_bool_var(f"together_{source}_{target}")
                    model.add(
                        team_variables[source] == team_variables[target]
                    ).only_enforce_if(same)
                    model.add(
                        team_variables[source] != team_variables[target]
                    ).only_enforce_if(same.Not())
                    together[pair] = same
            mutual_pairs = {
                tuple(sorted((source, target)))
                for source, target in self.friend_requests
                if (target, source) in self.friend_requests
            }
            mutual_kept = sum(together[pair] for pair in mutual_pairs)
            direct_flags, group_flags = [], []
            for pid in connected:
                has_direct = model.new_bool_var(f"has_direct_{pid}")
                has_group = model.new_bool_var(f"has_group_{pid}")
                model.add_max_equality(has_direct, [
                    together[tuple(sorted((pid, other)))]
                    for other in sorted(direct[pid])
                ])
                model.add_max_equality(has_group, [
                    together[tuple(sorted((pid, other)))]
                    for other in sorted(groupmates[pid])
                ])
                direct_flags.append(has_direct)
                group_flags.append(has_group)
            honoured = sum(
                together[tuple(sorted(pair))]
                for pair in sorted(self.friend_requests)
            )
            def weighted_score(criteria):
                """Encode strict priority using bounds for each criterion."""
                expression, baseline, maximum = 0, 0, 0
                for term, initial, upper in criteria:
                    expression = expression * (upper + 1) + term
                    baseline = baseline * (upper + 1) + initial
                    maximum = maximum * (upper + 1) + upper
                if maximum * (limit + 1) > (1 << 60):
                    raise ValueError(
                        "Preference objective too large; use staged "
                        "objectives for this league size or reduce precision."
                    )
                return expression * (limit + 1) - spread, baseline

            def friendship_criteria(assignment):
                stats = self.friendship_statistics(assignment)
                return [
                    (mutual_kept, stats["mutual_pairs_kept"], len(mutual_pairs)),
                    (sum(direct_flags), stats["direct_companions"], len(connected)),
                    (sum(group_flags), stats["group_companions"], len(connected)),
                    (honoured, stats["honoured_requests"], len(self.friend_requests)),
                ]

            if self.friend_requests:
                objective, baseline_score = weighted_score(
                    friendship_criteria(best_assignment)
                )
                model.maximize(objective)
                model.clear_hints()
                for pid, team in best_assignment.items():
                    model.add_hint(team_variables[pid], team)
                print("Optimizing friend requests within balance tolerance...",
                      flush=True)
                status = solver.solve(model)
                friendship_status = solver.status_name(status)
                if (
                    status in (cp_model.FEASIBLE, cp_model.OPTIMAL)
                    and solver.value(objective) >=
                    baseline_score * (limit + 1) - baseline_spread
                ):
                    best_assignment = {
                        pid: solver.value(team_variables[pid])
                        for pid in player_ids
                    }
                    baseline_spread_for_coverage = solver.value(spread)
                elif status in (cp_model.UNKNOWN, cp_model.FEASIBLE):
                    friendship_status += " (kept balance-pass draft)"
                    baseline_spread_for_coverage = baseline_spread
                else:
                    raise RuntimeError(
                        f"Unexpected friendship-pass status: {friendship_status}"
                    )
            else:
                baseline_spread_for_coverage = baseline_spread

            before_coverage = best_assignment.copy()
            if top_handlers:
                # Credit at most two top observed handlers on each team.
                # Additional handlers never improve this criterion.
                covered = []
                for team in teams:
                    count = model.new_int_var(
                        0, len(top_handlers), f"top_handlers_{team}"
                    )
                    model.add(count == sum(
                        chosen[pid, team] for pid in top_handlers
                    ))
                    credited = model.new_int_var(0, 2, f"handler_credit_{team}")
                    model.add_min_equality(credited, [count, 2])
                    covered.append(credited)
                coverage = sum(covered)
                counts = coverage_counts(before_coverage)
                priorities = friendship_criteria(before_coverage)
                priorities.insert(3, (
                    coverage, sum(min(2, count) for count in counts.values()),
                    min(len(top_handlers), 2 * len(teams)),
                ))
                objective, baseline_score = weighted_score(priorities)
                model.maximize(objective)
                model.clear_hints()
                for pid, team in best_assignment.items():
                    model.add_hint(team_variables[pid], team)
                print("Optimizing two top ball handlers per team within "
                      "balance and friendship priorities...", flush=True)
                status = solver.solve(model)
                coverage_status = solver.status_name(status)
                if (
                    status in (cp_model.FEASIBLE, cp_model.OPTIMAL)
                    and solver.value(objective) >= baseline_score * (limit + 1)
                    - baseline_spread_for_coverage
                ):
                    best_assignment = {
                        pid: solver.value(team_variables[pid])
                        for pid in player_ids
                    }
                elif status in (cp_model.UNKNOWN, cp_model.FEASIBLE):
                    coverage_status += " (kept friendship-pass draft)"
                else:
                    raise RuntimeError(
                        f"Unexpected coverage-pass status: {coverage_status}"
                    )
        else:
            before_coverage = best_assignment.copy()

        # Further secondary targets preserve the achieved friendship counts
        # and capped handling coverage. Separate passes avoid enormous weights
        # as more criteria are added, and retain the incumbent on timeout.
        secondary_specs = [
            ("tall", tall_players, self.tall_target_per_team),
            ("second_year", second_year_players, SECOND_YEAR_TARGET_PER_TEAM),
            ("unobserved", unobserved_players, None),
        ]
        active_secondary = any(
            pool and (target is None or target > 0)
            and (name != "unobserved" or BALANCE_UNOBSERVED_PLAYERS)
            for name, pool, target in secondary_specs
        )
        if active_secondary:
            for term, achieved, _ in friendship_criteria(best_assignment):
                model.add(term >= achieved)

            def capped_coverage(name, pool, target):
                credits = []
                for team in teams:
                    count = sum(chosen[pid, team] for pid in pool)
                    credit = model.new_int_var(0, target, f"{name}_credit_{team}")
                    model.add_min_equality(credit, [count, target])
                    credits.append(credit)
                return sum(credits)

            if top_handlers:
                handler_coverage = capped_coverage("handling_floor", top_handlers, 2)
                model.add(handler_coverage >= sum(
                    min(2, value) for value in coverage_counts(best_assignment).values()
                ))

            for name, pool, target in secondary_specs:
                before = best_assignment.copy()
                counts = pool_counts(pool, before)
                enabled = name != "unobserved" or BALANCE_UNOBSERVED_PLAYERS
                if not enabled or not pool or target == 0:
                    secondary_results[name] = {
                        "status": "Skipped (disabled or no eligible players)",
                        "target": target, "pool_size": len(pool),
                        "before": counts, "after": counts,
                        "moves": [],
                    }
                    continue
                if target is None:
                    # Minimize the max-minus-min count spread for players
                    # with no matching evaluation row, irrespective of ratings.
                    team_counts = []
                    for team in teams:
                        count = model.new_int_var(0, len(pool), f"{name}_count_{team}")
                        model.add(count == sum(chosen[pid, team] for pid in pool))
                        team_counts.append(count)
                    highest_count = model.new_int_var(0, len(pool), f"{name}_high")
                    lowest_count = model.new_int_var(0, len(pool), f"{name}_low")
                    model.add_max_equality(highest_count, team_counts)
                    model.add_min_equality(lowest_count, team_counts)
                    term = len(pool) - highest_count + lowest_count
                    initial_credit = len(pool) - max(counts.values()) + min(counts.values())
                else:
                    term = capped_coverage(name, pool, target)
                    initial_credit = sum(min(target, value) for value in counts.values())
                incumbent_spread = max(
                    sum(ratings[pid] for pid in player_ids if before[pid] == team)
                    * (multiplier // sum(before[pid] == team for pid in player_ids))
                    for team in teams
                ) - min(
                    sum(ratings[pid] for pid in player_ids if before[pid] == team)
                    * (multiplier // sum(before[pid] == team for pid in player_ids))
                    for team in teams
                )
                objective = term * (limit + 1) - spread
                model.maximize(objective)
                model.clear_hints()
                for pid, team in before.items():
                    model.add_hint(team_variables[pid], team)
                print(f"Optimizing {name.replace('_', ' ')} soft target...", flush=True)
                status = solver.solve(model)
                pass_status = solver.status_name(status)
                achieved = initial_credit
                if (
                    status in (cp_model.FEASIBLE, cp_model.OPTIMAL)
                    and solver.value(objective) >= initial_credit * (limit + 1)
                    - incumbent_spread
                ):
                    best_assignment = {
                        pid: solver.value(team_variables[pid]) for pid in player_ids
                    }
                    achieved = solver.value(term)
                elif status in (cp_model.UNKNOWN, cp_model.FEASIBLE):
                    pass_status += " (kept previous draft)"
                else:
                    raise RuntimeError(f"Unexpected {name} status: {pass_status}")
                model.add(term >= achieved)
                secondary_results[name] = {
                    "status": pass_status, "target": target, "pool_size": len(pool),
                    "before": counts, "after": pool_counts(pool, best_assignment),
                    "moves": [
                        (labels[pid], before[pid], best_assignment[pid])
                        for pid in player_ids if before[pid] != best_assignment[pid]
                    ],
                }
        # Also expose counts when every secondary pool is empty or disabled.
        for name, pool, target in secondary_specs:
            if name not in secondary_results:
                counts = pool_counts(pool, best_assignment)
                secondary_results[name] = {
                    "status": "Skipped (disabled or no eligible players)",
                    "target": target, "pool_size": len(pool),
                    "before": counts, "after": counts, "moves": [],
                }
        movement_status = "Skipped (no reference draft)"
        if reference_assignments is not None:
            if set(reference_assignments.index) != set(player_ids):
                raise ValueError("Reference draft must contain every player exactly once")
            if any(int(reference_assignments[pid]) not in teams for pid in player_ids):
                raise ValueError("Reference draft contains an unknown team")
            # Preserve achieved priorities before minimizing disruption. This
            # is a final tie-breaker, never a replacement for balance/friends.
            if (self.friend_requests or top_handlers or tall_players
                    or second_year_players or unobserved_players):
                for term, achieved, _ in friendship_criteria(best_assignment):
                    model.add(term >= achieved)
            for name, pool, target in [
                ("movement_handling", top_handlers, 2),
                ("movement_tall", tall_players, self.tall_target_per_team),
                ("movement_age", second_year_players, SECOND_YEAR_TARGET_PER_TEAM),
            ]:
                if pool and target > 0:
                    credits = []
                    for team in teams:
                        credit = model.new_int_var(0, target, f"{name}_{team}")
                        model.add_min_equality(credit, [
                            sum(chosen[pid, team] for pid in pool), target,
                        ])
                        credits.append(credit)
                    model.add(sum(credits) >= sum(
                        min(target, value) for value in pool_counts(pool, best_assignment).values()
                    ))
            if unobserved_players and BALANCE_UNOBSERVED_PLAYERS:
                counts = pool_counts(unobserved_players, best_assignment)
                for left, right in combinations(teams, 2):
                    difference = sum(chosen[pid, left] - chosen[pid, right]
                                     for pid in unobserved_players)
                    bound = max(counts.values()) - min(counts.values())
                    model.add(difference <= bound)
                    model.add(difference >= -bound)
            incumbent_means = [
                sum(ratings[pid] for pid in player_ids if best_assignment[pid] == team)
                * (multiplier // sum(best_assignment[pid] == team for pid in player_ids))
                for team in teams
            ]
            model.add(spread <= max(incumbent_means) - min(incumbent_means))
            moved = sum(1 - chosen[pid, int(reference_assignments[pid])]
                        for pid in player_ids)
            incumbent_moves = sum(best_assignment[pid] != int(reference_assignments[pid])
                                  for pid in player_ids)
            model.minimize(moved)
            model.clear_hints()
            for pid, team in best_assignment.items():
                model.add_hint(team_variables[pid], team)
            print("Minimizing additional moves while preserving achieved priorities...", flush=True)
            status = solver.solve(model)
            movement_status = solver.status_name(status)
            if status in (cp_model.FEASIBLE, cp_model.OPTIMAL) and solver.value(moved) <= incumbent_moves:
                best_assignment = {pid: solver.value(team_variables[pid]) for pid in player_ids}
            else:
                movement_status += " (kept previous draft)"
        self.set_assignments(pd.Series(best_assignment, dtype="Int64"))
        means = [float(team[OVERALL].mean()) for team in self.teams.values()]
        baseline_means = [
            sum(float(players.at[pid, OVERALL])
                for pid in player_ids if before_coverage[pid] == team)
            / sum(before_coverage[pid] == team for pid in player_ids)
            for team in teams
        ]
        self.optimization_result = {
            "balance_status": balance_status,
            "movement_status": movement_status,
            "friendship_status": friendship_status,
            "coverage_status": coverage_status,
            "baseline_spread": baseline_spread / mean_units,
            "spread_limit": limit / mean_units,
            "actual_spread": max(means) - min(means),
            "pre_coverage_spread": max(baseline_means) - min(baseline_means),
            "top_handler_pool_size": len(top_handlers),
            "top_handlers_before": coverage_counts(before_coverage),
            "top_handlers_after": coverage_counts(best_assignment),
            "secondary_targets": secondary_results,
            "tall_after": pool_counts(tall_players, best_assignment),
            "second_year_after": pool_counts(second_year_players, best_assignment),
            "unobserved_after": pool_counts(unobserved_players, best_assignment),
            "friendship_before_coverage": self.friendship_statistics(before_coverage),
            "moves_for_coverage": [
                (labels[pid], before_coverage[pid], best_assignment[pid])
                for pid in player_ids
                if before_coverage[pid] != best_assignment[pid]
            ],
            **self.friendship_statistics(),
        }
        return self.optimization_result

    def get_assigned_players(self):
        if self.assignments is None:
            raise ValueError("Call prepare_assignments() first")
        return self.df_players.join(
            self.assignments, on=SORT_OUT_NUMBER, validate="one_to_one"
        )

    @property
    def teams(self):
        """Computed views: assignments are the sole assignment source."""
        assigned = self.get_assigned_players()
        return {
            team: assigned.loc[
                assigned[ASSIGNED_TEAM].eq(team).fillna(False)
            ].copy()
            for team in range(1, TEAM_COUNT + 1)
        }

    def get_unassigned_players(self):
        assigned = self.get_assigned_players()
        return assigned.loc[assigned[ASSIGNED_TEAM].isna()].copy()

    def player_labels(self):
        """Use names in reports; include DOB only to distinguish duplicates."""
        players = self.df_players.set_index(SORT_OUT_NUMBER)
        names = players[FIRST_NAME] + " " + players[LAST_NAME]
        duplicates = names.map(normalize_text).duplicated(keep=False)
        return {
            player_id: (
                f"{name} (DOB: {players.at[player_id, DOB]})"
                if duplicates[player_id] else name
            )
            for player_id, name in names.items()
        }

    def friend_relationships(self):
        """Return direct neighbours and groupmates for each player ID."""
        direct = {pid: set() for pid in self.df_players[SORT_OUT_NUMBER]}
        for source, target in self.friend_requests:
            direct[source].add(target)
            direct[target].add(source)
        groupmates = {pid: set() for pid in direct}
        for group in self.friend_groups:
            for pid in group:
                groupmates[pid] = set(group) - {pid}
        return direct, groupmates

    def friendship_statistics(self, assignments=None):
        """Measure direct companionship and the friends-of-friends fallback."""
        assignments = self.assignments if assignments is None else assignments
        if assignments is None:
            raise ValueError("Call prepare_assignments() first")
        direct, groupmates = self.friend_relationships()

        def together(left, right):
            return bool(
                pd.notna(assignments[left])
                and pd.notna(assignments[right])
                and assignments[left] == assignments[right]
            )

        mutual = {
            tuple(sorted((source, target)))
            for source, target in self.friend_requests
            if (target, source) in self.friend_requests
        }
        connected = [pid for pid in direct if direct[pid]]
        direct_count = sum(
            any(together(pid, other) for other in direct[pid])
            for pid in connected
        )
        group_count = sum(
            any(together(pid, other) for other in groupmates[pid])
            for pid in connected
        )
        return {
            "mutual_pairs_kept": sum(together(*pair) for pair in mutual),
            "mutual_pairs_total": len(mutual),
            "direct_companions": direct_count,
            "group_companions": group_count,
            "connected_players": len(connected),
            "honoured_requests": sum(
                together(source, target)
                for source, target in self.friend_requests
            ),
            "isolated_players": [
                pid for pid in connected
                if pd.notna(assignments[pid])
                and not any(together(pid, other) for other in groupmates[pid])
            ],
        }

    def describe_companions(self, player_id, labels):
        if pd.isna(self.assignments[player_id]):
            return "Awaiting assignment"
        direct, groupmates = self.friend_relationships()
        categories = {
            "Mutual": [], "Direct request": [], "Friends-of-friends": [],
        }
        for other in sorted(groupmates[player_id], key=labels.get):
            if (
                pd.isna(self.assignments[other])
                or self.assignments[other] != self.assignments[player_id]
            ):
                continue
            if (
                (player_id, other) in self.friend_requests
                and (other, player_id) in self.friend_requests
            ):
                category = "Mutual"
            elif other in direct[player_id]:
                category = "Direct request"
            else:
                category = "Friends-of-friends"
            categories[category].append(labels[other])
        descriptions = [
            f"{category}: {', '.join(names)}"
            for category, names in categories.items() if names
        ]
        return "; ".join(descriptions) or "No friend-group member on this team"

    def describe_group_assignments(self, group, labels):
        by_team = {}
        for pid in sorted(group, key=labels.get):
            team = self.assignments[pid]
            team = None if pd.isna(team) else int(team)
            by_team.setdefault(team, []).append(labels[pid])
        entries = []
        for team in sorted(by_team, key=lambda value: (value is None, value)):
            placement = (
                "Unassigned" if team is None
                else f"Team {team} ({self.practice_days[team]})"
            )
            entries.append(f"{placement}: {', '.join(by_team[team])}")
        return "; ".join(entries)

    def get_friend_group_report(self):
        """Describe actual splits, including those selected by optimization."""
        if self.assignments is None:
            raise ValueError("Call prepare_assignments() first")
        labels = self.player_labels()
        _, maximum = self.team_size_bounds
        rows = []
        for group in self.friend_groups:
            if len(group) < 2:
                continue
            placements = self.assignments.loc[group]
            split = placements.dropna().nunique() > 1
            pending = placements.isna().any()
            status = (
                "Split (partial)" if split and pending else
                "Split" if split else "Pending" if pending else "Together"
            )
            common = set.intersection(
                *(set(self.allowed_teams[pid]) for pid in group)
            )
            reasons = []
            if not common:
                reasons.append("No common team under availability/fixed/accepted rules")
            if len(group) > maximum:
                reasons.append(
                    f"{len(group)} players exceed the {maximum}-player limit"
                )
            if not reasons:
                reasons.append(
                    "Selected draft split; not proven unavoidable" if split
                    else "Assignments incomplete" if pending
                    else "Entire group together"
                )
            rows.append({
                "Status": status,
                "Reason": "; ".join(reasons),
                "Assignments": self.describe_group_assignments(group, labels),
            })
        return pd.DataFrame(rows, columns=["Status", "Reason", "Assignments"])

    def describe_player_constraints(self, player_id, players, labels):
        player = players.loc[player_id]
        restrictions = []
        choices = parse_team_choices(player[FIXED_TEAM])
        if choices is not None:
            restrictions.append("allowed teams " + ", ".join(str(team) for team in sorted(choices)))
        if player_id in self.accepted_locks:
            restrictions.append(f"accepted placement on Team {self.accepted_locks[player_id]}")
        blocked = self.parse_blackout_day(player[BLACKOUT_DAYS])
        if blocked:
            restrictions.append(f"unavailable {blocked}")
        return f"{labels[player_id]}: " + ", ".join(restrictions)

    def get_friend_report(self):
        if self.assignments is None:
            raise ValueError("Call prepare_assignments() first")
        players = self.df_players.set_index(SORT_OUT_NUMBER)
        labels = self.player_labels()
        group_for_player = {
            pid: group for group in self.friend_groups for pid in group
        }
        rows = []
        for source, target in sorted(
            self.friend_requests,
            key=lambda pair: (labels[pair[0]], labels[pair[1]]),
        ):
            source_team = self.assignments[source]
            target_team = self.assignments[target]
            common = self.allowed_teams[source] & self.allowed_teams[target]
            if not common:
                status = "Impossible under constraints"
                details = [
                    self.describe_player_constraints(pid, players, labels)
                    for pid in (source, target)
                ]
                reason = "; ".join(details) + ". No common eligible team."
            elif pd.isna(source_team) or pd.isna(target_team):
                status = "Pending"
                unassigned = [
                    labels[pid] for pid in (source, target)
                    if pd.isna(self.assignments[pid])
                ]
                reason = "Awaiting assignment: " + ", ".join(unassigned)
            elif source_team == target_team:
                status = "Honoured"
                reason = f"Together on Team {int(source_team)}"
            else:
                status = "Not honoured"
                teams = ", ".join(str(team) for team in sorted(common))
                reason = (
                    f"Both eligible for team(s) {teams}; separated in this "
                    "draft. No direct availability/fixed-team conflict."
                )
                if self.optimization_result is not None:
                    reason += (
                        " Result reflects size, balance and friendship "
                        "trade-offs; this split is not proven unavoidable."
                    )
            rows.append({
                "Player": labels[source],
                "Player Team": (
                    "Unassigned" if pd.isna(source_team) else int(source_team)
                ),
                "Requested Friend": labels[target],
                "Friend Team": (
                    "Unassigned" if pd.isna(target_team) else int(target_team)
                ),
                "Mutual Request": (target, source) in self.friend_requests,
                "Status": status,
                "Reason": reason,
                "Player Companions": self.describe_companions(source, labels),
                "Friend Companions": self.describe_companions(target, labels),
                "Group Assignments": self.describe_group_assignments(
                    group_for_player[source], labels
                ),
            })
        return pd.DataFrame(rows, columns=[
            "Player", "Player Team", "Requested Friend", "Friend Team",
            "Mutual Request", "Status", "Reason", "Player Companions",
            "Friend Companions", "Group Assignments",
        ])

    def export_friend_report(self, output_path=None):
        """Export the current named report, without assessment scores."""
        output_path = (
            Path(OUTPUT_FOLDER) / FRIEND_REPORT_FILE
            if output_path is None else Path(output_path)
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self.get_friend_report().to_csv(
            output_path, index=False, encoding="utf-8-sig"
        )
        return output_path

    def export_draft_snapshot(self, output_path, overwrite=False):
        """Save draft inputs and assignments, without an ID reservation registry."""
        if self.assignments is None or self.assignments.isna().any():
            raise ValueError("A draft snapshot requires complete assignments")
        self.build_allowed_teams()
        payload = {
            "schema_version": 1,
            "accepted_locks": self.accepted_locks,
            "change_history": self.change_history,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "players": json.loads(self.df_players.to_json(orient="records")),
            "evaluation_evidence": json.loads(
                self.df_evaluation_evidence.to_json(orient="records")
            ) if self.df_evaluation_evidence is not None else [],
            "assignments": {str(pid): int(team)
                            for pid, team in self.assignments.items()},
            "practice_days": self.practice_days,
            "allowed_teams": {pid: sorted(teams)
                              for pid, teams in self.allowed_teams.items()},
            "friend_requests": [list(pair) for pair in sorted(self.friend_requests)],
            "friend_groups": self.friend_groups,
            "settings": {
                "size_bounds": list(self.team_size_bounds),
                "tall_target": self.tall_target_per_team,
                "second_year_target": SECOND_YEAR_TARGET_PER_TEAM,
                "balance_unobserved_players": BALANCE_UNOBSERVED_PLAYERS,
                "handler_target": 2,
                "spread_limit": (self.optimization_result or {}).get("spread_limit"),
                "friend_balance_tolerance": FRIEND_BALANCE_TOLERANCE,
            },
            "report_details": self.report_details,
        }
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w" if overwrite else "x", encoding="utf-8") as target:
            json.dump(payload, target, indent=2, ensure_ascii=False, allow_nan=False)
            target.write("\n")
        return path

    def get_assessment_report(self):
        """Long-form evidence: one row per player and assessed metric."""
        labels = self.player_labels()
        evidence = self.df_evaluation_evidence
        rows = []
        for _, player in self.df_players.iterrows():
            pid = player[SORT_OUT_NUMBER]
            team = (
                self.assignments[pid]
                if self.assignments is not None else pd.NA
            )
            for metric in METRICS:
                count = int(player[f"{metric} Assessment Count"])
                if count == 0:
                    status = "Imputed; no assessments"
                elif count == 1:
                    status = "Single assessment"
                else:
                    status = "Multiple assessments"
                matches = evidence.loc[
                    evidence[SORT_OUT_NUMBER].eq(pid).fillna(False)
                    & evidence[metric].notna()
                ]
                values = "; ".join(
                    f"{row['Evaluation Source']}: {float(row[metric]):g}"
                    for _, row in matches.iterrows()
                )
                rows.append({
                    "Player": labels[pid], "Team": team, "Metric": metric,
                    "Observed Mean": player[f"{metric} Observed Mean"],
                    "Assessment Count": count,
                    "Min": player[f"{metric} Min"],
                    "Max": player[f"{metric} Max"],
                    "Range": player[f"{metric} Range"],
                    "Rating Used": player[metric],
                    "Imputed": bool(player[f"{metric} Imputed"]),
                    "Evidence": status, "Evaluations": values,
                })
        return pd.DataFrame(rows, columns=[
            "Player", "Team", "Metric", "Observed Mean", "Assessment Count",
            "Min", "Max", "Range", "Rating Used", "Imputed", "Evidence",
            "Evaluations",
        ])

    def get_player_evidence_report(self):
        columns = [
            "Birth Year", "Age Cohort", HEIGHT, "Height Band", "Height Min Cm",
            "Height Max Cm", "Height Evidence", "Coach Tall Flag",
            "Tall Note Count", "Tall Review Count", "Height Needs Review",
            "Ball-Handling Observed Mean", "Ball-Handling Assessment Count",
            "Top Ball-Handling Pool",
            "Tall Evidence Pool", "No Evaluation Match",
        ]
        report = self.df_players.set_index(SORT_OUT_NUMBER)[columns].copy()
        labels = self.player_labels()
        report.insert(0, "Player", report.index.map(labels))
        report.insert(1, "Team", (
            self.assignments.reindex(report.index)
            if self.assignments is not None else pd.NA
        ))
        return report.reset_index(drop=True)

    def get_evaluation_notes_report(self):
        labels = self.player_labels()
        rows = []
        evidence = self.df_evaluation_evidence
        for _, row in evidence.loc[evidence[NOTES].notna()].iterrows():
            pid = row[SORT_OUT_NUMBER]
            matched = pd.notna(pid) and pid in labels
            registration = (
                "Matched" if matched else "Missing ID" if pd.isna(pid)
                else "Unmatched evaluation ID"
            )
            flags = []
            if row["Tall Note"]:
                flags.append("Tall description")
            if row["Tall Mention Review"]:
                flags.append("Tall mention needs review")
            rows.append({
                "Player": labels[pid] if matched else
                f"{row[FIRST_NAME]} {row[LAST_NAME]}",
                "Evaluation Source": row["Evaluation Source"],
                "Notes": row[NOTES], "Registration Match": registration,
                "Height Interpretation": "; ".join(flags) or "No tall flag",
            })
        return pd.DataFrame(rows, columns=[
            "Player", "Evaluation Source", "Notes", "Registration Match",
            "Height Interpretation",
        ])

    def export_assessment_reports(self, output_folder=None):
        folder = Path(
            OUTPUT_FOLDER if output_folder is None else output_folder
        )
        folder.mkdir(parents=True, exist_ok=True)
        reports = {
            "assessment_summary.csv": self.get_assessment_report(),
            "player_evidence.csv": self.get_player_evidence_report(),
            "evaluation_notes.csv": self.get_evaluation_notes_report(),
        }
        paths = []
        for filename, report in reports.items():
            output_path = folder / filename
            report.to_csv(output_path, index=False, encoding="utf-8-sig")
            paths.append(output_path)
        return paths

    def print_evidence_summary(self):
        players = self.df_players
        rows = []
        for metric in METRICS:
            counts = players[f"{metric} Assessment Count"]
            rows.append({
                "Metric": metric, "Assessed": int(counts.gt(0).sum()),
                "Single": int(counts.eq(1).sum()),
                "2+": int(counts.ge(2).sum()),
                "Different Scores": int(
                    players[f"{metric} Range"].gt(0).sum()
                ),
                "Imputed": int(counts.eq(0).sum()),
            })
        print("\nAssessment coverage (player counts):")
        print(pd.DataFrame(rows).to_string(index=False))
        print(
            "Players with no matching evaluation row: "
            f"{int(players['No Evaluation Match'].sum())} "
            "(matched rows with blank ratings excluded)."
        )
        ranges = players["Height Band"].notna()
        note_only = ~ranges & players["Coach Tall Flag"]
        unknown = ~ranges & ~players["Coach Tall Flag"]
        print(
            f"Height ranges: {int(ranges.sum())}; coach-description only: "
            f"{int(note_only.sum())}; unknown: {int(unknown.sum())}"
        )
        cohorts = players["Age Cohort"].fillna("Unknown").value_counts()
        print("Age cohorts: " + "; ".join(
            f"{cohort}: {count}" for cohort, count in cohorts.items()
        ))
        pool_size = int(players["Top Ball-Handling Pool"].sum())
        print(
            f"Top observed ball-handling pool: {pool_size} "
            f"(target {TOP_BALL_HANDLER_COUNT}; cutoff ties included; "
            "imputed ratings excluded)."
        )
        for issue in self.data_issues:
            print(f"Data review: {issue}")

    def print_results(self):
        columns = [
            SORT_OUT_NUMBER, FIRST_NAME, LAST_NAME, ASSIGNED_TEAM, OVERALL,
        ]
        if self.optimization_result is None:
            print("Initial placements; friend-group seeds are provisional.")
        else:
            result = self.optimization_result
            print("Completed draft: best solution found within solver limits.")
            print(
                f"Balance pass: {result['balance_status']}; "
                f"friendship pass: {result['friendship_status']}; "
                f"ball-handling pass: {result['coverage_status']}"
            )
            print(
                f"Team-to-team spread in Overall Level: "
                f"{result['actual_spread']:.3f}; "
                f"friend requests honoured: {result['honoured_requests']}/"
                f"{len(self.friend_requests)}"
            )
            before = result["friendship_before_coverage"]
            print(
                "Before -> after secondary passes: top observed handlers "
                f"{result['top_handlers_before']} -> "
                f"{result['top_handlers_after']}; Overall Level spread "
                f"{result['pre_coverage_spread']:.3f} -> "
                f"{result['actual_spread']:.3f}"
            )
            print(
                "Friendship comparison: mutual pairs "
                f"{before['mutual_pairs_kept']} -> "
                f"{result['mutual_pairs_kept']}; direct companions "
                f"{before['direct_companions']} -> "
                f"{result['direct_companions']}; group companions "
                f"{before['group_companions']} -> "
                f"{result['group_companions']}; honoured requests "
                f"{before['honoured_requests']} -> "
                f"{result['honoured_requests']}"
            )
            if result["moves_for_coverage"]:
                print("Moves across secondary passes: " + "; ".join(
                    f"{name}: Team {old} -> Team {new}"
                    for name, old, new in result["moves_for_coverage"]
                ))
            for name, summary in result["secondary_targets"].items():
                target_text = (
                    "even counts across teams" if summary["target"] is None
                    else f"{summary['target']}/team"
                )
                print(
                    f"{name.replace('_', ' ').title()} ({target_text}): "
                    f"{summary['before']} -> {summary['after']}; "
                    f"{summary['status']}"
                )
            target_rows = []
            for team in sorted(self.practice_days):
                target_rows.append({
                    "Team": team,
                    "Top Handlers": result["top_handlers_after"][team],
                    "Handler Shortfall": max(0, 2 - result["top_handlers_after"][team]),
                    "Tall Evidence": result["tall_after"][team],
                    "Tall Shortfall": max(0, self.tall_target_per_team - result["tall_after"][team]),
                    "Year 2": result["second_year_after"][team],
                    "Year 2 Shortfall": max(0, SECOND_YEAR_TARGET_PER_TEAM - result["second_year_after"][team]),
                    "No Evaluation Match": result["unobserved_after"][team],
                })
            print("Final secondary counts and unmet targets:")
            print(pd.DataFrame(target_rows).to_string(index=False))
        minimum, maximum = self.team_size_bounds
        size_text = (
            str(minimum) if minimum == maximum else f"{minimum}-{maximum}"
        )
        print(f"Target team size: {size_text}")
        for number, team in self.teams.items():
            strength = "N/A" if team.empty else f"{team[OVERALL].mean():.2f}"
            print(
                f"\nTeam {number}: {self.practice_days[number]} | "
                f"Players: {len(team)} | Team Average Overall Level: {strength}"
            )
            print(team[columns].to_string(index=False))
        print("\nUnassigned players:")
        print(self.get_unassigned_players()[columns].to_string(index=False))
        print("\nFriend-group placements:")
        for _, group in self.get_friend_group_report().iterrows():
            print(f"{group['Status']} => {group['Assignments']}")
            if group['Status'] != "Together":
                print(f"  Reason: {group['Reason']}")
        stats = self.friendship_statistics()
        print(
            f"Mutual pairs together: {stats['mutual_pairs_kept']}/"
            f"{stats['mutual_pairs_total']}; players with a direct friend: "
            f"{stats['direct_companions']}/{stats['connected_players']}; "
            "players with only friends-of-friends: "
            f"{stats['group_companions'] - stats['direct_companions']}"
        )
        if stats['isolated_players']:
            labels = self.player_labels()
            print(
                "Players without a friend-group member on their team: "
                + ", ".join(labels[pid] for pid in stats['isolated_players'])
            )
        if PRINT_DETAILED_FRIEND_REQUESTS:
            print("\nDetailed friend requests:")
            print(self.get_friend_report().drop(
                columns=["Group Assignments"]
            ).to_string(index=False))

    def run(self):
        self.read_data()
        self.transform_data()
        for label, df in [
            (
                "Generated IDs",
                self.df_players.loc[self.df_players["Generated Id"]],
            ),
            (
                "Evaluation rows without IDs (unused)",
                self.df_evaluations_missing_id,
            ),
            (
                "Evaluation IDs absent from registration",
                self.df_unmatched_evaluations,
            ),
        ]:
            if not df.empty:
                print(f"\n{label}:\n{df.to_string(index=False)}")
        self.prepare_assignments()
        for issue in self.friend_issues:
            print(f"Friend request needs review: {issue}")
        if RUN_OPTIMIZATION:
            self.optimize_teams()
        self.print_results()
        self.print_evidence_summary()
        if EXPORT_FRIEND_REPORT:
            report_path = self.export_friend_report()
            print(f"\nFriend report saved to {report_path}")
        if EXPORT_ASSESSMENT_REPORTS:
            for report_path in self.export_assessment_reports():
                print(f"Assessment evidence saved to {report_path}")
        if GENERATE_TEAM_REPORT_PDF and RUN_OPTIMIZATION:
            self.read_report_config()
            try:
                from teams_report import render_report
            except ImportError as exc:
                raise RuntimeError(
                    "Team Draft Report requires matplotlib and reportlab: "
                    "python -m pip install matplotlib reportlab"
                ) from exc
            report_path = render_report(
                self, OUTPUT_FOLDER / TEAM_REPORT_FILE,
                report_details=self.report_details,
            )
            print(f"Team Draft Report saved to {report_path}")
        if self.assignments.notna().all():
            snapshot = self.export_draft_snapshot(
                Path(OUTPUT_FOLDER) / "latest_draft.json", overwrite=True,
            )
            print(f"Draft snapshot saved to {snapshot}")


if __name__ == "__main__":
    AutoDrafter().run()
